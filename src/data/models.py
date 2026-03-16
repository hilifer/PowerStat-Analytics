"""核心数据模型：以电表为中心的数据结构。

电表号是唯一主键。每次从不同文件提取到同一电表号的信息时，
补全其关联数据（用户号、资产编号、倍率、项目名、电表类型等）。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log

# ============================================================
# SQL Schema
# ============================================================

SCHEMA_SQL = """
-- 电表主表：电表号唯一，关联属性可逐步补全
CREATE TABLE IF NOT EXISTS meters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_number    TEXT NOT NULL UNIQUE,            -- 电表号（唯一主键）
    asset_number    TEXT,                            -- 资产编号
    user_id         TEXT,                            -- 用户编号
    meter_type      TEXT NOT NULL DEFAULT '未知',     -- 上网表 / 发电表
    multiplier      REAL DEFAULT 1.0,                -- 倍率
    project_name    TEXT,                            -- 所属项目
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 月度抄表数据：每月追加
CREATE TABLE IF NOT EXISTS monthly_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id        INTEGER NOT NULL REFERENCES meters(id),
    reading_month   TEXT NOT NULL,               -- 格式: YYYY-MM
    sharp_peak      REAL,  -- 尖峰
    peak            REAL,  -- 峰
    flat            REAL,  -- 平
    valley          REAL,  -- 谷
    total_kwh       REAL,  -- 总电量
    source_file     TEXT,
    source_sheet    TEXT,
    email_date      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meter_id, reading_month)
);

-- 单价数据（通过 user_id 关联）
CREATE TABLE IF NOT EXISTS price_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    reading_month       TEXT NOT NULL,
    sharp_peak_price    REAL,
    peak_price          REAL,
    flat_price          REAL,
    valley_price        REAL,
    source_file         TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, reading_month)
);

-- 已处理邮件记录（防重复）
CREATE TABLE IF NOT EXISTS processed_emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    filename    TEXT,
    subject     TEXT,
    email_date  TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 月度账单汇总视图
CREATE VIEW IF NOT EXISTS v_monthly_bill AS
SELECT
    m.meter_number,
    m.asset_number,
    m.user_id,
    m.meter_type,
    m.multiplier,
    m.project_name,
    r.reading_month,
    r.sharp_peak,
    r.peak,
    r.flat,
    r.valley,
    r.total_kwh,
    p.sharp_peak_price,
    p.peak_price,
    p.flat_price,
    p.valley_price,
    ROUND(COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0), 2) AS sharp_peak_amount,
    ROUND(COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0), 2)             AS peak_amount,
    ROUND(COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0), 2)              AS flat_amount,
    ROUND(COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0), 2)          AS valley_amount,
    ROUND(
        COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0) +
        COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0) +
        COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0) +
        COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0),
    2) AS total_amount,
    r.source_file AS reading_source,
    p.source_file AS price_source
FROM meters m
JOIN monthly_readings r ON r.meter_id = m.id
LEFT JOIN price_records p ON p.user_id = m.user_id AND p.reading_month = r.reading_month;

