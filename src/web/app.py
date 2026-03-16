"""Flask Web 应用：电费数据管理系统的 Web 界面。"""

import hashlib
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_from_directory, flash, abort,
)

from src.config_loader import config
from src.data.models import Database
from src.logger import log

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    """创建并配置 Flask 应用。"""
    config.load()

    project_root = Path(__file__).parent.parent.parent
    template_dir = project_root / "templates"
    static_dir = project_root / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.secret_key = os.environ.get("FLASK_SECRET", "powerstat-dev-key-change-me")

    db = Database()
    app.config["DB"] = db
    app.config["PROJECT_ROOT"] = str(project_root)

    # 刷新任务锁（防止重复点击）
    app.config["REFRESH_LOCK"] = threading.Lock()
    app.config["REFRESH_STATUS"] = {
        "running": False,
        "progress": "",
        "last_run": None,
        "result": None,
    }

    _register_routes(app, db)
    return app


# ---------------------------------------------------------------------------
# 已处理邮件指纹跟踪（避免重复处理）
# ---------------------------------------------------------------------------

def _email_fingerprint(subject: str, sender: str, date_str: str, filename: str) -> str:
    """生成邮件+附件的唯一指纹。"""
    raw = f"{subject}|{sender}|{date_str}|{filename}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _init_processed_table(db: Database):
    """确保 processed_emails 表存在（兼容旧数据库）。"""
    with db.connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE NOT NULL,
                filename    TEXT,
                subject     TEXT,
                email_date  TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _is_already_processed(db: Database, fingerprint: str) -> bool:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None


