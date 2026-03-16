"""核心数据模型：以电表为中心的数据结构。

所有数据围绕电表展开，每条记录可追溯到具体电表。
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
-- 电表主表：固定属性，写入一次
CREATE TABLE IF NOT EXISTS meters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_number    TEXT NOT NULL,               -- 电表号
    asset_number    TEXT,                        -- 资产编号
    user_id         TEXT NOT NULL,               -- 用户编号（核心关联键）
    meter_type      TEXT NOT NULL DEFAULT '未知', -- 上网表 / 发电表
    multiplier      REAL DEFAULT 1.0,            -- 倍率
    project_name    TEXT,                        -- 所属项目
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meter_number, user_id)
);

-- 月度抄表数据：每月追加
CREATE TABLE IF NOT EXISTS monthly_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id        INTEGER NOT NULL REFERENCES meters(id),
    reading_month   TEXT NOT NULL,               -- 格式: YYYY-MM
    -- 表码数据（根据电表类型填对应方向）
    sharp_peak      REAL,  -- 尖峰（上网表: 反向尖峰 / 发电表: 正向尖）
    peak            REAL,  -- 峰
    flat            REAL,  -- 平
    valley          REAL,  -- 谷
    total_kwh       REAL,  -- 总电量（自动计算或从源取）
    -- 来源信息
    source_file     TEXT,  -- 数据来源文件
    source_sheet    TEXT,  -- 来源 Sheet 名
    email_date      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meter_id, reading_month)
);

-- 单价数据（从图片 OCR 提取，通过 user_id 关联）
CREATE TABLE IF NOT EXISTS price_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    reading_month       TEXT NOT NULL,               -- 格式: YYYY-MM
    sharp_peak_price    REAL,  -- 尖峰单价
    peak_price          REAL,  -- 峰单价
    flat_price          REAL,  -- 平单价
    valley_price        REAL,  -- 谷单价
    source_file         TEXT,  -- 图片来源
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, reading_month)
);

-- 月度账单汇总视图（电量 × 单价 = 金额）
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
    -- 金额计算 = 电量 × 倍率 × 单价
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

    def upsert_meter(self, meter_number: str, user_id: str,
                     meter_type: str = "未知", asset_number: str = None,
                     multiplier: float = 1.0, project_name: str = None) -> int:
        """插入或更新电表信息，返回电表 ID。"""
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO meters (meter_number, user_id, meter_type, asset_number, multiplier, project_name)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(meter_number, user_id) DO UPDATE SET
                       meter_type = COALESCE(excluded.meter_type, meters.meter_type),
                       asset_number = COALESCE(excluded.asset_number, meters.asset_number),
                       multiplier = COALESCE(excluded.multiplier, meters.multiplier),
                       project_name = COALESCE(excluded.project_name, meters.project_name),
                       updated_at = CURRENT_TIMESTAMP
                """,
                (meter_number, user_id, meter_type, asset_number, multiplier, project_name),
            )
            # 获取 ID
            row = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ? AND user_id = ?",
                (meter_number, user_id),
            ).fetchone()
            meter_id = row["id"]
            log.debug("电表 upsert: %s (user=%s) -> id=%d", meter_number, user_id, meter_id)
            return meter_id

    def upsert_reading(self, meter_id: int, reading_month: str,
                       sharp_peak: float = None, peak: float = None,
                       flat: float = None, valley: float = None,
                       total_kwh: float = None, source_file: str = None,
                       source_sheet: str = None, email_date: datetime = None):
        """插入或更新月度抄表数据。"""
        # 如果未提供 total_kwh，自动累加
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
        """查询电表列表，可按项目或用户编号过滤。"""
        query = "SELECT * FROM meters WHERE 1=1"
        params = []
        if project_name:
            query += " AND project_name = ?"
            params.append(project_name)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        query += " ORDER BY project_name, user_id, meter_number"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_monthly_bill(self, project_name: str = None, user_id: str = None,
                         meter_number: str = None, month_from: str = None,
                         month_to: str = None) -> list[dict]:
        """查询月度账单汇总，支持多维度过滤。"""
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
                "SELECT DISTINCT project_name FROM meters WHERE project_name IS NOT NULL ORDER BY project_name"
            ).fetchall()
            return [r["project_name"] for r in rows]

    def get_user_ids(self, project_name: str = None) -> list[str]:
        """获取所有用户编号，可按项目过滤。"""
        query = "SELECT DISTINCT user_id FROM meters WHERE 1=1"
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