-- 索引
CREATE INDEX IF NOT EXISTS idx_meters_user_id ON meters(user_id);
CREATE INDEX IF NOT EXISTS idx_meters_project ON meters(project_name);
CREATE INDEX IF NOT EXISTS idx_readings_month ON monthly_readings(reading_month);
CREATE INDEX IF NOT EXISTS idx_price_user_month ON price_records(user_id, reading_month);
"""


class Database:
    """SQLite 数据库管理器。"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = config.get("storage", "database", default="output/data/powerstat.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            log.info("数据库初始化完成: %s", self.db_path)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- 电表操作 ----

    def upsert_meter(self, meter_number: str, user_id: str = None,
                     meter_type: str = "未知", asset_number: str = None,
                     multiplier: float = None, project_name: str = None) -> int:
        """插入或更新电表信息，返回电表 ID。

        电表号是唯一标识。user_id、asset_number 等是关联属性，
        只在当前值为空时才用新值补全（不覆盖已有数据）。
        """
        import re
        _has_chinese = re.compile(r'[\u4e00-\u9fff]')

        # DB 层安全检查：拒绝含中文的编号
        if not meter_number or len(meter_number) < 6 or _has_chinese.search(meter_number):
            raise ValueError(f"无效电表号: {meter_number}")
        if user_id and _has_chinese.search(user_id):
            user_id = None
        if asset_number and _has_chinese.search(asset_number):
            asset_number = None
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO meters (meter_number, user_id, meter_type, asset_number, multiplier, project_name)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(meter_number) DO UPDATE SET
                       user_id = CASE
                           WHEN meters.user_id IS NULL OR meters.user_id = ''
                           THEN COALESCE(excluded.user_id, meters.user_id)
                           ELSE meters.user_id
                       END,
                       meter_type = CASE
                           WHEN excluded.meter_type != '未知' THEN excluded.meter_type
                           ELSE meters.meter_type
                       END,
                       asset_number = CASE
                           WHEN meters.asset_number IS NULL OR meters.asset_number = ''
                           THEN COALESCE(excluded.asset_number, meters.asset_number)
                           ELSE meters.asset_number
                       END,
                       multiplier = CASE
                           WHEN excluded.multiplier IS NOT NULL AND excluded.multiplier != 1.0
                           THEN excluded.multiplier
                           ELSE meters.multiplier
                       END,
                       project_name = CASE
                           WHEN meters.project_name IS NULL OR meters.project_name = ''
                           THEN COALESCE(excluded.project_name, meters.project_name)
                           ELSE meters.project_name
                       END,
                       updated_at = CURRENT_TIMESTAMP
                """,
                (meter_number, user_id or '', meter_type, asset_number,
                 multiplier if multiplier is not None else 1.0, project_name),
            )
            row = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ?",
                (meter_number,),
            ).fetchone()
            meter_id = row["id"]
            log.debug("电表 upsert: %s -> id=%d", meter_number, meter_id)
            return meter_id

    def upsert_reading(self, meter_id: int, reading_month: str,
                       sharp_peak: float = None, peak: float = None,
                       flat: float = None, valley: float = None,
                       total_kwh: float = None, source_file: str = None,
                       source_sheet: str = None, email_date: datetime = None):
        """插入或更新月度抄表数据。"""
        if total_kwh is None:
            total_kwh = sum(v for v in [sharp_peak, peak, flat, valley] if v is not None)

        with self.connection() as conn:
            conn.execute(
                """INSERT INTO monthly_readings
                   (meter_id, reading_month, sharp_peak, peak, flat, valley, total_kwh,
                    source_file, source_sheet, email_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(meter_id, reading_month) DO UPDATE SET
                       sharp_peak = COALESCE(excluded.sharp_peak, monthly_readings.sharp_peak),
                       peak = COALESCE(excluded.peak, monthly_readings.peak),
                       flat = COALESCE(excluded.flat, monthly_readings.flat),
                       valley = COALESCE(excluded.valley, monthly_readings.valley),
                       total_kwh = COALESCE(excluded.total_kwh, monthly_readings.total_kwh),
                       source_file = COALESCE(excluded.source_file, monthly_readings.source_file),
                       source_sheet = COALESCE(excluded.source_sheet, monthly_readings.source_sheet)
                """,
                (meter_id, reading_month, sharp_peak, peak, flat, valley, total_kwh,
                 source_file, source_sheet,
                 email_date.isoformat() if email_date else None),
            )
            log.debug("抄表数据 upsert: meter_id=%d, month=%s", meter_id, reading_month)

    def upsert_price(self, user_id: str, reading_month: str,
                     sharp_peak_price: float = None, peak_price: float = None,
                     flat_price: float = None, valley_price: float = None,
                     source_file: str = None):
        """插入或更新单价记录。"""
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO price_records
                   (user_id, reading_month, sharp_peak_price, peak_price, flat_price, valley_price, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, reading_month) DO UPDATE SET
                       sharp_peak_price = COALESCE(excluded.sharp_peak_price, price_records.sharp_peak_price),
                       peak_price = COALESCE(excluded.peak_price, price_records.peak_price),
                       flat_price = COALESCE(excluded.flat_price, price_records.flat_price),
                       valley_price = COALESCE(excluded.valley_price, price_records.valley_price),
                       source_file = COALESCE(excluded.source_file, price_records.source_file)
                """,
                (user_id, reading_month, sharp_peak_price, peak_price, flat_price, valley_price, source_file),
            )

    # ---- 查询 ----

    def get_meters(self, project_name: str = None, user_id: str = None) -> list[dict]:
        """查询电表列表。"""
        query = "SELECT * FROM meters WHERE 1=1"
        params = []
        if project_name:
            query += " AND project_name = ?"
            params.append(project_name)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        query += " ORDER BY project_name, meter_number"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_monthly_bill(self, project_name: str = None, user_id: str = None,
                         meter_number: str = None, month_from: str = None,
                         month_to: str = None) -> list[dict]:
        """查询月度账单汇总。"""
        query = "SELECT * FROM v_monthly_bill WHERE 1=1"
        params = []
        if project_name:
            query += " AND project_name = ?"
            params.append(project_name)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if meter_number:
            query += " AND meter_number = ?"
            params.append(meter_number)
        if month_from:
            query += " AND reading_month >= ?"
            params.append(month_from)
        if month_to:
            query += " AND reading_month <= ?"
            params.append(month_to)
        query += " ORDER BY reading_month, meter_number"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_projects(self) -> list[str]:
        """获取所有项目名称。"""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project_name FROM meters WHERE project_name IS NOT NULL AND project_name != '' ORDER BY project_name"
            ).fetchall()
            return [r["project_name"] for r in rows]

    def get_user_ids(self, project_name: str = None) -> list[str]:
        """获取所有用户编号。"""
        query = "SELECT DISTINCT user_id FROM meters WHERE user_id IS NOT NULL AND user_id != ''"
        params = []
        if project_name:
            query += " AND project_name = ?"
            params.append(project_name)
        query += " ORDER BY user_id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [r["user_id"] for r in rows]

    def export_csv(self, output_dir: str = None):
        """将所有数据导出为 CSV。"""
        import csv
        if output_dir is None:
            output_dir = config.get("storage", "csv_export_dir", default="output/data")

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        tables = {
            "meters": "SELECT * FROM meters",
            "monthly_readings": """
                SELECT r.*, m.meter_number, m.user_id, m.project_name
                FROM monthly_readings r JOIN meters m ON r.meter_id = m.id
            """,
            "price_records": "SELECT * FROM price_records",
            "monthly_bill": "SELECT * FROM v_monthly_bill",
        }

        with self.connection() as conn:
            for name, query in tables.items():
                rows = conn.execute(query).fetchall()
                if not rows:
                    continue
                filepath = out / f"{name}.csv"
                with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(dict(row))
                log.info("CSV 导出: %s (%d 行)", filepath, len(rows))