def _mark_processed(db: Database, fingerprint: str, filename: str,
                    subject: str, date_str: str):
    with db.connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO processed_emails
               (fingerprint, filename, subject, email_date)
               VALUES (?, ?, ?, ?)""",
            (fingerprint, filename, subject, date_str),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: Flask, db: Database):
    _init_processed_table(db)

    # ---- 首页 / 仪表盘 ----
    @app.route("/")
    def index():
        with db.connection() as conn:
            meter_count = conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]
            reading_count = conn.execute("SELECT COUNT(*) FROM monthly_readings").fetchone()[0]
            price_count = conn.execute("SELECT COUNT(*) FROM price_records").fetchone()[0]
            project_count = conn.execute(
                "SELECT COUNT(DISTINCT project_name) FROM meters WHERE project_name IS NOT NULL"
            ).fetchone()[0]
            user_count = conn.execute("SELECT COUNT(DISTINCT user_id) FROM meters").fetchone()[0]
            month_range = conn.execute(
                "SELECT MIN(reading_month), MAX(reading_month) FROM monthly_readings"
            ).fetchone()
            processed_count = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]

        refresh_status = app.config["REFRESH_STATUS"]
        return render_template("index.html",
                               meter_count=meter_count,
                               reading_count=reading_count,
                               price_count=price_count,
                               project_count=project_count,
                               user_count=user_count,
                               month_min=month_range[0] or "-",
                               month_max=month_range[1] or "-",
                               processed_count=processed_count,
                               refresh_status=refresh_status)

    # ---- 强制刷新（增量重新解析：清除处理记录，UPSERT 补全数据） ----
    @app.route("/force-refresh", methods=["POST"])
    def force_refresh():
        """清除已处理邮件记录，重新解析所有邮件。

        已有电表数据不会被删除，UPSERT 逻辑会补全缺失字段。
        锁定的电表不受影响。
        """
        with db.connection() as conn:
            conn.execute("DELETE FROM processed_emails")
            log.info("强制刷新：已清除处理记录，将增量重新解析所有邮件")
        flash("已清除处理记录，正在增量重新解析所有邮件（已有数据不会丢失）…", "info")
        return redirect(url_for("refresh_emails"))

    # ---- 刷新邮件（带防重复） ----
    @app.route("/refresh", methods=["GET", "POST"])
    def refresh_emails():
        lock = app.config["REFRESH_LOCK"]
        status = app.config["REFRESH_STATUS"]

        if not lock.acquire(blocking=False):
            flash("刷新任务正在执行中，请稍后再试。", "warning")
            return redirect(url_for("index"))

        status["running"] = True
        status["progress"] = "正在连接邮箱…"
        status["result"] = None

        def _do_refresh():
            try:
                from src.email_fetcher.fetcher import EmailFetcher
                from src.pipeline import SmartDispatcher
                from src.parsers.multi_pass import MultiPassExtractor
                import re, shutil

                status["progress"] = "正在连接邮箱并搜索邮件…"
                with EmailFetcher() as fetcher:
                    attachments = fetcher.fetch_attachments()

                if not attachments:
                    status["progress"] = "完成"
                    status["result"] = {"new": 0, "skipped": 0, "meters_added": 0}
                    status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    return

                new_count = 0
                skipped = 0
                meters_added = 0

                dispatcher = SmartDispatcher()
                all_sheets = []
                all_ocr = []
                new_attachments = []

                # 第一步：加载所有文件为 DataFrame
                for i, att in enumerate(attachments, 1):
                    date_str = att.email_date.strftime("%Y-%m-%d %H:%M") if att.email_date else ""
                    fp = _email_fingerprint(att.email_subject, att.email_sender,
                                            date_str, att.filename)

                    if _is_already_processed(db, fp):
                        skipped += 1
                        continue

                    new_attachments.append((att, fp, date_str))
                    status["progress"] = f"加载 {i}/{len(attachments)}: {att.filename}"
                    source_info = {
                        "email_date": att.email_date,
                        "email_subject": att.email_subject,
                        "filename": att.filename,
                    }

                    queue = [(att.filepath, source_info)]
                    processed_paths = set()

                    while queue:
                        fpath, sinfo = queue.pop(0)
                        if fpath in processed_paths:
                            continue
                        processed_paths.add(fpath)

                        # 加载为 DataFrame
                        sheets = dispatcher.load_as_dataframes(fpath, sinfo)
                        all_sheets.extend(sheets)

                        # 图片走 OCR
                        file_type = dispatcher.detect_type(fpath)
                        if file_type == "image":
                            try:
                                ocr = dispatcher.ocr_engine.extract_from_image(fpath, sinfo)
                                if ocr.has_any_data():
                                    all_ocr.append(ocr)
                            except Exception as e:
                                log.error("  OCR 失败: %s", e)

                        # 压缩包
                        if file_type == "zip":
                            sub_files = dispatcher._extract_zip(fpath)
                            for sf in sub_files:
                                queue.append((sf, sinfo))

                # 第二步：多轮扫描提取
                status["progress"] = f"多轮扫描提取 ({len(all_sheets)} 个sheet)…"
                extractor = MultiPassExtractor()
                extractor.load_dataframes(all_sheets)
                all_records = extractor.extract_all()

                # 第三步：入库
                status["progress"] = "写入数据库…"
                for rec in all_records:
                    meter_number = rec.get("meter_number", "").strip()
                    if not meter_number:
                        continue
                    try:
                        meter_id = db.upsert_meter(
                            meter_number=meter_number,
                            user_id=rec.get("user_id"),
                            meter_type=rec.get("meter_type", "未知"),
                            asset_number=rec.get("asset_number"),
                            multiplier=rec.get("multiplier"),
                            project_name=rec.get("project_name"),
                        )
                        discount = rec.get("discount")
                        if discount and discount != 1.0:
                            db.update_meter(meter_number, discount=discount)

                        month = rec.get("reading_month")
                        if month and month != "unknown":
                            db.upsert_reading(
                                meter_id=meter_id,
                                reading_month=month,
                                sharp_peak=rec.get("sharp_peak"),
                                peak=rec.get("peak"),
                                flat=rec.get("flat"),
                                valley=rec.get("valley"),
                                total_kwh=rec.get("total_kwh"),
                                source_file=rec.get("source_file"),
                                source_sheet=rec.get("source_sheet"),
                            )
                        meters_added += 1
                    except Exception as e:
                        log.error("入库失败: %s - %s", rec.get("meter_number"), e)

                # OCR 单价入库
                for ocr in all_ocr:
                    if ocr.user_id and ocr.reading_month:
                        try:
                            db.upsert_price(
                                user_id=ocr.user_id,
                                reading_month=ocr.reading_month,
                                sharp_peak_price=ocr.sharp_peak_price,
                                peak_price=ocr.peak_price,
                                flat_price=ocr.flat_price,
                                valley_price=ocr.valley_price,
                                source_file=ocr.source_file,
                            )
                        except Exception as e:
                            log.error("单价入库失败: %s", e)

                # 归档
                for att, fp, date_str in new_attachments:
                    if not att.is_body:
                        reading_month = None
                        for month_match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})', att.filename):
                            y, m = int(month_match.group(1)), int(month_match.group(2))
                            if 2015 <= y <= 2030 and 1 <= m <= 12:
                                reading_month = f"{y}-{str(m).zfill(2)}"
                                break
                        if not reading_month:
                            reading_month = (
                                att.email_date.strftime("%Y-%m") if att.email_date else "unknown"
                            )
                        archive_root = Path(config.get("storage", "archive_root",
                                                       default="output/archive"))
                        dest_dir = archive_root / reading_month
                        dest_dir.mkdir(parents=True, exist_ok=True)

                        src_path = Path(att.filepath)
                        if src_path.exists():
                            dest_path = dest_dir / src_path.name
                            counter = 1
                            while dest_path.exists():
                                dest_path = dest_dir / f"{src_path.stem}_{counter}{src_path.suffix}"
                                counter += 1
                            shutil.copy2(str(src_path), str(dest_path))

                    _mark_processed(db, fp, att.filename, att.email_subject, date_str)
                    new_count += 1

                # 推理补全缺失数据
                status["progress"] = "推理补全缺失数据…"
                db.infer_missing_data()

                status["result"] = {
                    "new": new_count,
                    "skipped": skipped,
                    "meters_added": meters_added,
                }
                status["progress"] = "完成"
                status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            except Exception as e:
                log.error("刷新失败: %s", e, exc_info=True)
                status["progress"] = f"错误: {e}"
                status["result"] = {"error": str(e)}
            finally:
                status["running"] = False
                lock.release()

        t = threading.Thread(target=_do_refresh, daemon=True)
        t.start()

        flash("刷新任务已启动，请稍候…", "info")
        return redirect(url_for("index"))

    # ---- 刷新状态轮询 API ----
    @app.route("/api/refresh-status")
    def refresh_status_api():
        return jsonify(app.config["REFRESH_STATUS"])

    # ---- 电表列表 ----
    @app.route("/meters")
    def meters_list():
        project = request.args.get("project")
        user_id = request.args.get("user_id")
        meters = db.get_meters(project_name=project, user_id=user_id)
        projects = db.get_projects()
        user_ids = db.get_user_ids(project_name=project)
        return render_template("meters.html",
                               meters=meters, projects=projects,
                               user_ids=user_ids,
                               sel_project=project, sel_user_id=user_id)

    # ---- 单个电表详情 ----
    @app.route("/meters/<int:meter_id>")
    def meter_detail(meter_id):
        with db.connection() as conn:
            meter = conn.execute("SELECT * FROM meters WHERE id = ?", (meter_id,)).fetchone()
            if not meter:
                abort(404)
            meter = dict(meter)
            readings = conn.execute(
                """SELECT r.*, p.sharp_peak_price, p.peak_price, p.flat_price, p.valley_price
                   FROM monthly_readings r
                   LEFT JOIN price_records p ON p.user_id = ? AND p.reading_month = r.reading_month
                   WHERE r.meter_id = ?
                   ORDER BY r.reading_month""",
                (meter["user_id"], meter_id)
            ).fetchall()
            readings = [dict(r) for r in readings]
        return render_template("meter_detail.html", meter=meter, readings=readings)

    # ---- 账单查询 ----
    @app.route("/bills")
    def bills():
        project = request.args.get("project")
        user_id = request.args.get("user_id")
        meter = request.args.get("meter")
        month_from = request.args.get("from")
        month_to = request.args.get("to")

        results = db.get_monthly_bill(
            project_name=project, user_id=user_id,
            meter_number=meter, month_from=month_from, month_to=month_to,
        )
        projects = db.get_projects()
        user_ids = db.get_user_ids()
        return render_template("bills.html",
                               bills=results, projects=projects, user_ids=user_ids,
                               sel_project=project, sel_user_id=user_id,
                               sel_meter=meter, sel_from=month_from, sel_to=month_to)

    # ---- 归档浏览（按年月分类） ----
    @app.route("/archive")
    def archive_index():
        archive_root = Path(config.get("storage", "archive_root",
                                       default="output/archive"))
        months = {}
        if archive_root.exists():
            for month_dir in sorted(archive_root.iterdir(), reverse=True):
                if month_dir.is_dir() and not month_dir.name.startswith("."):
                    files = []
                    for item in sorted(month_dir.rglob("*")):
                        if item.is_file():
                            rel = item.relative_to(archive_root)
                            files.append({
                                "name": item.name,
                                "path": str(rel),
                                "size_kb": round(item.stat().st_size / 1024, 1),
                                "subfolder": str(item.parent.relative_to(month_dir)) if item.parent != month_dir else "",
                            })
                    months[month_dir.name] = files
        return render_template("archive.html", months=months)

    # ---- 归档文件下载 ----
    @app.route("/archive/download/<path:filepath>")
    def archive_download(filepath):
        archive_root = Path(config.get("storage", "archive_root",
                                       default="output/archive"))
        full_path = archive_root / filepath
        if not full_path.exists() or not full_path.is_file():
            abort(404)
        # 确保路径不会逃逸到归档目录之外
        try:
            full_path.resolve().relative_to(archive_root.resolve())
        except ValueError:
            abort(403)
        return send_from_directory(str(full_path.parent), full_path.name,
                                  as_attachment=True)

    # ---- 可视化 ----
    @app.route("/charts")
    def charts():
        project = request.args.get("project")
        user_id = request.args.get("user_id")

        from src.visualization.charts import ChartGenerator
        gen = ChartGenerator(db)

        suffix = ""
        if project:
            suffix += f"_proj_{project}"
        if user_id:
            suffix += f"_user_{user_id}"

        gen.generate_all(project_name=project, user_id=user_id)

        chart_dir = Path(config.get("visualization", "output_dir",
                                    default="output/charts"))
        chart_files = []
        if chart_dir.exists():
            for f in sorted(chart_dir.glob(f"*{suffix}.png")):
                chart_files.append(f.name)
            # 如果没有带 suffix 的图，显示全部
            if not chart_files:
                chart_files = [f.name for f in sorted(chart_dir.glob("*.png"))]

        projects = db.get_projects()
        user_ids = db.get_user_ids()
        return render_template("charts.html",
                               chart_files=chart_files,
                               projects=projects, user_ids=user_ids,
                               sel_project=project, sel_user_id=user_id)

    # ---- 图表静态文件 ----
    @app.route("/chart-images/<path:filename>")
    def chart_image(filename):
        chart_dir = config.get("visualization", "output_dir", default="output/charts")
        return send_from_directory(chart_dir, filename)

    # ---- 已处理邮件记录 ----
    @app.route("/processed")
    def processed_list():
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM processed_emails ORDER BY processed_at DESC"
            ).fetchall()
            records = [dict(r) for r in rows]
        return render_template("processed.html", records=records)

    # ---- 导出 CSV ----
    @app.route("/export")
    def export_csv():
        db.export_csv()
        csv_dir = config.get("storage", "csv_export_dir", default="output/data")
        flash(f"CSV 已导出到 {csv_dir}", "success")
        return redirect(url_for("index"))

    # ---- 知识图谱 ----
    @app.route("/graph")
    def knowledge_graph():
        project = request.args.get("project")
        from src.knowledge.graph import KnowledgeGraph
        kg = KnowledgeGraph(db)
        kg.build()

        stats = kg.get_stats()
        anomalies = kg.detect_anomalies()
        ranking = kg.get_project_ranking()
        projects = db.get_projects()

        # 生成图谱图片
        graph_image = None
        try:
            path = kg.export_graph_image(project_name=project)
            if path:
                graph_image = Path(path).name
        except Exception as e:
            log.error("图谱可视化失败: %s", e)

        return render_template("graph.html",
                               stats=stats,
                               anomalies=anomalies,
                               ranking=ranking,
                               projects=projects,
                               sel_project=project,
                               graph_image=graph_image)

    # ---- 知识图谱 API：追溯电表 ----
    @app.route("/api/graph/trace/<meter_number>")
    def graph_trace(meter_number):
        from src.knowledge.graph import KnowledgeGraph
        kg = KnowledgeGraph(db)
        kg.build()
        return jsonify(kg.trace_meter(meter_number))

    # ---- 知识图谱 API：项目网络 ----
    @app.route("/api/graph/project/<project_name>")
    def graph_project(project_name):
        from src.knowledge.graph import KnowledgeGraph
        kg = KnowledgeGraph(db)
        kg.build()
        return jsonify(kg.get_project_network(project_name))

    # ---- 知识图谱 API：图谱 JSON（D3.js 用）----
    @app.route("/api/graph/data")
    def graph_data():
        project = request.args.get("project")
        from src.knowledge.graph import KnowledgeGraph
        kg = KnowledgeGraph(db)
        kg.build()

        if project:
            sub_nodes = kg._get_project_subgraph_nodes(project)
            G = kg.G.subgraph(sub_nodes)
        else:
            G = kg.G

        nodes = []
        for nid, attrs in G.nodes(data=True):
            nodes.append({
                "id": nid,
                "label": attrs.get("label", nid),
                "type": attrs.get("type", "unknown"),
            })
        edges = []
        for src, tgt, attrs in G.edges(data=True):
            edges.append({
                "source": src,
                "target": tgt,
                "relation": attrs.get("relation", ""),
            })

        return jsonify({"nodes": nodes, "edges": edges})

    # ---- 电表编辑页面 ----
    @app.route("/meters/<int:meter_id>/edit")
    def meter_edit(meter_id):
        with db.connection() as conn:
            meter = conn.execute("SELECT * FROM meters WHERE id = ?", (meter_id,)).fetchone()
            if not meter:
                abort(404)
            meter = dict(meter)
        projects = db.get_projects()
        return render_template("meter_edit.html", meter=meter, projects=projects)

    # ---- 电表更新 API ----
    @app.route("/api/meters/<meter_number>/update", methods=["POST"])
    def meter_update_api(meter_number):
        data = request.get_json() or request.form.to_dict()
        if not data:
            return jsonify({"error": "无更新数据"}), 400

        # 检查电表是否存在
        meter = db.get_meter(meter_number)
        if not meter:
            return jsonify({"error": f"电表 {meter_number} 不存在"}), 404

        # 检查锁定状态
        if meter.get("is_locked") and "is_locked" not in data:
            return jsonify({"error": "电表已锁定，请先解锁再修改", "is_locked": True}), 403

        # 类型转换
        fields = {}
        for k, v in data.items():
            if k in ("multiplier", "discount"):
                try:
                    fields[k] = float(v)
                except (ValueError, TypeError):
                    pass
            elif k == "is_locked":
                fields[k] = int(v)
            elif k in ("user_id", "meter_type", "asset_number", "project_name"):
                fields[k] = str(v).strip() if v else None

        ok = db.update_meter(meter_number, **fields)
        if ok:
            return jsonify({"success": True, "message": f"电表 {meter_number} 已更新"})
        else:
            return jsonify({"error": "更新失败（电表可能已锁定）"}), 403

    # ---- 电表锁定 API ----
    @app.route("/api/meters/<meter_number>/lock", methods=["POST"])
    def meter_lock_api(meter_number):
        ok = db.lock_meter(meter_number)
        return jsonify({"success": ok, "message": f"电表 {meter_number} 已锁定"})

    # ---- 电表解锁 API ----
    @app.route("/api/meters/<meter_number>/unlock", methods=["POST"])
    def meter_unlock_api(meter_number):
        ok = db.unlock_meter(meter_number)
        return jsonify({"success": ok, "message": f"电表 {meter_number} 已解锁"})

    # ---- 批量锁定/解锁 API ----
    @app.route("/api/meters/batch-lock", methods=["POST"])
    def meter_batch_lock():
        data = request.get_json()
        if not data or "meter_numbers" not in data:
            return jsonify({"error": "请提供 meter_numbers 列表"}), 400

        action = data.get("action", "lock")
        count = 0
        for mn in data["meter_numbers"]:
            if action == "lock":
                db.lock_meter(mn)
            else:
                db.unlock_meter(mn)
            count += 1

        return jsonify({"success": True, "count": count,
                        "message": f"已{'锁定' if action == 'lock' else '解锁'} {count} 个电表"})

    # ---- 电表详情 API ----
    @app.route("/api/meters/<meter_number>")
    def meter_detail_api(meter_number):
        meter = db.get_meter(meter_number)
        if not meter:
            return jsonify({"error": "电表不存在"}), 404
        return jsonify(meter)

    # ---- CSV 文件下载 ----
    @app.route("/download-csv/<filename>")
    def download_csv(filename):
        csv_dir = config.get("storage", "csv_export_dir", default="output/data")
        safe_dir = Path(csv_dir).resolve()
        target = (safe_dir / filename).resolve()
        if not str(target).startswith(str(safe_dir)) or not target.exists():
            abort(404)
        return send_from_directory(str(safe_dir), filename, as_attachment=True)
