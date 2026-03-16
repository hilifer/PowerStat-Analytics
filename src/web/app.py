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
    """确保 processed_emails 表存在。"""
    with db.connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE NOT NULL,
                filename    TEXT,
                email_subject TEXT,
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
               (fingerprint, filename, email_subject, email_date)
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

    # ---- 刷新邮件（带防重复） ----
    @app.route("/refresh", methods=["POST"])
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

                for i, att in enumerate(attachments, 1):
                    date_str = att.email_date.strftime("%Y-%m-%d %H:%M") if att.email_date else ""
                    fp = _email_fingerprint(att.email_subject, att.email_sender,
                                            date_str, att.filename)

                    if _is_already_processed(db, fp):
                        skipped += 1
                        continue

                    status["progress"] = f"处理 {i}/{len(attachments)}: {att.filename}"
                    source_info = {
                        "email_date": att.email_date,
                        "email_subject": att.email_subject,
                        "filename": att.filename,
                    }

                    # 智能处理：自动检测文件类型，递归解压
                    queue = [(att.filepath, source_info)]
                    processed_paths = set()

                    while queue:
                        fpath, sinfo = queue.pop(0)
                        if fpath in processed_paths:
                            continue
                        processed_paths.add(fpath)

                        result = dispatcher.process(fpath, sinfo)

                        # 入库电表记录
                        for rec in result["records"]:
                            try:
                                meter_id = db.upsert_meter(
                                    meter_number=rec.get("meter_number", ""),
                                    user_id=rec.get("user_id", ""),
                                    meter_type=rec.get("meter_type", "未知"),
                                    asset_number=rec.get("asset_number"),
                                    multiplier=rec.get("multiplier", 1.0),
                                    project_name=rec.get("project_name"),
                                )
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
                                log.error("入库失败: %s", e)

                        # 入库 OCR 单价
                        for ocr in result["ocr_results"]:
                            if ocr.user_id and ocr.reading_month:
                                db.upsert_price(
                                    user_id=ocr.user_id,
                                    reading_month=ocr.reading_month,
                                    sharp_peak_price=ocr.sharp_peak_price,
                                    peak_price=ocr.peak_price,
                                    flat_price=ocr.flat_price,
                                    valley_price=ocr.valley_price,
                                    source_file=ocr.source_file,
                                )

                        # 子文件加入队列
                        for sub in result["sub_files"]:
                            queue.append((sub, sinfo))

                    # 归档到月份目录
                    if not att.is_body:
                        month_match = re.search(r'(\d{4})[-_年]?(\d{1,2})', att.filename)
                        reading_month = (
                            f"{month_match.group(1)}-{month_match.group(2).zfill(2)}"
                            if month_match else
                            (att.email_date.strftime("%Y-%m") if att.email_date else "unknown")
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

    # ---- CSV 文件下载 ----
    @app.route("/download-csv/<filename>")
    def download_csv(filename):
        csv_dir = config.get("storage", "csv_export_dir", default="output/data")
        safe_dir = Path(csv_dir).resolve()
        target = (safe_dir / filename).resolve()
        if not str(target).startswith(str(safe_dir)) or not target.exists():
            abort(404)
        return send_from_directory(str(safe_dir), filename, as_attachment=True)
