"""核心数据模型：以电表为中心的数据结构。

电表号是唯一主键。每次从不同文件提取到同一电表号的信息时，
补全其关联数据（用户号、资产编号、倍率、项目名、电表类型等）。

锁定机制：
    - is_locked=1 时，自动化管线不能修改电表的固定信息
    - 手动修改需先解锁(is_locked=0)，修改后重新锁定
    - 锁定字段：user_id, project_name, meter_type, multiplier, discount, asset_number
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
    discount        REAL DEFAULT 1.0,                -- 折扣系数（如0.95表示95折）
    is_locked       INTEGER DEFAULT 0,               -- 锁定标志 (1=锁定, 0=未锁定)
    paired_meter_id INTEGER,                         -- 配对电表ID（发电表↔上网表）
    project_name    TEXT,                            -- 所属项目
    source_file     TEXT,                            -- 数据来源文件名
    source_sheet    TEXT,                            -- 数据来源工作表
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 月度抄表数据：每月追加
CREATE TABLE IF NOT EXISTS monthly_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id        INTEGER NOT NULL REFERENCES meters(id),
    reading_month   TEXT NOT NULL,               -- 格式: YYYY-MM
    -- 正向有功（用电量 = 本月表数 - 上月表数）
    sharp_peak      REAL,  -- 正向尖
    peak            REAL,  -- 正向峰
    flat            REAL,  -- 正向平
    valley          REAL,  -- 正向谷
    total_kwh       REAL,  -- 正向有功总
    -- 反向有功
    rev_sharp_peak  REAL,  -- 反向尖
    rev_peak        REAL,  -- 反向峰
    rev_flat        REAL,  -- 反向平
    rev_valley      REAL,  -- 反向谷
    rev_total       REAL,  -- 反向有功总
    -- 本月表数
    cur_sharp_peak  REAL,  -- 本月表数：尖峰
    cur_peak        REAL,  -- 本月表数：峰
    cur_flat        REAL,  -- 本月表数：平
    cur_valley      REAL,  -- 本月表数：谷
    cur_total       REAL,  -- 本月表数：总
    -- 上月表数
    prev_sharp_peak REAL,  -- 上月表数：尖峰
    prev_peak       REAL,  -- 上月表数：峰
    prev_flat       REAL,  -- 上月表数：平
    prev_valley     REAL,  -- 上月表数：谷
    prev_total      REAL,  -- 上月表数：总
    stat_date       TEXT,                            -- 统计日期（从表码文件提取）
    is_locked       INTEGER DEFAULT 0,               -- 锁定标志 (1=锁定, 0=未锁定)
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
    average_price       REAL,
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

-- 月度账单汇总视图（含折扣）
CREATE VIEW IF NOT EXISTS v_monthly_bill AS
SELECT
    m.meter_number,
    m.asset_number,
    m.user_id,
    m.meter_type,
    m.multiplier,
    m.discount,
    m.project_name,
    r.reading_month,
    r.sharp_peak,
    r.peak,
    r.flat,
    r.valley,
    r.total_kwh,
    r.rev_sharp_peak,
    r.rev_peak,
    r.rev_flat,
    r.rev_valley,
    r.rev_total,
    r.cur_sharp_peak,
    r.cur_peak,
    r.cur_flat,
    r.cur_valley,
    r.cur_total,
    r.prev_sharp_peak,
    r.prev_peak,
    r.prev_flat,
    r.prev_valley,
    r.prev_total,
    p.sharp_peak_price,
    p.peak_price,
    p.flat_price,
    p.valley_price,
    p.average_price,
    ROUND(COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0) * COALESCE(m.discount, 1.0), 2) AS sharp_peak_amount,
    ROUND(COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0) * COALESCE(m.discount, 1.0), 2)             AS peak_amount,
    ROUND(COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0) * COALESCE(m.discount, 1.0), 2)              AS flat_amount,
    ROUND(COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0) * COALESCE(m.discount, 1.0), 2)          AS valley_amount,
    ROUND(
        CASE
            WHEN (p.sharp_peak_price IS NOT NULL OR p.peak_price IS NOT NULL
                  OR p.flat_price IS NOT NULL OR p.valley_price IS NOT NULL)
            THEN
                (COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0) +
                 COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0) +
                 COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0) +
                 COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0))
                * COALESCE(m.discount, 1.0)
            WHEN p.average_price IS NOT NULL
            THEN COALESCE(r.total_kwh, 0) * m.multiplier * p.average_price * COALESCE(m.discount, 1.0)
            ELSE 0
        END,
    2) AS total_amount,
    r.is_locked AS reading_locked,
    m.is_locked AS meter_locked,
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
            self._migrate(conn)
            log.info("数据库初始化完成: %s", self.db_path)

    def _migrate(self, conn):
        """兼容旧数据库：确保新字段和约束存在。"""
        # 检查 meters 表的列信息
        columns = {row[1] for row in conn.execute("PRAGMA table_info(meters)").fetchall()}

        # 添加 discount 列（如果缺失）
        if "discount" not in columns:
            log.info("迁移: 添加 meters.discount 列")
            conn.execute("ALTER TABLE meters ADD COLUMN discount REAL DEFAULT 1.0")

        # 添加 is_locked 列（如果缺失）
        if "is_locked" not in columns:
            log.info("迁移: 添加 meters.is_locked 列")
            conn.execute("ALTER TABLE meters ADD COLUMN is_locked INTEGER DEFAULT 0")

        # 添加 paired_meter_id 列（如果缺失）
        if "paired_meter_id" not in columns:
            log.info("迁移: 添加 meters.paired_meter_id 列")
            conn.execute("ALTER TABLE meters ADD COLUMN paired_meter_id INTEGER")

        # 添加 source_file / source_sheet 列（如果缺失）
        if "source_file" not in columns:
            log.info("迁移: 添加 meters.source_file 列")
            conn.execute("ALTER TABLE meters ADD COLUMN source_file TEXT")
        if "source_sheet" not in columns:
            log.info("迁移: 添加 meters.source_sheet 列")
            conn.execute("ALTER TABLE meters ADD COLUMN source_sheet TEXT")

        # 添加 monthly_readings 的本月/上月表数列（如果缺失）
        reading_columns = {row[1] for row in conn.execute("PRAGMA table_info(monthly_readings)").fetchall()}
        for col in ("rev_sharp_peak", "rev_peak", "rev_flat", "rev_valley", "rev_total",
                     "cur_sharp_peak", "cur_peak", "cur_flat", "cur_valley", "cur_total",
                     "prev_sharp_peak", "prev_peak", "prev_flat", "prev_valley", "prev_total"):
            if col not in reading_columns:
                log.info("迁移: 添加 monthly_readings.%s 列", col)
                conn.execute(f"ALTER TABLE monthly_readings ADD COLUMN {col} REAL")
        if "stat_date" not in reading_columns:
            log.info("迁移: 添加 monthly_readings.stat_date 列")
            conn.execute("ALTER TABLE monthly_readings ADD COLUMN stat_date TEXT")

        # 添加 monthly_readings.is_locked 列（如果缺失）
        if "is_locked" not in reading_columns:
            log.info("迁移: 添加 monthly_readings.is_locked 列")
            conn.execute("ALTER TABLE monthly_readings ADD COLUMN is_locked INTEGER DEFAULT 0")

        # 添加 average_price 列（如果缺失）
        price_columns = {row[1] for row in conn.execute("PRAGMA table_info(price_records)").fetchall()}
        if "average_price" not in price_columns:
            log.info("迁移: 添加 price_records.average_price 列")
            conn.execute("ALTER TABLE price_records ADD COLUMN average_price REAL")

        # 重建视图（确保包含新字段）
        conn.execute("DROP VIEW IF EXISTS v_monthly_bill")
        conn.execute("""
            CREATE VIEW v_monthly_bill AS
            SELECT
                m.meter_number,
                m.asset_number,
                m.user_id,
                m.meter_type,
                m.multiplier,
                m.discount,
                m.project_name,
                r.reading_month,
                r.sharp_peak,
                r.peak,
                r.flat,
                r.valley,
                r.total_kwh,
                r.rev_sharp_peak,
                r.rev_peak,
                r.rev_flat,
                r.rev_valley,
                r.rev_total,
                r.cur_sharp_peak,
                r.cur_peak,
                r.cur_flat,
                r.cur_valley,
                r.cur_total,
                r.prev_sharp_peak,
                r.prev_peak,
                r.prev_flat,
                r.prev_valley,
                r.prev_total,
                p.sharp_peak_price,
                p.peak_price,
                p.flat_price,
                p.valley_price,
                p.average_price,
                ROUND(COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0) * COALESCE(m.discount, 1.0), 2) AS sharp_peak_amount,
                ROUND(COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0) * COALESCE(m.discount, 1.0), 2)             AS peak_amount,
                ROUND(COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0) * COALESCE(m.discount, 1.0), 2)              AS flat_amount,
                ROUND(COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0) * COALESCE(m.discount, 1.0), 2)          AS valley_amount,
                ROUND(
                    CASE
                        WHEN (p.sharp_peak_price IS NOT NULL OR p.peak_price IS NOT NULL
                              OR p.flat_price IS NOT NULL OR p.valley_price IS NOT NULL)
                        THEN
                            (COALESCE(r.sharp_peak, 0) * m.multiplier * COALESCE(p.sharp_peak_price, 0) +
                             COALESCE(r.peak, 0) * m.multiplier * COALESCE(p.peak_price, 0) +
                             COALESCE(r.flat, 0) * m.multiplier * COALESCE(p.flat_price, 0) +
                             COALESCE(r.valley, 0) * m.multiplier * COALESCE(p.valley_price, 0))
                            * COALESCE(m.discount, 1.0)
                        WHEN p.average_price IS NOT NULL
                        THEN COALESCE(r.total_kwh, 0) * m.multiplier * p.average_price * COALESCE(m.discount, 1.0)
                        ELSE 0
                    END,
                2) AS total_amount,
                r.is_locked AS reading_locked,
                m.is_locked AS meter_locked,
                r.source_file AS reading_source,
                p.source_file AS price_source
            FROM meters m
            JOIN monthly_readings r ON r.meter_id = m.id
            LEFT JOIN price_records p ON p.user_id = m.user_id AND p.reading_month = r.reading_month
        """)

        # 检查 UNIQUE 约束
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='meters'"
        ).fetchall()
        index_names = {row[0] for row in indexes}
        has_unique = any("autoindex" in name or "meter_number" in name for name in index_names)
        if not has_unique:
            log.warning("检测到旧数据库，正在迁移: 重建 meters 表以添加 UNIQUE 约束...")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meters_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    meter_number    TEXT NOT NULL UNIQUE,
                    asset_number    TEXT,
                    user_id         TEXT,
                    meter_type      TEXT NOT NULL DEFAULT '未知',
                    multiplier      REAL DEFAULT 1.0,
                    discount        REAL DEFAULT 1.0,
                    is_locked       INTEGER DEFAULT 0,
                    paired_meter_id INTEGER,
                    project_name    TEXT,
                    source_file     TEXT,
                    source_sheet    TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO meters_new
                    (id, meter_number, asset_number, user_id, meter_type, multiplier, discount, is_locked, project_name, created_at, updated_at)
                    SELECT id, meter_number, asset_number, user_id, meter_type, multiplier,
                           COALESCE(discount, 1.0), COALESCE(is_locked, 0), project_name, created_at, updated_at
                    FROM meters;
                DROP TABLE meters;
                ALTER TABLE meters_new RENAME TO meters;
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meters_user_id ON meters(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meters_project ON meters(project_name)")
            log.info("meters 表迁移完成")

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
                     multiplier: float = None, project_name: str = None,
                     source_file: str = None, source_sheet: str = None) -> int:
        """插入或更新电表信息，返回电表 ID。

        电表号是唯一标识。user_id、asset_number 等是关联属性，
        只在当前值为空时才用新值补全（不覆盖已有数据）。
        锁定的电表不会被自动更新固定信息。

        模糊匹配：如果传入的短电表号是某个已有长电表号的子串，
        则合并到长电表号上（不创建新记录）。反之，如果传入的长电表号
        包含已有的短电表号，则用长号替换短号。
        """
        import re
        _has_chinese = re.compile(r'[\u4e00-\u9fff]')

        if not meter_number or len(meter_number) < 6 or _has_chinese.search(meter_number):
            raise ValueError(f"无效电表号: {meter_number}")
        if user_id and _has_chinese.search(user_id):
            user_id = None
        if asset_number and _has_chinese.search(asset_number):
            asset_number = None

        with self.connection() as conn:
            # 模糊匹配：查找是否有包含关系的已有电表号
            resolved = self._resolve_meter_number(conn, meter_number)
            if resolved != meter_number:
                log.info("  电表号模糊匹配: '%s' -> '%s'", meter_number, resolved)
                meter_number = resolved

            conn.execute(
                """INSERT INTO meters (meter_number, user_id, meter_type, asset_number, multiplier, project_name, source_file, source_sheet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(meter_number) DO UPDATE SET
                       user_id = CASE
                           WHEN meters.is_locked = 1 THEN meters.user_id
                           WHEN meters.user_id IS NULL OR meters.user_id = ''
                           THEN COALESCE(excluded.user_id, meters.user_id)
                           ELSE meters.user_id
                       END,
                       meter_type = CASE
                           WHEN meters.is_locked = 1 THEN meters.meter_type
                           WHEN excluded.meter_type != '未知' THEN excluded.meter_type
                           ELSE meters.meter_type
                       END,
                       asset_number = CASE
                           WHEN meters.is_locked = 1 THEN meters.asset_number
                           WHEN meters.asset_number IS NULL OR meters.asset_number = ''
                           THEN COALESCE(excluded.asset_number, meters.asset_number)
                           ELSE meters.asset_number
                       END,
                       multiplier = CASE
                           WHEN meters.is_locked = 1 THEN meters.multiplier
                           WHEN excluded.multiplier IS NOT NULL AND excluded.multiplier != 1.0
                           THEN excluded.multiplier
                           ELSE meters.multiplier
                       END,
                       project_name = CASE
                           WHEN meters.is_locked = 1 THEN meters.project_name
                           WHEN meters.project_name IS NULL OR meters.project_name = ''
                           THEN COALESCE(excluded.project_name, meters.project_name)
                           ELSE meters.project_name
                       END,
                       source_file = CASE
                           WHEN meters.source_file IS NULL OR meters.source_file = ''
                           THEN COALESCE(excluded.source_file, meters.source_file)
                           ELSE meters.source_file
                       END,
                       source_sheet = CASE
                           WHEN meters.source_sheet IS NULL OR meters.source_sheet = ''
                           THEN COALESCE(excluded.source_sheet, meters.source_sheet)
                           ELSE meters.source_sheet
                       END,
                       updated_at = CURRENT_TIMESTAMP
                """,
                (meter_number, user_id or '', meter_type, asset_number,
                 multiplier if multiplier is not None else 1.0, project_name,
                 source_file or '', source_sheet or ''),
            )
            row = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ?",
                (meter_number,),
            ).fetchone()
            meter_id = row["id"]
            log.debug("电表 upsert: %s -> id=%d", meter_number, meter_id)
            return meter_id

    def _resolve_meter_number(self, conn, meter_number: str) -> str:
        """模糊匹配电表号：短号是长号子串时合并到长号。

        规则：
        1. 精确匹配 → 直接返回
        2. 传入短号，DB中有包含它的长号 → 返回长号（合并到长号）
        3. 传入长号，DB中有被它包含的短号 → 迁移短号数据到长号，删除短号
        4. 无匹配 → 返回原号

        只在纯数字电表号之间做模糊匹配，防止误匹配。
        """
        import re

        # 精确匹配
        exact = conn.execute(
            "SELECT id FROM meters WHERE meter_number = ?", (meter_number,)
        ).fetchone()
        if exact:
            return meter_number

        # 只对纯数字电表号做模糊匹配
        if not re.match(r'^\d+$', meter_number):
            return meter_number

        # 查找所有纯数字电表号
        all_meters = conn.execute(
            "SELECT id, meter_number FROM meters"
        ).fetchall()

        for row in all_meters:
            existing = row["meter_number"]
            if not re.match(r'^\d+$', existing):
                continue

            # Case 2: 传入短号，已有长号包含它
            if len(meter_number) < len(existing) and meter_number in existing:
                return existing

            # Case 3: 传入长号，已有短号被它包含
            if len(meter_number) > len(existing) and existing in meter_number:
                # 迁移：将短号的 readings 和 prices 转移到长号
                log.info("  电表号升级: '%s' -> '%s'，迁移关联数据", existing, meter_number)
                self._migrate_meter(conn, from_number=existing, to_number=meter_number)
                return meter_number

        return meter_number

    def _migrate_meter(self, conn, from_number: str, to_number: str):
        """将短电表号的关联数据迁移到长电表号。"""
        old = conn.execute(
            "SELECT id, user_id, meter_type, asset_number, multiplier, project_name, discount "
            "FROM meters WHERE meter_number = ?", (from_number,)
        ).fetchone()
        if not old:
            return

        old_id = old["id"]

        # 先创建长号记录（继承短号的属性）
        conn.execute(
            """INSERT OR IGNORE INTO meters
               (meter_number, user_id, meter_type, asset_number, multiplier, project_name, discount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (to_number, old["user_id"], old["meter_type"], old["asset_number"],
             old["multiplier"], old["project_name"], old["discount"]),
        )
        new_row = conn.execute(
            "SELECT id FROM meters WHERE meter_number = ?", (to_number,)
        ).fetchone()
        new_id = new_row["id"]

        # 迁移 monthly_readings
        conn.execute(
            "UPDATE OR IGNORE monthly_readings SET meter_id = ? WHERE meter_id = ?",
            (new_id, old_id),
        )
        # 删除无法迁移的冲突记录（同月份）
        conn.execute(
            "DELETE FROM monthly_readings WHERE meter_id = ?", (old_id,)
        )

        # 删除旧短号记录
        conn.execute("DELETE FROM meters WHERE id = ?", (old_id,))

    def create_meter(self, meter_number: str, user_id: str = None,
                     meter_type: str = "未知", asset_number: str = None,
                     multiplier: float = 1.0, discount: float = 1.0,
                     project_name: str = None) -> int:
        """手动创建电表，返回电表 ID。

        与 upsert_meter 不同，此方法用于用户手动添加电表，
        不做模糊匹配，电表号必须唯一。
        """
        if not meter_number or not meter_number.strip():
            raise ValueError("电表号不能为空")
        meter_number = meter_number.strip()

        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ?", (meter_number,)
            ).fetchone()
            if existing:
                raise ValueError(f"电表号 {meter_number} 已存在")

            conn.execute(
                """INSERT INTO meters
                   (meter_number, user_id, meter_type, asset_number, multiplier, discount, project_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (meter_number, user_id or '', meter_type, asset_number,
                 multiplier, discount, project_name),
            )
            row = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ?", (meter_number,)
            ).fetchone()
            meter_id = row["id"]
            log.info("手动创建电表: %s -> id=%d", meter_number, meter_id)
            return meter_id

    def delete_meter(self, meter_number: str) -> bool:
        """删除电表及其关联的抄表数据。"""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM meters WHERE meter_number = ?", (meter_number,)
            ).fetchone()
            if not row:
                return False
            meter_id = row["id"]
            conn.execute("DELETE FROM monthly_readings WHERE meter_id = ?", (meter_id,))
            conn.execute("DELETE FROM meters WHERE id = ?", (meter_id,))
            log.info("删除电表: %s (id=%d) 及其关联数据", meter_number, meter_id)
            return True

    def cleanup_incomplete_meters(self) -> int:
        """删除数据不全的电表及其关联读数。

        不全的定义：缺少 用户编号、项目名、电表类型（未知）任一关键字段。
        已锁定的电表不删除（手动维护的数据视为有效）。
        """
        with self.connection() as conn:
            incomplete = conn.execute("""
                SELECT id, meter_number, user_id, project_name, meter_type
                FROM meters
                WHERE is_locked = 0
                  AND (user_id IS NULL OR user_id = ''
                       OR project_name IS NULL OR project_name = ''
                       OR meter_type IS NULL OR meter_type = '未知')
            """).fetchall()

            if not incomplete:
                log.info("清理：无数据不全的电表")
                return 0

            count = 0
            for row in incomplete:
                meter_id = row["id"]
                mn = row["meter_number"]
                missing = []
                if not row["user_id"]:
                    missing.append("用户编号")
                if not row["project_name"]:
                    missing.append("项目名")
                if not row["meter_type"] or row["meter_type"] == "未知":
                    missing.append("电表类型")

                conn.execute("DELETE FROM monthly_readings WHERE meter_id = ?", (meter_id,))
                conn.execute("DELETE FROM meters WHERE id = ?", (meter_id,))
                log.info("  清理: %s (缺少: %s)", mn, ", ".join(missing))
                count += 1

            log.info("清理完成：删除 %d 个数据不全的电表", count)
            return count

    def update_meter(self, meter_number: str, **fields) -> bool:
        """手动更新电表信息（仅限解锁状态，或管理员操作）。

        可更新字段: user_id, meter_type, multiplier, discount,
                   asset_number, project_name, is_locked
        """
        allowed = {"user_id", "meter_type", "multiplier", "discount",
                    "asset_number", "project_name", "is_locked"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        with self.connection() as conn:
            # 若不是解锁/锁定操作，检查是否已锁定
            if "is_locked" not in updates:
                row = conn.execute(
                    "SELECT is_locked FROM meters WHERE meter_number = ?",
                    (meter_number,)
                ).fetchone()
                if row and row["is_locked"]:
                    log.warning("电表 %s 已锁定，拒绝修改。请先解锁。", meter_number)
                    return False

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [meter_number]
            conn.execute(
                f"UPDATE meters SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE meter_number = ?",
                values,
            )
            log.info("电表 %s 手动更新: %s", meter_number, updates)
            return True

    def lock_meter(self, meter_number: str) -> bool:
        """锁定电表，防止自动化程序修改固定信息。"""
        return self.update_meter(meter_number, is_locked=1)

    def unlock_meter(self, meter_number: str) -> bool:
        """解锁电表，允许手动修改。"""
        with self.connection() as conn:
            conn.execute(
                "UPDATE meters SET is_locked = 0, updated_at = CURRENT_TIMESTAMP WHERE meter_number = ?",
                (meter_number,)
            )
            log.info("电表 %s 已解锁", meter_number)
            return True

    def set_meter_pair(self, meter_number_a: str, meter_number_b: str) -> bool:
        """设置两个电表的配对关系（发电表↔上网表）。"""
        with self.connection() as conn:
            a = conn.execute("SELECT id FROM meters WHERE meter_number = ?", (meter_number_a,)).fetchone()
            b = conn.execute("SELECT id FROM meters WHERE meter_number = ?", (meter_number_b,)).fetchone()
            if not a or not b:
                return False
            conn.execute("UPDATE meters SET paired_meter_id = ? WHERE id = ?", (b["id"], a["id"]))
            conn.execute("UPDATE meters SET paired_meter_id = ? WHERE id = ?", (a["id"], b["id"]))
            log.info("配对电表: %s <-> %s", meter_number_a, meter_number_b)
            return True

    def get_paired_meter(self, meter_id: int) -> Optional[dict]:
        """获取配对电表信息。"""
        with self.connection() as conn:
            row = conn.execute(
                """SELECT m2.* FROM meters m1
                   JOIN meters m2 ON m1.paired_meter_id = m2.id
                   WHERE m1.id = ?""",
                (meter_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_reading(self, meter_id: int, reading_month: str,
                       sharp_peak: float = None, peak: float = None,
                       flat: float = None, valley: float = None,
                       total_kwh: float = None,
                       rev_sharp_peak: float = None, rev_peak: float = None,
                       rev_flat: float = None, rev_valley: float = None,
                       rev_total: float = None,
                       source_file: str = None,
                       source_sheet: str = None, email_date: datetime = None,
                       cur_sharp_peak: float = None, cur_peak: float = None,
                       cur_flat: float = None, cur_valley: float = None,
                       cur_total: float = None,
                       prev_sharp_peak: float = None, prev_peak: float = None,
                       prev_flat: float = None, prev_valley: float = None,
                       prev_total: float = None,
                       stat_date: str = None):
        """插入或更新月度抄表数据。已锁定的记录不会被自动更新。"""
        if total_kwh is None:
            fwd_parts = [v for v in [sharp_peak, peak, flat, valley] if v is not None]
            if fwd_parts:
                total_kwh = sum(fwd_parts)
        if rev_total is None:
            rev_parts = [v for v in [rev_sharp_peak, rev_peak, rev_flat, rev_valley] if v is not None]
            if rev_parts:
                rev_total = sum(rev_parts)

        with self.connection() as conn:
            # 检查是否已锁定
            existing = conn.execute(
                "SELECT is_locked FROM monthly_readings WHERE meter_id = ? AND reading_month = ?",
                (meter_id, reading_month),
            ).fetchone()
            if existing and existing["is_locked"]:
                log.debug("抄表数据已锁定，跳过: meter_id=%d, month=%s", meter_id, reading_month)
                return

            conn.execute(
                """INSERT INTO monthly_readings
                   (meter_id, reading_month, sharp_peak, peak, flat, valley, total_kwh,
                    rev_sharp_peak, rev_peak, rev_flat, rev_valley, rev_total,
                    cur_sharp_peak, cur_peak, cur_flat, cur_valley, cur_total,
                    prev_sharp_peak, prev_peak, prev_flat, prev_valley, prev_total,
                    stat_date, source_file, source_sheet, email_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(meter_id, reading_month) DO UPDATE SET
                       sharp_peak = COALESCE(monthly_readings.sharp_peak, excluded.sharp_peak),
                       peak = COALESCE(monthly_readings.peak, excluded.peak),
                       flat = COALESCE(monthly_readings.flat, excluded.flat),
                       valley = COALESCE(monthly_readings.valley, excluded.valley),
                       total_kwh = COALESCE(monthly_readings.total_kwh, excluded.total_kwh),
                       rev_sharp_peak = COALESCE(monthly_readings.rev_sharp_peak, excluded.rev_sharp_peak),
                       rev_peak = COALESCE(monthly_readings.rev_peak, excluded.rev_peak),
                       rev_flat = COALESCE(monthly_readings.rev_flat, excluded.rev_flat),
                       rev_valley = COALESCE(monthly_readings.rev_valley, excluded.rev_valley),
                       rev_total = COALESCE(monthly_readings.rev_total, excluded.rev_total),
                       cur_sharp_peak = COALESCE(monthly_readings.cur_sharp_peak, excluded.cur_sharp_peak),
                       cur_peak = COALESCE(monthly_readings.cur_peak, excluded.cur_peak),
                       cur_flat = COALESCE(monthly_readings.cur_flat, excluded.cur_flat),
                       cur_valley = COALESCE(monthly_readings.cur_valley, excluded.cur_valley),
                       cur_total = COALESCE(monthly_readings.cur_total, excluded.cur_total),
                       prev_sharp_peak = COALESCE(monthly_readings.prev_sharp_peak, excluded.prev_sharp_peak),
                       prev_peak = COALESCE(monthly_readings.prev_peak, excluded.prev_peak),
                       prev_flat = COALESCE(monthly_readings.prev_flat, excluded.prev_flat),
                       prev_valley = COALESCE(monthly_readings.prev_valley, excluded.prev_valley),
                       prev_total = COALESCE(monthly_readings.prev_total, excluded.prev_total),
                       stat_date = COALESCE(monthly_readings.stat_date, excluded.stat_date),
                       source_file = COALESCE(monthly_readings.source_file, excluded.source_file),
                       source_sheet = COALESCE(monthly_readings.source_sheet, excluded.source_sheet)
                """,
                (meter_id, reading_month, sharp_peak, peak, flat, valley, total_kwh,
                 rev_sharp_peak, rev_peak, rev_flat, rev_valley, rev_total,
                 cur_sharp_peak, cur_peak, cur_flat, cur_valley, cur_total,
                 prev_sharp_peak, prev_peak, prev_flat, prev_valley, prev_total,
                 stat_date, source_file, source_sheet,
                 email_date.isoformat() if email_date else None),
            )
            log.debug("抄表数据 upsert: meter_id=%d, month=%s", meter_id, reading_month)

    def update_reading(self, meter_id: int, reading_month: str, **fields):
        """手动更新抄表数据（用于前端编辑，不受锁定限制）。"""
        allowed = {"sharp_peak", "peak", "flat", "valley", "total_kwh",
                   "rev_sharp_peak", "rev_peak", "rev_flat", "rev_valley", "rev_total",
                   "cur_sharp_peak", "cur_peak", "cur_flat", "cur_valley", "cur_total",
                   "prev_sharp_peak", "prev_peak", "prev_flat", "prev_valley", "prev_total",
                   "stat_date"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [meter_id, reading_month]
        with self.connection() as conn:
            conn.execute(
                f"UPDATE monthly_readings SET {set_clause} WHERE meter_id = ? AND reading_month = ?",
                values,
            )

    def lock_reading(self, meter_id: int, reading_month: str, locked: bool = True):
        """锁定/解锁某条抄表记录。"""
        with self.connection() as conn:
            conn.execute(
                "UPDATE monthly_readings SET is_locked = ? WHERE meter_id = ? AND reading_month = ?",
                (1 if locked else 0, meter_id, reading_month),
            )

    def get_readings_grouped(self, project_name: str = None, user_id: str = None,
                             reading_month: str = None) -> list[dict]:
        """获取抄表数据，按用户分组，发电表+上网表并排，含单价信息。"""
        query = """
            SELECT m.id AS meter_id, m.meter_number, m.asset_number, m.user_id,
                   m.meter_type, m.multiplier, m.discount, m.project_name,
                   m.paired_meter_id, m.is_locked AS meter_locked,
                   r.reading_month,
                   r.sharp_peak, r.peak, r.flat, r.valley, r.total_kwh,
                   r.rev_sharp_peak, r.rev_peak, r.rev_flat, r.rev_valley, r.rev_total,
                   r.cur_sharp_peak, r.cur_peak, r.cur_flat, r.cur_valley, r.cur_total,
                   r.prev_sharp_peak, r.prev_peak, r.prev_flat, r.prev_valley, r.prev_total,
                   r.stat_date,
                   r.is_locked AS reading_locked,
                   r.source_file, r.source_sheet,
                   p.sharp_peak_price, p.peak_price, p.flat_price, p.valley_price,
                   p.average_price, p.source_file AS price_source
            FROM meters m
            JOIN monthly_readings r ON r.meter_id = m.id
            LEFT JOIN price_records p ON p.user_id = m.user_id AND p.reading_month = r.reading_month
            WHERE 1=1
        """
        params = []
        if project_name:
            query += " AND m.project_name = ?"
            params.append(project_name)
        if user_id:
            query += " AND m.user_id = ?"
            params.append(user_id)
        if reading_month:
            query += " AND r.reading_month = ?"
            params.append(reading_month)
        query += " ORDER BY m.user_id, r.reading_month, m.meter_type"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def upsert_price(self, user_id: str, reading_month: str,
                     sharp_peak_price: float = None, peak_price: float = None,
                     flat_price: float = None, valley_price: float = None,
                     average_price: float = None, source_file: str = None):
        """插入或更新单价记录。"""
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO price_records
                   (user_id, reading_month, sharp_peak_price, peak_price, flat_price, valley_price,
                    average_price, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, reading_month) DO UPDATE SET
                       sharp_peak_price = COALESCE(price_records.sharp_peak_price, excluded.sharp_peak_price),
                       peak_price = COALESCE(price_records.peak_price, excluded.peak_price),
                       flat_price = COALESCE(price_records.flat_price, excluded.flat_price),
                       valley_price = COALESCE(price_records.valley_price, excluded.valley_price),
                       average_price = COALESCE(price_records.average_price, excluded.average_price),
                       source_file = COALESCE(price_records.source_file, excluded.source_file)
                """,
                (user_id, reading_month, sharp_peak_price, peak_price, flat_price, valley_price,
                 average_price, source_file),
            )

    # ---- 查询 ----

    def get_meter(self, meter_number: str) -> Optional[dict]:
        """查询单个电表详情。"""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM meters WHERE meter_number = ?", (meter_number,)
            ).fetchone()
            return dict(row) if row else None

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

    def get_months(self) -> list[str]:
        """获取所有抄表月份（降序）。"""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT reading_month FROM monthly_readings ORDER BY reading_month DESC"
            ).fetchall()
            return [r["reading_month"] for r in rows]

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

    def infer_missing_data(self):
        """推理补全缺失数据：从汇总行、跨月数据中推算缺失月份。

        策略：
        1. 自动补全 total_kwh：如果分项有值但 total_kwh 为空，累加分项
        2. 跨月推理：同一电表如果大部分月份有数据、缺少个别月份，
           且有汇总/累计数据可参考，则推算缺失月
        """
        with self.connection() as conn:
            # 策略1：补全 total_kwh
            conn.execute("""
                UPDATE monthly_readings SET total_kwh = (
                    COALESCE(sharp_peak, 0) + COALESCE(peak, 0) +
                    COALESCE(flat, 0) + COALESCE(valley, 0)
                )
                WHERE total_kwh IS NULL
                AND (sharp_peak IS NOT NULL OR peak IS NOT NULL
                     OR flat IS NOT NULL OR valley IS NOT NULL)
            """)
            updated = conn.execute("SELECT changes()").fetchone()[0]
            if updated:
                log.info("推理补全: %d 条记录的 total_kwh 已从分项累加", updated)

            # 策略2：跨月推理 - 从汇总数据反推缺失月份
            # 查找所有电表的月度数据情况
            meters = conn.execute("""
                SELECT m.id, m.meter_number, COUNT(r.id) as month_count,
                       GROUP_CONCAT(r.reading_month ORDER BY r.reading_month) as months
                FROM meters m
                JOIN monthly_readings r ON r.meter_id = m.id
                GROUP BY m.id
                HAVING month_count >= 2
            """).fetchall()

            for meter in meters:
                meter_id = meter["id"]
                existing_months = set(meter["months"].split(","))

                # 检查是否有连续月份的缺口
                all_months = sorted(existing_months)
                if not all_months:
                    continue

                # 解析年月范围
                import re
                parsed = []
                for m in all_months:
                    match = re.match(r'(\d{4})-(\d{2})', m)
                    if match:
                        parsed.append((int(match.group(1)), int(match.group(2))))

                if len(parsed) < 2:
                    continue

                # 找出缺失的月份（在最小和最大月之间的空洞）
                min_y, min_m = parsed[0]
                max_y, max_m = parsed[-1]
                expected = set()
                y, mo = min_y, min_m
                while (y, mo) <= (max_y, max_m):
                    expected.add(f"{y}-{str(mo).zfill(2)}")
                    mo += 1
                    if mo > 12:
                        mo = 1
                        y += 1

                missing = expected - existing_months
                if not missing:
                    continue

                # 只尝试推理单个缺失月份（多个缺失不靠谱）
                if len(missing) > 2:
                    continue

                # 获取该电表所有月度数据（求平均模式来填补）
                readings = conn.execute("""
                    SELECT reading_month, sharp_peak, peak, flat, valley, total_kwh
                    FROM monthly_readings WHERE meter_id = ?
                    ORDER BY reading_month
                """, (meter_id,)).fetchall()

                if len(readings) < 3:
                    continue  # 样本太少

                # 计算各字段的平均值作为缺失月份的估算
                fields = ["sharp_peak", "peak", "flat", "valley"]
                avgs = {}
                for f in fields:
                    vals = [r[f] for r in readings if r[f] is not None]
                    if vals:
                        avgs[f] = round(sum(vals) / len(vals), 2)
                    else:
                        avgs[f] = None

                total_vals = [r["total_kwh"] for r in readings if r["total_kwh"] is not None]
                avg_total = round(sum(total_vals) / len(total_vals), 2) if total_vals else None

                for missing_month in sorted(missing):
                    # 用平均值填充缺失月份
                    conn.execute("""
                        INSERT OR IGNORE INTO monthly_readings
                        (meter_id, reading_month, sharp_peak, peak, flat, valley, total_kwh,
                         source_file, source_sheet)
                        VALUES (?, ?, ?, ?, ?, ?, ?, '推理补全', '跨月平均')
                    """, (meter_id, missing_month,
                          avgs.get("sharp_peak"), avgs.get("peak"),
                          avgs.get("flat"), avgs.get("valley"), avg_total))
                    log.info("推理补全: 电表 %s 月份 %s 使用跨月平均估算 (total=%.2f)",
                             meter["meter_number"], missing_month, avg_total or 0)

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
