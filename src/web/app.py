"""Flask Web 应用：电费数据管理系统的 Web 界面。"""

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_from_directory, flash, abort, send_file,
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
        "logs": [],          # 详细进度日志列表
        "started_at": None,  # 任务开始时间
    }

    # 数据更新独立状态（与首页更新分开）
    app.config["BILL_REFRESH_LOCK"] = threading.Lock()
    app.config["BILL_REFRESH_STATUS"] = {
        "running": False,
        "progress": "",
        "last_run": None,
        "result": None,
        "logs": [],
        "started_at": None,
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
# 归档辅助函数
# ---------------------------------------------------------------------------

def _infer_month(filename: str, email_date=None) -> str:
    """从文件名或邮件日期推断月份。"""
    import re
    for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})', filename or ""):
        y, m = int(match.group(1)), int(match.group(2))
        if 2015 <= y <= 2035 and 1 <= m <= 12:
            return f"{y}-{str(m).zfill(2)}"
    if email_date:
        return email_date.strftime("%Y-%m")
    return "unknown"


def _infer_project(db: Database, filename: str, email_subject: str = "") -> str:
    """从文件名/邮件主题推断项目名。优先匹配数据库已有项目。"""
    import re
    # 先尝试匹配数据库中已有的项目名
    try:
        projects = db.get_project_names()
        for proj in projects:
            if proj and (proj in (filename or "") or proj in (email_subject or "")):
                return proj
    except Exception:
        pass

    # 从文件名/邮件主题提取项目名
    for text in [email_subject, filename]:
        if not text:
            continue
        text = re.sub(r'^(?:Fwd?|Re)\s*[:：]\s*', '', text, flags=re.IGNORECASE)
        m = re.match(
            r'([\u4e00-\u9fff、·]+?)(?:\d|电费|月|抄表|账单|统计)',
            text.strip()
        )
        if m:
            name = m.group(1).rstrip('、·')
            if len(name) >= 2:
                return name

    return None


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

    # ---- 全部更新（清空所有数据，从头开始） ----
    @app.route("/full-reset", methods=["POST"])
    def full_reset():
        """清空所有电表、抄表、单价、处理记录，删除已下载附件，然后重新抓取。"""
        with db.connection() as conn:
            conn.execute("DELETE FROM monthly_readings")
            conn.execute("DELETE FROM price_records")
            conn.execute("DELETE FROM meters")
            conn.execute("DELETE FROM processed_emails")
            log.info("全部更新：已清空所有数据表")

        # 删除已下载的附件文件
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        if temp_dir.exists():
            import shutil
            shutil.rmtree(str(temp_dir), ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            log.info("全部更新：已清空附件目录 %s", temp_dir)

        flash("已清空所有数据和下载文件，正在从头抓取邮件…", "warning")
        return redirect(url_for("refresh_emails"))

    # ---- 增量更新（只下载未处理的新邮件） ----
    @app.route("/incremental-refresh", methods=["POST"])
    def incremental_refresh():
        """增量更新：只下载没有下载过的邮件，跳过已处理的。

        已有电表数据不会被删除，新数据通过 UPSERT 补全。
        """
        log.info("增量更新：只下载未处理的新邮件")
        flash("正在增量更新，只下载新邮件…", "info")
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
        status["logs"] = []
        status["started_at"] = datetime.now().strftime("%H:%M:%S")

        def _log(msg):
            """追加一条带时间戳的进度日志。"""
            ts = datetime.now().strftime("%H:%M:%S")
            status["logs"].append(f"[{ts}] {msg}")
            status["progress"] = msg

        def _do_refresh():
            try:
                from src.email_fetcher.fetcher import EmailFetcher
                from src.pipeline import SmartDispatcher
                from src.parsers.multi_pass import MultiPassExtractor
                from src.parsers.text_extractor import extract_meters_from_text, read_text_file
                import re, shutil

                # ============================================================
                # 第一阶段：建立电表档案
                # 下载新附件 → 提取电表信息 → 入库 → 配对 → 补全 → 清理
                # ============================================================
                _log("═══ 第一阶段：建立电表档案 ═══")
                _log("正在连接邮箱并搜索邮件…")
                with EmailFetcher() as fetcher:
                    attachments = fetcher.fetch_attachments()

                _log(f"搜索完成，共找到 {len(attachments)} 个附件")

                if not attachments:
                    _log("没有新附件，完成")
                    status["result"] = {"new": 0, "skipped": 0, "meters_added": 0}
                    status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    return

                new_count = 0
                skipped = 0
                meters_added = 0
                processed_files = []  # 记录处理了哪些文件

                dispatcher = SmartDispatcher()
                all_sheets = []
                all_text_records = []
                new_attachments = []

                # 1.1 加载新附件
                for i, att in enumerate(attachments, 1):
                    date_str = att.email_date.strftime("%Y-%m-%d %H:%M") if att.email_date else ""
                    fp = _email_fingerprint(att.email_subject, att.email_sender,
                                            date_str, att.filename)

                    if _is_already_processed(db, fp):
                        skipped += 1
                        continue

                    new_attachments.append((att, fp, date_str))
                    processed_files.append(att.filename)
                    _log(f"加载附件 [{len(new_attachments)}/{len(attachments)}] {att.filename}")
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

                        sheets = dispatcher.load_as_dataframes(fpath, sinfo)
                        all_sheets.extend(sheets)

                        file_type = dispatcher.detect_type(fpath)

                        # 文本文件提取电表档案
                        if file_type in ("text", "unknown") or (file_type == "html" and not sheets):
                            try:
                                text = read_text_file(fpath)
                                text_records = extract_meters_from_text(text, fpath)
                                all_text_records.extend(text_records)
                            except Exception as e:
                                log.error("  文本提取失败: %s", e)

                        if file_type == "zip":
                            sub_files = dispatcher._extract_zip(fpath)
                            for sf in sub_files:
                                queue.append((sf, sinfo))

                # 1.2 多轮扫描提取电表档案 + 读数（完整模式）
                _log(f"多轮扫描提取电表档案 + 读数（{len(all_sheets)} 个 sheet，{len(new_attachments)} 个新附件，跳过 {skipped} 个已处理）")
                extractor = MultiPassExtractor()
                extractor.load_dataframes(all_sheets)
                all_records = extractor.extract_all()

                if all_text_records:
                    all_records.extend(all_text_records)

                # 1.3 写入电表档案 + 抄表读数
                readings_added = 0
                _log(f"提取到 {len(all_records)} 条记录，写入电表档案和读数…")
                for rec in all_records:
                    meter_number = rec.get("meter_number", "").strip()
                    if not meter_number:
                        continue
                    try:
                        db.upsert_meter(
                            meter_number=meter_number,
                            user_id=rec.get("user_id"),
                            meter_type=rec.get("meter_type", "未知"),
                            asset_number=rec.get("asset_number"),
                            multiplier=rec.get("multiplier"),
                            project_name=rec.get("project_name"),
                            source_file=rec.get("source_file"),
                            source_sheet=rec.get("source_sheet"),
                        )
                        discount = rec.get("discount")
                        if discount and discount != 1.0:
                            db.update_meter(meter_number, discount=discount)
                        meters_added += 1
                    except Exception as e:
                        log.error("电表入库失败: %s - %s", rec.get("meter_number"), e)

                    # 写入抄表读数
                    month = rec.get("reading_month")
                    if not month or month == "unknown":
                        continue
                    try:
                        with db.connection() as conn:
                            row = conn.execute(
                                "SELECT id FROM meters WHERE meter_number = ?",
                                (meter_number,),
                            ).fetchone()
                        if not row:
                            continue
                        meter_id = row["id"]
                        db.upsert_reading(
                            meter_id=meter_id,
                            reading_month=month,
                            sharp_peak=rec.get("sharp_peak"),
                            peak=rec.get("peak"),
                            flat=rec.get("flat"),
                            valley=rec.get("valley"),
                            total_kwh=rec.get("total_kwh"),
                            rev_sharp_peak=rec.get("rev_sharp_peak"),
                            rev_peak=rec.get("rev_peak"),
                            rev_flat=rec.get("rev_flat"),
                            rev_valley=rec.get("rev_valley"),
                            rev_total=rec.get("rev_total"),
                            cur_sharp_peak=rec.get("cur_sharp_peak"),
                            cur_peak=rec.get("cur_peak"),
                            cur_flat=rec.get("cur_flat"),
                            cur_valley=rec.get("cur_valley"),
                            cur_total=rec.get("cur_total"),
                            prev_sharp_peak=rec.get("prev_sharp_peak"),
                            prev_peak=rec.get("prev_peak"),
                            prev_flat=rec.get("prev_flat"),
                            prev_valley=rec.get("prev_valley"),
                            prev_total=rec.get("prev_total"),
                            stat_date=rec.get("stat_date"),
                            source_file=rec.get("source_file"),
                            source_sheet=rec.get("source_sheet"),
                        )
                        readings_added += 1
                    except Exception as e:
                        log.error("抄表入库失败: %s - %s", meter_number, e)

                _log(f"电表档案写入完成: {meters_added} 条，抄表读数: {readings_added} 条")

                # 1.4 保存配对关系
                if extractor.pairs:
                    _log(f"保存 {len(extractor.pairs)} 组电表配对关系…")
                    for gen_meter, grid_meter in extractor.pairs:
                        try:
                            db.set_meter_pair(gen_meter, grid_meter)
                        except Exception as e:
                            log.error("配对保存失败: %s <-> %s: %s", gen_meter, grid_meter, e)

                for att, fp, date_str in new_attachments:
                    _mark_processed(db, fp, att.filename, att.email_subject, date_str)
                    new_count += 1

                # 1.5 提取单价数据（图片 OCR）
                price_saved = 0
                ocr_count = 0
                all_ocr = []
                for att, fp, date_str in new_attachments:
                    fpath = att.filepath
                    file_type = dispatcher.detect_type(fpath)
                    if file_type != "image":
                        continue
                    ocr_count += 1
                    try:
                        source_info = {
                            "email_date": att.email_date,
                            "email_subject": att.email_subject,
                            "filename": att.filename,
                        }
                        ocr = dispatcher.ocr_engine.extract_from_image(fpath, source_info)
                        if ocr.has_any_data():
                            all_ocr.append(ocr)
                            if ocr.has_price_data():
                                _log(f"  OCR 单价: {att.filename} → 用户={ocr.user_id or '?'}, "
                                     f"月={ocr.reading_month or '?'}")
                    except Exception as e:
                        log.error("OCR 失败 %s: %s", att.filename, e)

                if all_ocr:
                    _log(f"OCR 识别 {ocr_count} 张图片，有效 {len(all_ocr)} 条，写入数据库…")
                    known_user_ids = set()
                    try:
                        for m in db.get_meters():
                            uid = m.get("user_id")
                            if uid:
                                known_user_ids.add(uid)
                    except Exception:
                        pass

                    from src.pipeline import Pipeline as _Pipeline
                    for ocr in all_ocr:
                        if not ocr.reading_month or not ocr.has_price_data():
                            continue
                        user_id = ocr.user_id
                        if not user_id:
                            continue
                        if user_id not in known_user_ids and known_user_ids:
                            matched = _Pipeline._match_user_id(user_id, known_user_ids)
                            if matched:
                                user_id = matched
                            else:
                                continue
                        try:
                            db.upsert_price(
                                user_id=user_id,
                                reading_month=ocr.reading_month,
                                sharp_peak_price=ocr.sharp_peak_price,
                                peak_price=ocr.peak_price,
                                flat_price=ocr.flat_price,
                                valley_price=ocr.valley_price,
                                average_price=ocr.average_price,
                                source_file=ocr.source_file,
                            )
                            price_saved += 1
                        except Exception as e:
                            log.error("单价入库失败: %s", e)

                    _log(f"单价写入完成: {price_saved} 条")

                # 1.6 推理补全
                _log("推理补全缺失数据…")
                db.infer_missing_data()

                # 1.7 清理不完整电表
                _log("清理数据不全的电表…")
                cleaned = db.cleanup_incomplete_meters()
                if cleaned:
                    _log(f"已清理 {cleaned} 个数据不全的电表")

                with db.connection() as conn:
                    final_meter_count = conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]
                    final_reading_count = conn.execute("SELECT COUNT(*) FROM monthly_readings").fetchone()[0]
                    final_price_count = conn.execute("SELECT COUNT(*) FROM price_records").fetchone()[0]
                _log(f"完成！电表: {final_meter_count}，读数: {final_reading_count}，单价: {final_price_count}")

                status["result"] = {
                    "new": new_count,
                    "skipped": skipped,
                    "meters_added": meters_added,
                    "readings_added": readings_added,
                    "prices_added": price_saved,
                    "cleaned": cleaned,
                    "processed_files": processed_files,
                }

                _log(f"新增 {new_count} 个附件，电表 {meters_added} 条，读数 {readings_added} 条，单价 {price_saved} 条")
                status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            except Exception as e:
                log.error("刷新失败: %s", e, exc_info=True)
                _log(f"错误: {e}")
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

        # 按用户编号分组，体现发电表/上网表配对关系
        meter_groups = {}
        ungrouped = []
        for m in meters:
            uid = m.get("user_id") or ""
            if uid:
                meter_groups.setdefault(uid, []).append(m)
            else:
                ungrouped.append(m)

        projects = db.get_project_names()
        user_ids = db.get_user_ids(project_name=project)
        return render_template("meters.html",
                               meters=meters, meter_groups=meter_groups,
                               ungrouped=ungrouped,
                               projects=projects,
                               user_ids=user_ids,
                               sel_project=project, sel_user_id=user_id)

    # ---- 单个电表详情 ----
    @app.route("/meters/<int:meter_id>")
    def meter_detail(meter_id):
        from collections import OrderedDict
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

            # 查找配对电表（通过 paired_meter_id 或同用户编号不同类型）
            paired_meter = None
            if meter.get("paired_meter_id"):
                row = conn.execute("SELECT * FROM meters WHERE id = ?",
                                   (meter["paired_meter_id"],)).fetchone()
                if row:
                    paired_meter = dict(row)
            if not paired_meter and meter.get("user_id"):
                # 同用户编号下找配对类型
                pair_type = "上网表" if meter["meter_type"] == "发电表" else "发电表"
                row = conn.execute(
                    "SELECT * FROM meters WHERE user_id = ? AND meter_type = ? AND id != ?",
                    (meter["user_id"], pair_type, meter_id)
                ).fetchone()
                if row:
                    paired_meter = dict(row)

            # 查找配对电表的抄表数据并合并
            paired_readings = []
            merged_readings = OrderedDict()
            if paired_meter:
                paired_readings = conn.execute(
                    """SELECT r.*, p.sharp_peak_price, p.peak_price, p.flat_price, p.valley_price
                       FROM monthly_readings r
                       LEFT JOIN price_records p ON p.user_id = ? AND p.reading_month = r.reading_month
                       WHERE r.meter_id = ?
                       ORDER BY r.reading_month""",
                    (paired_meter.get("user_id", ""), paired_meter["id"])
                ).fetchall()
                paired_readings = [dict(r) for r in paired_readings]

                # 确定哪个是发电表、哪个是上网表
                if meter["meter_type"] == "发电表":
                    gen_readings, grid_readings = readings, paired_readings
                else:
                    gen_readings, grid_readings = paired_readings, readings

                # 按月份合并
                all_months = set()
                gen_by_month = {r["reading_month"]: r for r in gen_readings}
                grid_by_month = {r["reading_month"]: r for r in grid_readings}
                all_months.update(gen_by_month.keys())
                all_months.update(grid_by_month.keys())
                for month in sorted(all_months):
                    merged_readings[month] = {
                        "gen": gen_by_month.get(month),
                        "grid": grid_by_month.get(month),
                    }

            # 查找该用户的单价记录
            price_records = []
            if meter.get("user_id"):
                rows = conn.execute(
                    "SELECT * FROM price_records WHERE user_id = ? ORDER BY reading_month",
                    (meter["user_id"],)
                ).fetchall()
                price_records = [dict(r) for r in rows]

        return render_template("meter_detail.html", meter=meter, readings=readings,
                               paired_meter=paired_meter, paired_readings=paired_readings,
                               merged_readings=merged_readings, price_records=price_records)

    # ---- 电费单计算 ----

    @app.route("/bill-calc")
    def bill_calc():
        """电费单计算页面：按项目+月份查看发电统计表。

        计算逻辑（参照发电统计表）：
        - 正向数据（发电量）：发电表的正向有功数据
          电表用量 = 本月表数 - 上月表数
          发电量 = 电表用量 × 倍率
        - 反向数据（上网电量）：上网表的反向有功数据
          电表用量 = 本月表数 - 上月表数
          上网电量 = 电表用量 × 倍率
        - 自发用电量 = 发电量 - 上网电量
        - 金额 = 自发用电量 × 优惠后电价
        """
        from collections import OrderedDict
        from src.config_loader import config

        sel_project = request.args.get("project", "")
        sel_month = request.args.get("month", "")  # 这里是账期月份（billing_month）

        projects = db.get_project_names()
        months = db.get_billing_months()

        # billing_month_offset: 抄表月 + offset = 账期月，所以抄表月 = 账期月 - offset
        bill_offset = int(config.get("billing_month_offset", default=0))

        bill_groups = []  # [{user_id, project_name, month, gen, grid, prev_gen, prev_grid, prices}]

        if sel_project and sel_month:
            # 将账期月份转换为抄表月份
            reading_month = db.offset_month(sel_month, -bill_offset)
            # 上一个抄表月（用于计算电表用量差值）
            prev_reading_month = db.offset_month(reading_month, -1)

            # 查询当月数据（用抄表月份查询）
            raw = db.get_readings_grouped(
                project_name=sel_project,
                reading_month=reading_month,
            )

            # 查询上月数据（用于计算电表用量）
            prev_raw = []
            if prev_reading_month:
                prev_raw = db.get_readings_grouped(
                    project_name=sel_project,
                    reading_month=prev_reading_month,
                )

            # 上月数据按 meter_id 索引
            prev_by_meter = {}
            for r in prev_raw:
                prev_by_meter[r["meter_id"]] = r

            # 分组: user_id -> {gen, grid, prev_gen, prev_grid}
            # month/prev_month 用账期月份展示
            prev_billing_month = db.offset_month(sel_month, -1)
            user_map = OrderedDict()
            for r in raw:
                uid = r["user_id"] or "unknown"
                if uid not in user_map:
                    user_map[uid] = {
                        "user_id": uid,
                        "project_name": r["project_name"],
                        "month": sel_month,
                        "prev_month": prev_billing_month,
                        "gen": None,
                        "grid": None,
                        "prev_gen": None,
                        "prev_grid": None,
                    }
                if r["meter_type"] == "发电表":
                    user_map[uid]["gen"] = r
                    user_map[uid]["prev_gen"] = prev_by_meter.get(r["meter_id"])
                elif r["meter_type"] == "上网表":
                    user_map[uid]["grid"] = r
                    user_map[uid]["prev_grid"] = prev_by_meter.get(r["meter_id"])

            bill_groups = list(user_map.values())

        return render_template("bill_calc.html",
                               bill_groups=bill_groups,
                               projects=projects,
                               months=months,
                               sel_project=sel_project,
                               sel_month=sel_month)

    @app.route("/api/bill-calc/save-prices", methods=["POST"])
    def bill_calc_save_prices():
        """保存电费单页面的单价数据。"""
        from src.config_loader import config as _cfg
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "无数据"})
        bill_offset = int(_cfg.get("billing_month_offset", default=0))
        items = data.get("items", [])
        for item in items:
            user_id = item.get("user_id")
            month = item.get("month")  # 前端传的是账期月份
            prices = item.get("prices", {})
            if user_id and month:
                # 转换为抄表月份存储
                reading_month = db.offset_month(month, -bill_offset)
                db.upsert_price(
                    user_id=user_id,
                    reading_month=reading_month,
                    sharp_peak_price=prices.get("sharp_peak_price"),
                    peak_price=prices.get("peak_price"),
                    flat_price=prices.get("flat_price"),
                    valley_price=prices.get("valley_price"),
                    average_price=prices.get("average_price"),
                    grid_sharp_peak_price=prices.get("grid_sharp_peak_price"),
                    grid_peak_price=prices.get("grid_peak_price"),
                    grid_flat_price=prices.get("grid_flat_price"),
                    grid_valley_price=prices.get("grid_valley_price"),
                    grid_average_price=prices.get("grid_average_price"),
                )
        return jsonify({"ok": True})

    # ---- 账单数据更新（共用核心逻辑） ----

    def _do_bill_update(clear_first: bool, task_type: str = "all", log_fn=None):
        """账单更新核心逻辑。

        task_type: 'readings' 只提取抄表数据, 'prices' 只提取单价, 'all' 两者都做
        clear_first: 全量模式，先清空对应数据
        log_fn: 可选，外部传入的日志函数。不传则写入 BILL_REFRESH_STATUS。
        """
        status = app.config["BILL_REFRESH_STATUS"]

        def _default_log(msg):
            ts = datetime.now().strftime("%H:%M:%S")
            status["logs"].append(f"[{ts}] {msg}")
            status["progress"] = msg

        _log = log_fn or _default_log

        try:
            from src.email_fetcher.fetcher import EmailFetcher
            from src.pipeline import SmartDispatcher
            from src.parsers.multi_pass import MultiPassExtractor
            import re, shutil

            mode_label = "全量" if clear_first else "增量"
            task_labels = {"readings": "抄表数据", "prices": "单价提取", "all": "全部"}
            task_label = task_labels.get(task_type, task_type)

            # ---- 步骤 1：全量时先清空 ----
            if clear_first:
                with db.connection() as conn:
                    if task_type in ("readings", "all"):
                        rc = conn.execute("SELECT COUNT(*) FROM monthly_readings").fetchone()[0]
                        conn.execute("DELETE FROM monthly_readings")
                        _log(f"已清空 {rc} 条抄表数据")
                    if task_type in ("prices", "all"):
                        pc = conn.execute("SELECT COUNT(*) FROM price_records").fetchone()[0]
                        conn.execute("""
                            UPDATE price_records SET
                                sharp_peak_price = NULL, peak_price = NULL,
                                flat_price = NULL, valley_price = NULL,
                                average_price = NULL, source_file = NULL,
                                is_locked = 0
                        """)
                        _log(f"已清空 {pc} 条单价数据（保留结算字段）")

            # ---- 步骤 2：下载新邮件附件 ----
            _log("正在连接邮箱搜索新附件…")
            new_email_count = 0
            try:
                with EmailFetcher() as fetcher:
                    attachments = fetcher.fetch_attachments()
                _log(f"邮箱中共 {len(attachments)} 个附件")
                new_attachment_files = []  # 新附件文件路径（增量模式用）

                for i, att in enumerate(attachments, 1):
                    date_str = att.email_date.strftime("%Y-%m-%d %H:%M") if att.email_date else ""
                    fp = _email_fingerprint(att.email_subject, att.email_sender, date_str, att.filename)

                    if _is_already_processed(db, fp):
                        continue

                    _log(f"  新附件 [{new_email_count + 1}] {att.filename}")
                    new_attachment_files.append(att.filepath)

                    _mark_processed(db, fp, att.filename, att.email_subject, date_str)
                    new_email_count += 1

            except Exception as e:
                _log(f"邮箱连接失败（继续处理已有文件）: {e}")

            if new_email_count > 0:
                _log(f"已下载 {new_email_count} 个新附件")
            else:
                _log("没有新邮件附件")

            # ---- 步骤 3：扫描原始文件 ----
            source_files = []
            if clear_first:
                # 全量模式：扫描所有文件
                temp_dir = Path(config.get("attachments", "temp_dir",
                                           default="output/temp_attachments"))
                archive_root = Path(config.get("storage", "archive_root", default="output/archive"))
                IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"}
                EXCEL_EXTS = {".xlsx", ".xls", ".csv"}
                ALL_EXTS = IMAGE_EXTS | EXCEL_EXTS | {".pdf", ".html", ".htm", ".txt", ".zip"}
                SKIP_PREFIXES = ("~$",)
                for scan_root in [temp_dir, archive_root]:
                    if scan_root.exists():
                        for f in sorted(scan_root.rglob("*")):
                            if f.is_file() and f.suffix.lower() in ALL_EXTS and not f.name.startswith(SKIP_PREFIXES):
                                rel = f.relative_to(scan_root)
                                month_dir = rel.parts[0] if rel.parts else f.parent.name
                                source_info = {"filename": f.name, "archive_month": month_dir}
                                source_files.append((str(f), source_info))
            else:
                # 增量模式：只处理新下载的附件
                for fp in new_attachment_files:
                    fpath = Path(fp)
                    if fpath.exists():
                        month_dir = fpath.parent.name
                        source_info = {"filename": fpath.name, "archive_month": month_dir}
                        source_files.append((str(fpath), source_info))

            _log(f"共 {len(source_files)} 个文件，开始扫描…")

            if not source_files:
                _log("没有文件可处理，完成")
                status["result"] = {"readings_added": 0, "prices_added": 0, "new_emails": new_email_count}
                status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return

            # ---- 步骤 4：提取抄表数据 ----
            dispatcher = SmartDispatcher()
            readings_added = 0
            ocr_count = 0
            price_saved = 0

            if task_type in ("readings", "all"):
                all_sheets = []
                _log(f"[抄表] 开始从 {len(source_files)} 个文件中提取…")
                for i, (fpath, sinfo) in enumerate(source_files, 1):
                    fname = Path(fpath).name
                    if i <= 3 or i % 10 == 0 or i == len(source_files):
                        _log(f"[抄表] 加载文件 [{i}/{len(source_files)}] {fname}")
                    try:
                        sheets = dispatcher.load_as_dataframes(fpath, sinfo)
                        all_sheets.extend(sheets)
                    except Exception as e:
                        log.error("加载文件失败 %s: %s", fname, e)

                _log(f"[抄表] 多轮提取（{len(all_sheets)} 个 sheet）…")
                extractor = MultiPassExtractor()
                extractor.load_dataframes(all_sheets)
                all_records = extractor.extract_all()

                _log(f"[抄表] 提取到 {len(all_records)} 条记录，写入数据库…")
                for rec in all_records:
                    meter_number = rec.get("meter_number", "").strip()
                    if not meter_number:
                        continue
                    month = rec.get("reading_month")
                    if not month or month == "unknown":
                        continue
                    try:
                        with db.connection() as conn:
                            row = conn.execute("SELECT id FROM meters WHERE meter_number = ?",
                                               (meter_number,)).fetchone()
                        if not row:
                            continue
                        meter_id = row["id"]
                        db.upsert_reading(
                            meter_id=meter_id,
                            reading_month=month,
                            sharp_peak=rec.get("sharp_peak"),
                            peak=rec.get("peak"),
                            flat=rec.get("flat"),
                            valley=rec.get("valley"),
                            total_kwh=rec.get("total_kwh"),
                            rev_sharp_peak=rec.get("rev_sharp_peak"),
                            rev_peak=rec.get("rev_peak"),
                            rev_flat=rec.get("rev_flat"),
                            rev_valley=rec.get("rev_valley"),
                            rev_total=rec.get("rev_total"),
                            cur_sharp_peak=rec.get("cur_sharp_peak"),
                            cur_peak=rec.get("cur_peak"),
                            cur_flat=rec.get("cur_flat"),
                            cur_valley=rec.get("cur_valley"),
                            cur_total=rec.get("cur_total"),
                            prev_sharp_peak=rec.get("prev_sharp_peak"),
                            prev_peak=rec.get("prev_peak"),
                            prev_flat=rec.get("prev_flat"),
                            prev_valley=rec.get("prev_valley"),
                            prev_total=rec.get("prev_total"),
                            stat_date=rec.get("stat_date"),
                            source_file=rec.get("source_file"),
                            source_sheet=rec.get("source_sheet"),
                        )
                        readings_added += 1
                    except Exception as e:
                        log.error("抄表入库失败: %s - %s", meter_number, e)

            # ---- 步骤 5：提取单价数据（图片 OCR） ----
            if task_type in ("prices", "all"):
                price_keyword = config.get("price_extraction", "filename_keyword", default="")
                all_ocr = []
                _log(f"[单价] 开始从 {len(source_files)} 个文件中识别图片…")
                skipped_by_filter = 0
                for i, (fpath, sinfo) in enumerate(source_files, 1):
                    fname = Path(fpath).name
                    file_type = dispatcher.detect_type(fpath)
                    if file_type != "image":
                        continue
                    if price_keyword and price_keyword not in fname:
                        skipped_by_filter += 1
                        continue
                    ocr_count += 1
                    if ocr_count <= 3 or ocr_count % 10 == 0:
                        _log(f"[单价] OCR 图片 [{ocr_count}] {fname}")
                    try:
                        ocr = dispatcher.ocr_engine.extract_from_image(fpath, sinfo)
                        if ocr.has_any_data():
                            all_ocr.append(ocr)
                            if ocr.has_price_data():
                                _log(f"  OCR 单价: {fname} → 用户={ocr.user_id or '?'}, "
                                     f"月={ocr.reading_month or '?'}, "
                                     f"尖={ocr.sharp_peak_price}, 峰={ocr.peak_price}, "
                                     f"平={ocr.flat_price}, 谷={ocr.valley_price}, "
                                     f"均价={ocr.average_price}")
                            elif ocr.user_id:
                                _log(f"  OCR: {fname} → 用户={ocr.user_id}, 未提取到单价")
                    except Exception as e:
                        log.error("OCR 失败 %s: %s", fname, e)

                filter_suffix = f"（关键词过滤跳过 {skipped_by_filter} 张）" if skipped_by_filter else ""
                _log(f"[单价] OCR 识别 {ocr_count} 张图片，有效 {len(all_ocr)} 条，写入数据库…{filter_suffix}")
                # 获取已知 user_id 用于修正 OCR 提取结果
                known_user_ids = set()
                try:
                    for m in db.get_meters():
                        uid = m.get("user_id")
                        if uid:
                            known_user_ids.add(uid)
                except Exception:
                    pass

                from src.pipeline import Pipeline
                for ocr in all_ocr:
                    if not ocr.reading_month or not ocr.has_price_data():
                        continue
                    user_id = ocr.user_id
                    if not user_id:
                        log.warning("单价无 user_id，跳过: src=%s", ocr.source_file)
                        continue
                    # 修正 user_id
                    if user_id not in known_user_ids and known_user_ids:
                        matched = Pipeline._match_user_id(user_id, known_user_ids)
                        if matched:
                            log.info("单价 user_id 修正: %s -> %s", user_id, matched)
                            user_id = matched
                        else:
                            log.warning("单价 user_id 无法匹配: %s, src=%s",
                                        user_id, ocr.source_file)
                            continue
                    try:
                        db.upsert_price(
                            user_id=user_id,
                            reading_month=ocr.reading_month,
                            sharp_peak_price=ocr.sharp_peak_price,
                            peak_price=ocr.peak_price,
                            flat_price=ocr.flat_price,
                            valley_price=ocr.valley_price,
                            average_price=ocr.average_price,
                            source_file=ocr.source_file,
                        )
                        price_saved += 1
                    except Exception as e:
                        log.error("单价入库失败: %s", e)

            _log(f"[{task_label}] {mode_label}更新完成！"
                 f"抄表 {readings_added} 条，单价 {price_saved} 条"
                 f"（扫描 {len(source_files)} 个文件，OCR {ocr_count} 张，新邮件 {new_email_count} 个）")
            status["result"] = {
                "readings_added": readings_added,
                "prices_added": price_saved,
                "new_emails": new_email_count,
                "files_scanned": len(source_files),
                "ocr_images": ocr_count,
            }
            status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            log.error("账单更新失败: %s", e, exc_info=True)
            _log(f"错误: {e}")
            status["result"] = {"error": str(e)}

    def _start_bill_update(clear_first: bool, task_type: str = "all"):
        """启动账单更新后台任务。"""
        lock = app.config["BILL_REFRESH_LOCK"]
        status = app.config["BILL_REFRESH_STATUS"]

        if not lock.acquire(blocking=False):
            return jsonify({"error": "账单更新任务正在执行中"}), 409

        mode = "全量" if clear_first else "增量"
        task_labels = {"readings": "抄表数据", "prices": "单价提取", "all": "全部"}
        task_label = task_labels.get(task_type, task_type)
        status["running"] = True
        status["progress"] = f"正在启动{task_label}{mode}更新…"
        status["result"] = None
        status["logs"] = []
        status["started_at"] = datetime.now().strftime("%H:%M:%S")

        def _worker():
            try:
                _do_bill_update(clear_first, task_type=task_type)
            finally:
                status["running"] = False
                lock.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return jsonify({"success": True, "message": f"{task_label}{mode}更新已启动"})

    @app.route("/api/bills/readings/incremental", methods=["POST"])
    def bill_readings_incremental():
        """抄表数据增量更新。"""
        return _start_bill_update(clear_first=False, task_type="readings")

    @app.route("/api/bills/readings/full", methods=["POST"])
    def bill_readings_full():
        """抄表数据全量更新：清空 monthly_readings 后重新提取。"""
        return _start_bill_update(clear_first=True, task_type="readings")

    @app.route("/api/bills/prices/incremental", methods=["POST"])
    def bill_prices_incremental():
        """单价提取增量更新。"""
        return _start_bill_update(clear_first=False, task_type="prices")

    @app.route("/api/bills/prices/full", methods=["POST"])
    def bill_prices_full():
        """单价提取全量更新：清空 price_records 后重新提取。"""
        return _start_bill_update(clear_first=True, task_type="prices")

    def _do_settlement_update(clear_first: bool):
        """电费结算单：从结算单文件提取上网原电价 (grid_average_price)。

        从邮件附件中提取三类核心数据：
          1. 电厂（交易对象）编号 → user_id
          2. 购电月份 → reading_month（文件月份+1）
          3. 电价 → settlement_price（电价栏最后值）

        clear_first: True = 全量（清空现有值重新提取），False = 增量（只补空）
        """
        status = app.config["BILL_REFRESH_STATUS"]

        def _log(msg):
            ts = datetime.now().strftime("%H:%M:%S")
            status["logs"].append(f"[{ts}] {msg}")
            status["progress"] = msg

        mode_label = "全量" if clear_first else "增量"
        _log(f"[电费结算单] {mode_label}提取开始…")

        try:
            from src.pipeline import Pipeline
            import re, os

            # 获取所有已知 user_id（同时索引 meter_number → user_id）
            known_users = {}
            meter_to_user = {}
            for m in db.get_meters():
                uid = m.get("user_id")
                pname = m.get("project_name")
                mnum = m.get("meter_number")
                if uid:
                    known_users[uid] = pname or ""
                if mnum:
                    meter_to_user[mnum] = {"user_id": uid, "project_name": pname or ""}

            from bill_settlement_tool import BillSettlementTool
            bst = BillSettlementTool(dpi=300)

            # ---- 全量模式清空 ----
            total_processed = 0
            skipped = 0

            if clear_first:
                with db.connection() as conn:
                    n = conn.execute(
                        "UPDATE price_records SET "
                        "grid_sharp_peak_price = NULL, grid_peak_price = NULL, "
                        "grid_flat_price = NULL, grid_valley_price = NULL, "
                        "grid_average_price = NULL, settlement_source_file = NULL "
                        "WHERE grid_average_price IS NOT NULL"
                    ).rowcount
                    _log(f"已清空 {n} 条上网电价记录")

            # ---- 扫描 + 过滤 + 提取（与测试一致：filter_bill_files + extract_file） ----
            temp_dir = Path(config.get("attachments", "temp_dir",
                                       default="output/temp_attachments"))

            files = bst.filter_bill_files(str(temp_dir)) if temp_dir.exists() else []
            _log(f"过滤+去重后电费结算单文件: {len(files)} 个")

            total_files = len(files)
            for fi, fpath_str in enumerate(files, 1):
                fname = os.path.basename(fpath_str)
                _log(f"[{fi}/{total_files}] OCR提取: {fname}")
                try:
                    recs = bst.extract_file(fpath_str)
                    if not recs:
                        skipped += 1
                        continue

                    file_records = 0
                    for rec in recs:
                        if not rec.bill_id or not rec.month or rec.price == 0.0:
                            continue
                        meter_id = rec.bill_id
                        y, m = rec.month
                        bill_month = f"{y}-{m:02d}"
                        settlement_price = rec.price

                        # 匹配已知 user_id
                        user_id = meter_id
                        if user_id not in known_users:
                            # 先试 meter_number → user_id 映射
                            if meter_id in meter_to_user:
                                mapped = meter_to_user[meter_id]
                                user_id = mapped["user_id"]
                            else:
                                matched = Pipeline._match_user_id(
                                    user_id, list(known_users.keys())
                                )
                                if matched:
                                    user_id = matched

                        # 月份+1：文件月份 → 抄表月份
                        y, m = bill_month.split("-")
                        m_int = int(m) + 1
                        if m_int > 12:
                            m_int = 1
                            y = str(int(y) + 1)
                        reading_month = f"{y}-{str(m_int).zfill(2)}"

                        # ---- 增量模式：跳过已有 ----
                        if not clear_first:
                            with db.connection() as conn:
                                existing = conn.execute(
                                    "SELECT grid_average_price FROM price_records "
                                    "WHERE user_id = ? AND reading_month = ?",
                                    (user_id, reading_month),
                                ).fetchone()
                                if existing and existing["grid_average_price"] is not None:
                                    skipped += 1
                                    continue

                        # ---- 存入数据库（只写上网电价，不动分时电价） ----
                        db.upsert_price(
                            user_id=user_id,
                            reading_month=reading_month,
                            grid_average_price=settlement_price,
                            settlement_source_file=fpath_str,
                        )
                        total_processed += 1
                        file_records += 1
                        _log(f"  ✓ [{file_records}] {fname} → "
                             f"用户={user_id}, 月份={bill_month}→{reading_month}, "
                             f"电价={settlement_price:.8f}")

                    if file_records == 0:
                        skipped += 1

                except Exception as e:
                    log.error("结算单处理失败 [%s]: %s", fname, e)
                    skipped += 1

            _log(f"[电费结算单] {mode_label}提取完成！"
                 f"处理 {total_processed} 条，跳过 {skipped} 条"
                 f"（共 {total_files} 个文件）")
            status["result"] = {
                "processed": total_processed,
                "skipped": skipped,
                "files_scanned": total_files,
            }
            status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            log.error("电费结算单提取失败: %s", e, exc_info=True)
            _log(f"错误: {e}")
            status["result"] = {"error": str(e)}

    def _start_settlement_update(clear_first: bool):
        """启动电费结算单提取后台任务。"""
        lock = app.config["BILL_REFRESH_LOCK"]
        status = app.config["BILL_REFRESH_STATUS"]

        if not lock.acquire(blocking=False):
            return jsonify({"error": "任务正在执行中"}), 409

        mode = "全量" if clear_first else "增量"
        status["running"] = True
        status["progress"] = f"正在启动电费结算单{mode}提取…"
        status["result"] = None
        status["logs"] = []
        status["started_at"] = datetime.now().strftime("%H:%M:%S")

        def _worker():
            try:
                _do_settlement_update(clear_first)
            finally:
                status["running"] = False
                lock.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return jsonify({"success": True, "message": f"电费结算单{mode}提取已启动"})

    @app.route("/api/bills/settlement/candidates", methods=["POST"])
    def settlement_candidates():
        """扫描附件目录，按文件名筛选出结算单候选文件列表（不做 OCR 加速响应）。"""
        try:
            import re
            from pathlib import Path
            data = request.get_json(silent=True) or {}
            filter_user_id = data.get("user_id", "")
            filter_month = data.get("reading_month", "")

            temp_dir = Path(config.get("attachments", "temp_dir",
                                       default="output/temp_attachments"))
            IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"}
            candidates = []
            for fpath in sorted(temp_dir.rglob("*")) if temp_dir.exists() else []:
                if not fpath.is_file() or fpath.name.startswith("~$"):
                    continue
                ext = fpath.suffix.lower()
                if ext != ".pdf" and ext not in IMAGE_EXTS:
                    continue
                fname = fpath.name
                if not any(kw in fname for kw in ("结算单", "核算单", "电费单")):
                    continue
                # 从文件名/目录推断 user_id 和月份作为预览信息
                user_id = ""
                m = re.search(r'(\d{8,16})', fname)
                if m:
                    user_id = m.group(1)
                if not user_id:
                    m = re.search(r'(\d{8,16})', str(fpath.parent))
                    if m:
                        user_id = m.group(1)
                reading_month = ""
                m = re.search(r'(\d{4})[-_]?(\d{2})', fname)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        reading_month = f"{y}-{str(mo).zfill(2)}"
                if not reading_month:
                    m = re.search(r'(\d{4})[-_]?(\d{2})', str(fpath.parent))
                    if m:
                        y, mo = int(m.group(1)), int(m.group(2))
                        if 2015 <= y <= 2035 and 1 <= mo <= 12:
                            reading_month = f"{y}-{str(mo).zfill(2)}"
                if filter_user_id and user_id != filter_user_id:
                    continue
                if filter_month:
                    if reading_month:
                        parts = reading_month.split("-")
                        m_int = int(parts[1]) + 1
                        y = parts[0]
                        if m_int > 12:
                            m_int = 1
                            y = str(int(y) + 1)
                        cal_month = f"{y}-{str(m_int).zfill(2)}"
                        if cal_month != filter_month:
                            continue
                    else:
                        continue
                candidates.append({
                    "filepath": str(fpath),
                    "filename": fname,
                    "user_id": user_id or "",
                    "reading_month": reading_month or "",
                    "settlement_price": None,
                })

            return jsonify({"candidates": candidates, "total": len(candidates)})
        except Exception as e:
            log.error("获取结算单候选文件失败: %s", e, exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bills/settlement/extract-selected", methods=["POST"])
    def settlement_extract_selected():
        """对用户选中的文件执行结算单提取并保存。"""
        data = request.get_json(silent=True)
        if not data or "files" not in data:
            return jsonify({"error": "未选择文件"}), 400

        selected = data["files"]
        target_user_id = data.get("user_id", "")
        target_month = data.get("reading_month", "")
        clear_first = data.get("clear_first", False)
        results = {"processed": 0, "failed": 0, "skipped": 0, "detail": ""}

        if clear_first:
            with db.connection() as conn:
                if target_user_id and target_month:
                    conn.execute(
                        "UPDATE price_records SET "
                        "grid_sharp_peak_price = NULL, grid_peak_price = NULL, "
                        "grid_flat_price = NULL, grid_valley_price = NULL, "
                        "grid_average_price = NULL, settlement_source_file = NULL "
                        "WHERE user_id = ? AND reading_month = ?",
                        (target_user_id, target_month),
                    )
                else:
                    conn.execute(
                        "UPDATE price_records SET "
                        "grid_sharp_peak_price = NULL, grid_peak_price = NULL, "
                        "grid_flat_price = NULL, grid_valley_price = NULL, "
                        "grid_average_price = NULL, settlement_source_file = NULL "
                        "WHERE grid_average_price IS NOT NULL"
                    )
        try:
            import os
            os.environ["PATH"] = "/usr/bin:" + os.environ.get("PATH", "")
            from src.pipeline import Pipeline

            known_users = {}
            for m in db.get_meters():
                uid = m.get("user_id")
                pname = m.get("project_name")
                if uid:
                    known_users[uid] = pname or ""

            from bill_settlement_tool import BillSettlementTool
            bst = BillSettlementTool(dpi=300)

            for fpath_str in selected:
                fpath = Path(fpath_str)
                if not fpath.exists():
                    results["failed"] += 1
                    continue
                fname = fpath.name
                try:
                    recs = bst.extract_file(str(fpath))
                except Exception:
                    results["failed"] += 1
                    continue

                if not recs:
                    results["skipped"] += 1
                    continue

                file_ok = False
                for rec in recs:
                    if not rec.bill_id or not rec.month or rec.price == 0.0:
                        continue
                    meter_id = rec.bill_id
                    y, m = rec.month
                    bill_month = f"{y}-{m:02d}"
                    settlement_price = rec.price

                    user_id = meter_id
                    if not user_id:
                        dir_name = str(fpath.parent)
                        for uid, pname in known_users.items():
                            if (pname and pname in dir_name) or uid in dir_name:
                                user_id = uid
                                break
                    if not user_id:
                        continue

                    if target_user_id and user_id != target_user_id:
                        if user_id in known_users:
                            matched = Pipeline._match_user_id(
                                user_id, [target_user_id]
                            )
                            if not matched:
                                continue
                        else:
                            continue
                    if user_id not in known_users:
                        matched = Pipeline._match_user_id(
                            user_id, list(known_users.keys())
                        )
                        if matched:
                            user_id = matched
                        else:
                            continue

                    reading_month = bill_month
                    if not reading_month:
                        m = re.search(r'(\d{4})[-_]?(\d{2})', fname)
                        if m:
                            y, mo = int(m.group(1)), int(m.group(2))
                            if 2015 <= y <= 2035 and 1 <= mo <= 12:
                                reading_month = f"{y}-{str(mo).zfill(2)}"
                        if not reading_month:
                            m = re.search(r'(\d{4})[-_]?(\d{2})', str(fpath.parent))
                            if m:
                                y, mo = int(m.group(1)), int(m.group(2))
                                if 2015 <= y <= 2035 and 1 <= mo <= 12:
                                    reading_month = f"{y}-{str(mo).zfill(2)}"
                    if not reading_month:
                        continue

                    y, m = reading_month.split("-")
                    m_int = int(m) + 1
                    if m_int > 12:
                        m_int = 1
                        y = str(int(y) + 1)
                    reading_month = f"{y}-{str(m_int).zfill(2)}"

                    if target_month and reading_month != target_month:
                        continue

                    db.upsert_price(
                        user_id=user_id,
                        reading_month=reading_month,
                        grid_average_price=settlement_price,
                        settlement_source_file=fname,
                    )
                    results["processed"] += 1
                    file_ok = True

                if not file_ok:
                    results["skipped"] += 1

            return jsonify(results)
        except Exception as e:
            log.error("结算单提取失败: %s", e, exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bills/settlement/incremental", methods=["POST"])
    def bill_settlement_incremental():
        """电费结算单增量提取。"""
        return _start_settlement_update(clear_first=False)

    @app.route("/api/bills/settlement/full", methods=["POST"])
    def bill_settlement_full():
        """电费结算单全量提取。"""
        return _start_settlement_update(clear_first=True)

    # ---- 账单更新状态轮询 API ----
    @app.route("/api/bill-refresh-status")
    def bill_refresh_status_api():
        return jsonify(app.config["BILL_REFRESH_STATUS"])

    # ---- 抄表数据查看/编辑 ----
    @app.route("/readings")
    def readings():
        sel_project = request.args.get("project", "")
        sel_user_id = request.args.get("user_id", "")
        sel_month = request.args.get("month", "")         # 服务端过滤
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 10))

        import re

        # 先统计 (用户, 月份) 组合总数（SQL COUNT）
        total_items = db.count_user_months(
            project_name=sel_project or None,
            user_id=sel_user_id or None,
            reading_month=sel_month or None,
        )
        total_pages = max(1, (total_items + per_page - 1) // per_page)
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        # 查当前页数据（SQL 层按 (user_id, reading_month) 对分页）
        raw = db.get_readings_grouped(
            project_name=sel_project or None,
            user_id=sel_user_id or None,
            reading_month=sel_month or None,
            limit=per_page,
            offset=(page - 1) * per_page,
        )

        # 收集所有出现的月份，计算上月列表，用于回填 prev/cur 表数
        months_in_data = set()
        for r in raw:
            months_in_data.add(r["reading_month"])

        prev_months_needed = set()
        for m in months_in_data:
            match = re.match(r'(\d{4})-(\d{2})', m)
            if match:
                y, mo = int(match.group(1)), int(match.group(2))
                pm = f"{y-1}-12" if mo == 1 else f"{y}-{str(mo-1).zfill(2)}"
                prev_months_needed.add(pm)

        # 只查询尚未包含在 raw 中的上月数据
        prev_months_to_fetch = prev_months_needed - months_in_data
        prev_by_meter = {}  # meter_id -> {month -> reading}
        for pm in prev_months_to_fetch:
            prev_raw = db.get_readings_grouped(
                project_name=sel_project or None,
                user_id=sel_user_id or None,
                reading_month=pm,
            )
            for pr in prev_raw:
                prev_by_meter.setdefault(pr["meter_id"], {})[pm] = pr

        # 也把 raw 中的数据加入 prev_by_meter，以便跨月引用
        for r in raw:
            prev_by_meter.setdefault(r["meter_id"], {})[r["reading_month"]] = r

        def _calc_prev_month(month_str):
            match = re.match(r'(\d{4})-(\d{2})', month_str)
            if not match:
                return None
            y, mo = int(match.group(1)), int(match.group(2))
            return f"{y-1}-12" if mo == 1 else f"{y}-{str(mo-1).zfill(2)}"

        # 构建 按用户→按月→发电表/上网表 的分组结构
        from collections import OrderedDict
        user_groups = OrderedDict()
        for r in raw:
            uid = r["user_id"] or "unknown"
            if uid not in user_groups:
                user_groups[uid] = {"user_id": uid, "project_name": r["project_name"], "months_dict": OrderedDict()}
            md = user_groups[uid]["months_dict"]
            month = r["reading_month"]
            if month not in md:
                md[month] = {"month": month, "gen": None, "grid": None,
                             "prev_gen": None, "prev_grid": None}
            if r["meter_type"] == "发电表":
                md[month]["gen"] = r
            elif r["meter_type"] == "上网表":
                md[month]["grid"] = r

        grouped_data = []
        for uid, gdata in user_groups.items():
            grouped_data.append({
                "user_id": uid,
                "project_name": gdata["project_name"],
                "months": list(gdata["months_dict"].values()),
            })

        # 附加上月数据（用于电费单计算）
        for g in grouped_data:
            for mdata in g["months"]:
                pm = _calc_prev_month(mdata["month"])
                if pm:
                    if mdata["gen"]:
                        mid = mdata["gen"]["meter_id"]
                        mdata["prev_gen"] = prev_by_meter.get(mid, {}).get(pm)
                    if mdata["grid"]:
                        mid = mdata["grid"]["meter_id"]
                        mdata["prev_grid"] = prev_by_meter.get(mid, {}).get(pm)

        # 分页链接的基础 URL（不含 page 参数）
        pagination_base = url_for("readings",
                                  project=sel_project or None,
                                  user_id=sel_user_id or None,
                                  month=sel_month or None,
                                  per_page=per_page if per_page != 10 else None)

        return render_template("readings.html",
                               grouped_data=grouped_data,
                               projects=db.get_project_names(),
                               user_ids=db.get_user_ids(project_name=sel_project or None),
                               months=db.get_months(),
                               sel_project=sel_project,
                               sel_user_id=sel_user_id,
                               sel_month=sel_month,
                               page=page, total_pages=total_pages,
                               per_page=per_page, total_items=total_items,
                               pagination_base=pagination_base)

    # ---- 手动创建抄表记录 ----
    @app.route("/readings/create", methods=["GET", "POST"])
    def reading_create():
        if request.method == "POST":
            meter_id = request.form.get("meter_id", "").strip()
            reading_month = request.form.get("reading_month", "").strip()

            if not meter_id or not reading_month:
                flash("请选择电表并填写月份", "danger")
                return redirect(url_for("reading_create"))

            # 验证月份格式
            import re as _re
            if not _re.match(r'^\d{4}-\d{2}$', reading_month):
                flash("月份格式应为 YYYY-MM", "danger")
                return redirect(url_for("reading_create"))

            # 验证电表存在
            with db.connection() as conn:
                meter = conn.execute("SELECT id FROM meters WHERE id = ?", (int(meter_id),)).fetchone()
                if not meter:
                    flash("所选电表不存在", "danger")
                    return redirect(url_for("reading_create"))

            def _float_or_none(key):
                v = request.form.get(key, "").strip()
                return float(v) if v else None

            try:
                db.upsert_reading(
                    meter_id=int(meter_id),
                    reading_month=reading_month,
                    sharp_peak=_float_or_none("sharp_peak"),
                    peak=_float_or_none("peak"),
                    flat=_float_or_none("flat"),
                    valley=_float_or_none("valley"),
                    total_kwh=_float_or_none("total_kwh"),
                    rev_sharp_peak=_float_or_none("rev_sharp_peak"),
                    rev_peak=_float_or_none("rev_peak"),
                    rev_flat=_float_or_none("rev_flat"),
                    rev_valley=_float_or_none("rev_valley"),
                    rev_total=_float_or_none("rev_total"),
                    cur_sharp_peak=_float_or_none("cur_sharp_peak"),
                    cur_peak=_float_or_none("cur_peak"),
                    cur_flat=_float_or_none("cur_flat"),
                    cur_valley=_float_or_none("cur_valley"),
                    cur_total=_float_or_none("cur_total"),
                    source_file="手动创建",
                )
                flash(f"抄表记录创建成功（月份: {reading_month}）", "success")
                return redirect(url_for("readings", month=reading_month))
            except Exception as e:
                flash(f"创建失败: {e}", "danger")
                return redirect(url_for("reading_create"))

        meters = db.get_meters()
        return render_template("reading_create.html", meters=meters)

    @app.route("/api/readings/batch-update", methods=["POST"])
    def readings_batch_update():
        """批量更新抄表数据。"""
        data = request.get_json()
        if not data or "updates" not in data:
            return jsonify({"ok": False, "error": "缺少 updates 参数"})
        try:
            for item in data["updates"]:
                meter_id = item["meter_id"]
                month = item["month"]
                fields = item.get("fields", {})
                # 检查是否锁定
                with db.connection() as conn:
                    row = conn.execute(
                        "SELECT is_locked FROM monthly_readings WHERE meter_id = ? AND reading_month = ?",
                        (meter_id, month)).fetchone()
                    if row and row["is_locked"]:
                        continue  # 跳过锁定记录
                db.update_reading(meter_id, month, **fields)
            return jsonify({"ok": True})
        except Exception as e:
            log.error("批量更新抄表数据失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/readings/batch-lock", methods=["POST"])
    def readings_batch_lock():
        """按用户+月份批量锁定/解锁抄表数据和单价数据。"""
        data = request.get_json()
        user_id = data.get("user_id")
        month = data.get("month")
        locked = data.get("locked", True)
        if not user_id or not month:
            return jsonify({"ok": False, "error": "缺少 user_id 或 month"})
        try:
            with db.connection() as conn:
                # 锁定/解锁抄表数据
                conn.execute("""
                    UPDATE monthly_readings SET is_locked = ?
                    WHERE reading_month = ? AND meter_id IN (
                        SELECT id FROM meters WHERE user_id = ?
                    )
                """, (1 if locked else 0, month, user_id))
                # 同时锁定/解锁单价数据
                conn.execute("""
                    UPDATE price_records SET is_locked = ?
                    WHERE user_id = ? AND reading_month = ?
                """, (1 if locked else 0, user_id, month))
            return jsonify({"ok": True})
        except Exception as e:
            log.error("批量锁定失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/prices/lock", methods=["POST"])
    def prices_lock():
        """单独锁定/解锁单价数据。"""
        data = request.get_json()
        user_id = data.get("user_id")
        month = data.get("month")
        locked = data.get("locked", True)
        if not user_id or not month:
            return jsonify({"ok": False, "error": "缺少 user_id 或 month"})
        try:
            db.lock_price(user_id, month, locked)
            return jsonify({"ok": True})
        except Exception as e:
            log.error("单价锁定失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/settlement/lock", methods=["POST"])
    def settlement_lock():
        """单独锁定/解锁电费结算单数据（独立于单价锁定）。"""
        data = request.get_json()
        user_id = data.get("user_id")
        month = data.get("month")
        locked = data.get("locked", True)
        if not user_id or not month:
            return jsonify({"ok": False, "error": "缺少 user_id 或 month"})
        try:
            db.lock_settlement_price(user_id, month, locked)
            return jsonify({"ok": True})
        except Exception as e:
            log.error("结算单锁定失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/files/images")
    def files_images():
        """列出所有附件文件（图片+PDF），按邮件（目录）分组返回，支持 q 参数关键词过滤。"""
        keyword = request.args.get("q", "")
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        archive_root = Path(config.get("storage", "archive_root", default="output/archive"))
        SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp", ".pdf"}
        from collections import OrderedDict
        groups = OrderedDict()
        seen = set()
        for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
            if not scan_root.exists():
                continue
            for f in sorted(scan_root.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                if f.name.startswith("~$"):
                    continue
                if keyword:
                    path_str = str(f.relative_to(scan_root))
                    if keyword.lower() not in path_str.lower():
                        continue
                rel = f.relative_to(scan_root)
                key = f"{prefix}/{rel}"
                if key in seen:
                    continue
                seen.add(key)
                ext = f.suffix.lower()
                file_type = "pdf" if ext == ".pdf" else "image"
                top_dir = rel.parts[0] if len(rel.parts) > 1 else ("其他" if prefix == "temp" else "归档")
                group_key = f"{prefix}:{top_dir}"
                if group_key not in groups:
                    groups[group_key] = {"name": top_dir, "source": prefix, "files": []}
                entry = {
                    "filename": f.name,
                    "path": f"{prefix}/{rel}",
                    "dir": str(rel.parent),
                    "file_type": file_type,
                }
                if file_type == "image":
                    entry["url"] = url_for("file_image", source=prefix, filepath=str(rel))
                groups[group_key]["files"].append(entry)
        all_files = []
        grouped = []
        for gk, gv in groups.items():
            grouped.append(gv)
            all_files.extend(gv["files"])
        return jsonify({"files": all_files, "groups": grouped, "total": len(all_files)})

    @app.route("/api/readings/ocr-extract", methods=["POST"])
    def readings_ocr_extract():
        """手动选择文件 OCR 提取单价/结算单，用户编号由前端指定（人工确认）。

        target: 'price'（默认）提取尖峰平谷均价，'settlement' 提取上网电价。
        支持图片和 PDF 文件。
        """
        data = request.get_json()
        image_path = data.get("image_path", "")  # 格式: temp/xxx 或 archive/xxx
        user_id = data.get("user_id", "")
        month = data.get("month", "")
        target = data.get("target", "price")
        if not image_path or not user_id or not month:
            return jsonify({"ok": False, "error": "缺少 image_path / user_id / month"})

        # 解析 source/filepath 格式
        parts = image_path.split("/", 1)
        if len(parts) == 2 and parts[0] in ("temp", "archive"):
            source, rel_path = parts
            if source == "temp":
                root = Path(config.get("attachments", "temp_dir",
                                       default="output/temp_attachments"))
            else:
                root = Path(config.get("storage", "archive_root", default="output/archive"))
            full_path = root / rel_path
        else:
            # 兼容旧格式（纯 archive 相对路径）
            archive_root = Path(config.get("storage", "archive_root", default="output/archive"))
            full_path = archive_root / image_path
        if not full_path.exists():
            return jsonify({"ok": False, "error": "文件不存在"})

        try:
            from src.pipeline import SmartDispatcher
            dispatcher = SmartDispatcher()
            ext = full_path.suffix.lower()
            if ext == ".pdf":
                ocr = dispatcher.ocr_engine.extract_from_pdf(
                    str(full_path), {"filename": full_path.name, "archive_month": month})
            else:
                ocr = dispatcher.ocr_engine.extract_from_image(
                    str(full_path), {"filename": full_path.name, "archive_month": month})

            if not ocr.has_any_data():
                return jsonify({"ok": False, "error": "OCR 未识别到任何数据",
                                "raw_text": ocr.raw_text[:500] if ocr.raw_text else ""})

            if target == "settlement":
                # 结算单提取：保存上网电价（全部 grid_ 字段填入同一值）
                db.upsert_price(
                    user_id=user_id,
                    reading_month=month,
                    grid_sharp_peak_price=ocr.settlement_price,
                    grid_peak_price=ocr.settlement_price,
                    grid_flat_price=ocr.settlement_price,
                    grid_valley_price=ocr.settlement_price,
                    grid_average_price=ocr.settlement_price,
                    settlement_source_file=full_path.name,
                )
            else:
                # 单价提取：保存尖峰平谷均价
                if ocr.has_price_data():
                    db.upsert_price(
                        user_id=user_id,
                        reading_month=month,
                        sharp_peak_price=ocr.sharp_peak_price,
                        peak_price=ocr.peak_price,
                        flat_price=ocr.flat_price,
                        valley_price=ocr.valley_price,
                        average_price=ocr.average_price,
                        source_file=full_path.name,
                    )

            # 返回所有提取到的数据
            return jsonify({
                "ok": True,
                "has_prices": ocr.has_price_data(),
                "prices": {
                    "sharp_peak_price": ocr.sharp_peak_price,
                    "peak_price": ocr.peak_price,
                    "flat_price": ocr.flat_price,
                    "valley_price": ocr.valley_price,
                    "average_price": ocr.average_price,
                },
                "settlement_price": ocr.settlement_price,
                "target": target,
                "ocr_user_id": ocr.user_id,
                "reading_month": ocr.reading_month,
                "meter_records": ocr.meter_records or [],
                "source_file": full_path.name,
                "raw_text": ocr.raw_text[:1000] if ocr.raw_text else "",
            })
        except Exception as e:
            log.error("手动 OCR 提取失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/readings/update-price", methods=["POST"])
    def readings_update_price():
        """手动更新单价数据。"""
        data = request.get_json()
        user_id = data.get("user_id", "")
        month = data.get("month", "")
        prices = data.get("prices", {})
        if not user_id or not month:
            return jsonify({"ok": False, "error": "缺少 user_id / month"})
        try:
            db.upsert_price(
                user_id=user_id,
                reading_month=month,
                sharp_peak_price=prices.get("sharp_peak_price"),
                peak_price=prices.get("peak_price"),
                flat_price=prices.get("flat_price"),
                valley_price=prices.get("valley_price"),
                average_price=prices.get("average_price"),
                grid_sharp_peak_price=prices.get("grid_sharp_peak_price"),
                grid_peak_price=prices.get("grid_peak_price"),
                grid_flat_price=prices.get("grid_flat_price"),
                grid_valley_price=prices.get("grid_valley_price"),
                grid_average_price=prices.get("grid_average_price"),
            )
            return jsonify({"ok": True})
        except Exception as e:
            log.error("更新单价失败: %s", e)
            return jsonify({"ok": False, "error": str(e)})

    # ---- 邮件文件查看 ----
    @app.route("/email-files")
    def email_files():
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"}
        email_groups = []
        if temp_dir.exists():
            # 顶层目录 = 邮件（日期_主题），子目录 = 附件/解压包
            for entry in sorted(temp_dir.iterdir(), reverse=True):
                if entry.is_dir() and not entry.name.startswith("."):
                    files = []
                    for item in sorted(entry.rglob("*")):
                        if item.is_file() and not item.name.startswith("~$"):
                            rel = item.relative_to(temp_dir)
                            ext = item.suffix.lower()
                            files.append({
                                "name": item.name,
                                "path": str(rel),
                                "size_kb": round(item.stat().st_size / 1024, 1),
                                "subfolder": str(item.parent.relative_to(entry)) if item.parent != entry else "",
                                "is_image": ext in IMAGE_EXTS,
                                "is_excel": ext in {".xlsx", ".xls", ".csv"},
                                "ext": ext,
                            })
                    email_groups.append({
                        "name": entry.name,
                        "files": files,
                        "file_count": len(files),
                        "image_count": sum(1 for f in files if f["is_image"]),
                    })
            # 顶层散文件
            loose_files = []
            for item in sorted(temp_dir.iterdir()):
                if item.is_file() and not item.name.startswith("~$"):
                    ext = item.suffix.lower()
                    loose_files.append({
                        "name": item.name,
                        "path": item.name,
                        "size_kb": round(item.stat().st_size / 1024, 1),
                        "subfolder": "",
                        "is_image": ext in IMAGE_EXTS,
                        "is_excel": ext in {".xlsx", ".xls", ".csv"},
                        "ext": ext,
                    })
            if loose_files:
                email_groups.append({
                    "name": "其他文件",
                    "files": loose_files,
                    "file_count": len(loose_files),
                    "image_count": sum(1 for f in loose_files if f["is_image"]),
                })
        return render_template("email_files.html", email_groups=email_groups)

    @app.route("/email-files/view/<path:filepath>")
    def email_file_view(filepath):
        """内联查看邮件附件文件。"""
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        full_path = temp_dir / filepath
        if not full_path.exists() or not full_path.is_file():
            abort(404)
        try:
            full_path.resolve().relative_to(temp_dir.resolve())
        except ValueError:
            abort(403)
        return send_from_directory(str(full_path.parent), full_path.name)

    @app.route("/email-files/download/<path:filepath>")
    def email_file_download(filepath):
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        full_path = temp_dir / filepath
        if not full_path.exists() or not full_path.is_file():
            abort(404)
        try:
            full_path.resolve().relative_to(temp_dir.resolve())
        except ValueError:
            abort(403)
        return send_from_directory(str(full_path.parent), full_path.name,
                                  as_attachment=True)

    # ---- 兼容旧归档路由 ----
    @app.route("/archive")
    def archive_index():
        return redirect(url_for("email_files"))

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

    # ---- 文件内联显示（支持 temp_attachments 和 archive） ----
    @app.route("/files/image/<source>/<path:filepath>")
    def file_image(source, filepath):
        if source == "temp":
            root = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        elif source == "archive":
            root = Path(config.get("storage", "archive_root",
                                   default="output/archive"))
        else:
            abort(400)
        full_path = root / filepath
        if not full_path.exists() or not full_path.is_file():
            abort(404)
        try:
            full_path.resolve().relative_to(root.resolve())
        except ValueError:
            abort(403)
        return send_from_directory(str(full_path.parent), full_path.name)

    # ---- 兼容旧路径：归档图片内联显示 ----
    @app.route("/archive/image/<path:filepath>")
    def archive_image(filepath):
        return file_image("archive", filepath)

    @app.route("/api/files/find-image/<filename>")
    def find_image(filename):
        """根据文件名在邮件附件和归档目录中查找图片，返回内联显示 URL。"""
        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        archive_root = Path(config.get("storage", "archive_root", default="output/archive"))
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"}
        # 精确 → 模糊，优先 temp
        for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
            if not scan_root.exists():
                continue
            for f in scan_root.rglob("*"):
                if f.is_file() and f.name == filename and f.suffix.lower() in IMAGE_EXTS:
                    rel = f.relative_to(scan_root)
                    return jsonify({"found": True, "url": url_for("file_image", source=prefix, filepath=str(rel))})
        stem = Path(filename).stem
        for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
            if not scan_root.exists():
                continue
            for f in scan_root.rglob("*"):
                if f.is_file() and stem in f.stem and f.suffix.lower() in IMAGE_EXTS:
                    rel = f.relative_to(scan_root)
                    return jsonify({"found": True, "url": url_for("file_image", source=prefix, filepath=str(rel))})
        return jsonify({"found": False})

    # 兼容旧 API
    @app.route("/api/archive/find-image/<filename>")
    def find_archive_image(filename):
        return find_image(filename)

    @app.route("/api/archive/preview/<path:filename>")
    @app.route("/api/archive/preview", endpoint="archive_file_preview_query")
    def archive_file_preview(filename=None):
        """预览文件：图片返回 URL，Excel/PDF 转 HTML 表格。搜索 temp 和 archive。"""
        # 支持 query parameter（避免路径中含 / 和中文的编码问题）
        if filename is None:
            filename = request.args.get("filename", "").strip()
        if not filename:
            return jsonify({"found": False})

        temp_dir = Path(config.get("attachments", "temp_dir",
                                   default="output/temp_attachments"))
        archive_root = Path(config.get("storage", "archive_root",
                                       default="output/archive"))
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"}
        EXCEL_EXTS = {".xlsx", ".xls", ".csv"}

        # 查找文件（优先 temp，再 archive）
        found = None
        found_root = None
        found_prefix = None

        # 1) 如果 filename 含目录分隔符，先按相对路径精确匹配
        if "/" in filename:
            for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
                if not scan_root.exists():
                    continue
                candidate = scan_root / filename
                if candidate.is_file():
                    found, found_root, found_prefix = candidate, scan_root, prefix
                    break

        # 2) 按文件名精确匹配
        bare_name = Path(filename).name
        if not found:
            for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
                if not scan_root.exists():
                    continue
                for f in scan_root.rglob(bare_name):
                    if f.is_file() and not f.name.startswith("~$"):
                        found, found_root, found_prefix = f, scan_root, prefix
                        break
                if found:
                    break

        # 3) 模糊匹配（stem 包含）
        if not found:
            stem = Path(filename).stem
            for scan_root, prefix in [(temp_dir, "temp"), (archive_root, "archive")]:
                if not scan_root.exists():
                    continue
                for f in scan_root.rglob("*"):
                    if f.is_file() and stem in f.stem and not f.name.startswith("~$"):
                        found, found_root, found_prefix = f, scan_root, prefix
                        break
                if found:
                    break

        if not found:
            return jsonify({"found": False})

        rel = found.relative_to(found_root)
        ext = found.suffix.lower()
        download_url = url_for("file_image", source=found_prefix, filepath=str(rel))

        if ext in IMAGE_EXTS:
            return jsonify({
                "found": True,
                "type": "image",
                "url": url_for("file_image", source=found_prefix, filepath=str(rel)),
                "download_url": download_url,
                "filename": found.name,
            })

        if ext in EXCEL_EXTS:
            try:
                import pandas as pd
                if ext == ".csv":
                    df = pd.read_csv(str(found))
                    sheets = {"Sheet1": df}
                else:
                    xls = pd.ExcelFile(str(found))
                    sheets = {}
                    for name in xls.sheet_names[:10]:  # 最多10个sheet
                        sheets[name] = pd.read_excel(xls, sheet_name=name,
                                                     nrows=200, header=None)

                html_parts = []
                for name, df in sheets.items():
                    df = df.fillna("")
                    # 使用 header=None 保留原始行（含合并单元格标题等）
                    table_html = df.to_html(index=False, header=False,
                                            classes="preview-table", border=0)
                    if len(sheets) > 1:
                        html_parts.append(
                            f'<h4 style="margin:1rem 0 0.5rem;color:var(--primary);">{name}</h4>'
                            + table_html)
                    else:
                        html_parts.append(table_html)

                return jsonify({
                    "found": True,
                    "type": "excel",
                    "html": "\n".join(html_parts),
                    "download_url": download_url,
                    "filename": found.name,
                })
            except Exception as e:
                return jsonify({
                    "found": True,
                    "type": "error",
                    "message": f"无法预览: {e}",
                    "download_url": download_url,
                    "filename": found.name,
                })

        if ext == ".pdf":
            return jsonify({
                "found": True,
                "type": "pdf",
                "url": url_for("file_image", source=found_prefix, filepath=str(rel)),
                "download_url": download_url,
                "filename": found.name,
            })

        return jsonify({
            "found": True,
            "type": "unsupported",
            "download_url": download_url,
            "filename": found.name,
        })

    # ---- 已处理邮件记录 ----
    @app.route("/processed")
    def processed_list():
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM processed_emails ORDER BY processed_at DESC"
            ).fetchall()
            records = [dict(r) for r in rows]
        return render_template("processed.html", records=records)

    # ---- 导出校验 ----
    @app.route("/api/export/validate", methods=["GET", "POST"])
    def export_validate():
        """校验数据完整性，返回可导出的月份和不可导出的原因。

        GET: 按筛选条件校验全部
        POST: 校验指定的 user_id+month 列表 (items)
        """
        from collections import defaultdict

        # 获取指定项或全量
        selected_keys = None
        if request.method == "POST":
            data = request.get_json() or {}
            items = data.get("items", [])
            if items:
                selected_keys = {(it["user_id"], it["month"]) for it in items}

        project = request.args.get("project") or None
        user_id = request.args.get("user_id") or None
        month = request.args.get("month") or None

        raw = db.get_readings_grouped(
            project_name=project, user_id=user_id, reading_month=month)

        # 按用户+月份分组检查
        groups = defaultdict(lambda: {"gen": None, "grid": None, "price": False, "user_id": None, "project": None})
        for r in raw:
            key = (r["user_id"], r["reading_month"])
            if selected_keys and key not in selected_keys:
                continue
            g = groups[key]
            g["user_id"] = r["user_id"]
            g["project"] = r["project_name"]
            if r["meter_type"] == "发电表":
                g["gen"] = r
            elif r["meter_type"] == "上网表":
                g["grid"] = r
            if r.get("sharp_peak_price") is not None or r.get("peak_price") is not None:
                g["price"] = True

        exportable = []
        issues = []
        for (uid, month_val), g in sorted(groups.items()):
            gen_ok = g["gen"] and g["gen"]["total_kwh"] is not None
            grid_ok = g["grid"] and g["grid"]["total_kwh"] is not None
            price_ok = g["price"]

            if g["grid"]:
                if gen_ok and grid_ok and price_ok:
                    exportable.append({"user_id": uid, "month": month_val, "project": g["project"]})
                else:
                    reasons = []
                    if not gen_ok:
                        reasons.append("发电表数据不全")
                    if not grid_ok:
                        reasons.append("上网表数据不全")
                    if not price_ok:
                        reasons.append("单价缺失")
                    issues.append({"user_id": uid, "month": month_val, "reasons": reasons})
            else:
                if gen_ok and price_ok:
                    exportable.append({"user_id": uid, "month": month_val, "project": g["project"]})
                else:
                    reasons = []
                    if not gen_ok:
                        reasons.append("发电表数据不全")
                    if not price_ok:
                        reasons.append("单价缺失")
                    issues.append({"user_id": uid, "month": month_val, "reasons": reasons})

        return jsonify({"exportable": len(exportable), "issues": issues, "total": len(groups)})

    # ---- 导出 CSV ----
    @app.route("/export")
    def export_csv():
        db.export_csv()
        csv_dir = config.get("storage", "csv_export_dir", default="output/data")
        flash(f"CSV 已导出到 {csv_dir}", "success")
        return redirect(url_for("index"))

    @app.route("/bills/export-excel", methods=["GET", "POST"])
    def bills_export_excel():
        """导出月度电费单 Excel 文件。

        GET: 按 project/user_id/month 筛选导出全部
        POST: 导出指定的 user_id+month 组合 (items JSON)

        month_is_reading=1 时，month 参数视为抄表月份（读数月份），不做 offset 转换。
        用于 readings.html 的电费单 Tab（显示原始抄表月份）。
        """
        from src.web.bill_export import generate_bill_excel

        selected_items = None
        month_is_reading = False
        if request.method == "POST":
            items_json = request.form.get("items", "")
            if items_json:
                try:
                    selected_items = json.loads(items_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            month_is_reading = request.form.get("month_is_reading") in ("1", "true")
        else:
            month_is_reading = request.args.get("month_is_reading") in ("1", "true")

        project = request.args.get("project") or None
        user_id = request.args.get("user_id") or None
        month = request.args.get("month") or None

        buf = generate_bill_excel(db, project_name=project, user_id=user_id,
                                  month=month, selected_items=selected_items,
                                  month_is_reading=month_is_reading)

        # 文件名: 显示月份为账期月份（抄表月份 - 1）
        parts = []
        if project:
            parts.append(project)
        if month:
            y, m = month.split("-")
            ny = str(int(y) - 1) if m == "01" else y
            nm = "12" if m == "01" else f"{int(m)-1:02d}"
            parts.append(f"{ny}-{nm}")
        elif selected_items:
            months = sorted(set(it["month"] for it in selected_items))
            if len(months) == 1:
                y, m = months[0].split("-")
                ny = str(int(y) - 1) if m == "01" else y
                nm = "12" if m == "01" else f"{int(m)-1:02d}"
                parts.append(f"{ny}-{nm}")
            elif len(months) <= 3:
                display = []
                for mk in months:
                    y, m = mk.split("-")
                    ny = str(int(y) - 1) if m == "01" else y
                    nm = "12" if m == "01" else f"{int(m)-1:02d}"
                    display.append(f"{ny}-{nm}")
                parts.append("_".join(display))
        parts.append("电费单")
        filename = "_".join(parts) + ".xlsx"

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )

    # ---- 新建电表页面 ----
    @app.route("/meters/create", methods=["GET", "POST"])
    def meter_create():
        if request.method == "POST":
            data = request.form.to_dict()
            meter_number = data.get("meter_number", "").strip()
            if not meter_number:
                flash("电表号不能为空", "danger")
                return redirect(url_for("meter_create"))
            try:
                multiplier = float(data.get("multiplier") or 1.0)
                discount = float(data.get("discount") or 1.0)
                pricing_mode = data.get("pricing_mode", "discount").strip()
                raw_param = data.get("pricing_param", "").strip()
                pricing_param = float(raw_param) if raw_param else None
                meter_id = db.create_meter(
                    meter_number=meter_number,
                    user_id=data.get("user_id", "").strip() or None,
                    meter_type=data.get("meter_type", "未知"),
                    asset_number=data.get("asset_number", "").strip() or None,
                    multiplier=multiplier,
                    discount=discount,
                    pricing_mode=pricing_mode,
                    pricing_param=pricing_param,
                    project_name=data.get("project_name", "").strip() or None,
                )
                flash(f"电表 {meter_number} 创建成功", "success")
                return redirect(url_for("meter_detail", meter_id=meter_id))
            except ValueError as e:
                flash(str(e), "danger")
                return redirect(url_for("meter_create"))
            except Exception as e:
                flash(f"创建失败: {e}", "danger")
                return redirect(url_for("meter_create"))

        projects = db.get_project_names()
        return render_template("meter_create.html", projects=projects)

    # ---- 电表编辑页面 ----
    @app.route("/meters/<int:meter_id>/edit")
    def meter_edit(meter_id):
        with db.connection() as conn:
            meter = conn.execute("SELECT * FROM meters WHERE id = ?", (meter_id,)).fetchone()
            if not meter:
                abort(404)
            meter = dict(meter)
        projects = db.get_project_names()
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
            elif k in ("user_id", "meter_type", "asset_number", "project_name", "pricing_mode"):
                fields[k] = str(v).strip() if v else None
            elif k == "pricing_param":
                try:
                    fields[k] = float(v) if v != "" else None
                except (ValueError, TypeError):
                    fields[k] = None

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

    # ---- 电表删除 API ----
    @app.route("/api/meters/<meter_number>/delete", methods=["POST"])
    def meter_delete_api(meter_number):
        meter = db.get_meter(meter_number)
        if not meter:
            return jsonify({"error": f"电表 {meter_number} 不存在"}), 404
        ok = db.delete_meter(meter_number)
        if ok:
            return jsonify({"success": True, "message": f"电表 {meter_number} 已删除"})
        else:
            return jsonify({"error": "删除失败"}), 500

    # ---- 电表详情 API ----
    @app.route("/api/meters/<meter_number>")
    def meter_detail_api(meter_number):
        meter = db.get_meter(meter_number)
        if not meter:
            return jsonify({"error": "电表不存在"}), 404
        return jsonify(meter)

    # ---- 电表数据来源 API ----
    @app.route("/api/meters/sources")
    def meter_sources_api():
        """查询某个用户编号下所有电表的数据来源文件。

        合并两个层级的来源信息：
        1. 电表档案来源（meters.source_file）—— 首页更新时写入
        2. 抄表数据来源（monthly_readings.source_file）—— 账单导入时写入
        """
        user_id = request.args.get("user_id", "")
        if not user_id:
            return jsonify({"error": "缺少 user_id 参数"}), 400
        with db.connection() as conn:
            # 抄表数据来源
            reading_rows = conn.execute("""
                SELECT DISTINCT m.meter_number, m.meter_type,
                       r.reading_month, r.source_file, r.source_sheet
                FROM meters m
                JOIN monthly_readings r ON r.meter_id = m.id
                WHERE m.user_id = ?
                ORDER BY m.meter_number, r.reading_month
            """, (user_id,)).fetchall()

            # 电表档案来源（仅补充没有抄表来源的电表）
            meter_rows = conn.execute("""
                SELECT m.meter_number, m.meter_type,
                       '电表档案' AS reading_month,
                       m.source_file, m.source_sheet
                FROM meters m
                WHERE m.user_id = ?
                  AND m.source_file IS NOT NULL AND m.source_file != ''
                ORDER BY m.meter_number
            """, (user_id,)).fetchall()

            sources = [dict(r) for r in reading_rows]

            # 把电表档案来源也加入（标记为"电表档案"）
            meters_with_readings = {r["meter_number"] for r in reading_rows}
            for r in meter_rows:
                d = dict(r)
                if d["meter_number"] not in meters_with_readings:
                    sources.append(d)
                else:
                    # 已有抄表来源的电表，也追加档案来源行
                    sources.append(d)

            # 按电表号排序
            sources.sort(key=lambda x: (x.get("meter_number", ""), x.get("reading_month", "")))

        return jsonify({"user_id": user_id, "sources": sources})

    # ---- 源文件内容查看 API ----
    @app.route("/api/source-file/view")
    def source_file_view():
        """查看数据来源文件的内容。

        支持 Excel (.xlsx/.xls) 文件：返回指定工作表的表格内容。
        支持图片文件：返回图片的 Base64 编码。
        """
        filename = request.args.get("filename", "").strip()
        sheet_name = request.args.get("sheet", "").strip()
        if not filename:
            return jsonify({"error": "缺少 filename 参数"}), 400

        # 在 temp_attachments 和 archive 目录中查找文件
        search_dirs = [
            Path(config.get("attachments", "temp_dir",
                            default="output/temp_attachments")),
            Path(config.get("storage", "archive_root",
                            default="output/archive")),
        ]

        found_path = None
        for base_dir in search_dirs:
            if not base_dir.exists():
                continue
            # 优先按相对路径精确匹配（source_file 含目录时）
            if "/" in filename:
                candidate = base_dir / filename
                if candidate.is_file():
                    found_path = candidate
                    break
            # 兜底：按文件名搜索（兼容旧数据）
            if not found_path:
                for p in base_dir.rglob(Path(filename).name):
                    if p.is_file() and not p.name.startswith("~$"):
                        found_path = p
                        break
            if found_path:
                break

        if not found_path:
            return jsonify({"error": f"文件未找到: {filename}"}), 404

        suffix = found_path.suffix.lower()

        # Excel 文件：读取并返回表格内容
        if suffix in (".xlsx", ".xls"):
            try:
                import pandas as pd
                xls = pd.ExcelFile(str(found_path))
                all_sheets = xls.sheet_names

                if sheet_name and sheet_name in all_sheets:
                    target_sheets = [sheet_name]
                else:
                    target_sheets = all_sheets

                result_sheets = []
                for sn in target_sheets:
                    df = pd.read_excel(xls, sheet_name=sn, header=None,
                                       dtype=str, keep_default_na=False)
                    # 限制返回行数避免数据过大
                    max_rows = 200
                    truncated = len(df) > max_rows
                    if truncated:
                        df = df.head(max_rows)
                    rows = df.values.tolist()
                    result_sheets.append({
                        "sheet_name": sn,
                        "rows": rows,
                        "total_rows": len(df) if not truncated else f"{max_rows}+",
                        "truncated": truncated,
                    })

                return jsonify({
                    "filename": filename,
                    "type": "excel",
                    "sheets": result_sheets,
                    "all_sheet_names": all_sheets,
                })
            except Exception as e:
                return jsonify({"error": f"读取文件失败: {e}"}), 500

        # 图片文件
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
            import base64
            with open(found_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            mime = {
                ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".bmp": "image/bmp",
                ".tiff": "image/tiff",
            }.get(suffix, "image/png")
            return jsonify({
                "filename": filename,
                "type": "image",
                "mime": mime,
                "data": data,
            })

        # CSV 文件
        elif suffix == ".csv":
            try:
                import pandas as pd
                df = pd.read_csv(str(found_path), dtype=str,
                                 keep_default_na=False, nrows=200)
                return jsonify({
                    "filename": filename,
                    "type": "csv",
                    "headers": list(df.columns),
                    "rows": df.values.tolist(),
                })
            except Exception as e:
                return jsonify({"error": f"读取文件失败: {e}"}), 500

        else:
            return jsonify({"error": f"不支持的文件类型: {suffix}"}), 400

    # ---- CSV 文件下载 ----
    @app.route("/download-csv/<filename>")
    def download_csv(filename):
        csv_dir = config.get("storage", "csv_export_dir", default="output/data")
        safe_dir = Path(csv_dir).resolve()
        target = (safe_dir / filename).resolve()
        if not str(target).startswith(str(safe_dir)) or not target.exists():
            abort(404)
        return send_from_directory(str(safe_dir), filename, as_attachment=True)

    # ---- 项目管理 ----
    @app.route("/projects")
    def projects_list():
        all_projects = db.get_projects()
        return render_template("projects.html", projects=all_projects)

    @app.route("/projects/create", methods=["GET", "POST"])
    def project_create():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            try:
                db.create_project(name)
                flash(f"项目 '{name}' 创建成功", "success")
                return redirect(url_for("projects_list"))
            except ValueError as e:
                flash(str(e), "danger")
                return redirect(url_for("project_create"))
        return render_template("project_create.html")

    @app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
    def project_edit(project_id):
        project = db.get_project(project_id)
        if not project:
            abort(404)
        if request.method == "POST":
            new_name = request.form.get("name", "").strip()
            try:
                db.update_project(project_id, name=new_name)
                flash("项目已更新", "success")
                return redirect(url_for("projects_list"))
            except ValueError as e:
                flash(str(e), "danger")
                return redirect(url_for("project_edit", project_id=project_id))
        return render_template("project_edit.html", project=project)

    @app.route("/projects/<int:project_id>/delete", methods=["POST"])
    def project_delete(project_id):
        try:
            db.delete_project(project_id)
            flash("项目已删除", "success")
        except ValueError as e:
            flash(str(e), "danger")
        return redirect(url_for("projects_list"))

    @app.route("/api/projects/<int:project_id>", methods=["DELETE"])
    def project_delete_api(project_id):
        try:
            db.delete_project(project_id)
            return jsonify({"success": True, "message": "项目已删除"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
