"""用大模型独立提取表码数据，与程序提取结果做对比校验。

测试方法：
1. 大模型（Claude）独立阅读含完整表码格式的 Excel/XLS 文件
   （必须同时包含：日期、正向 总/尖/峰/平/谷、反向 总/尖/峰/平/谷）
2. 程序端用 MultiPassExtractor 提取同样的数据
3. 逐字段对比两者结果，全部一致则通过

大模型提取结果作为 fixtures 固化在本文件中（由 Claude 阅读原始 Excel 后生成）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import SmartDispatcher
from src.parsers.multi_pass import MultiPassExtractor

# 允许的数值误差
TOLERANCE = 0.01

# ============================================================
# 大模型提取的 fixtures（由 Claude 阅读原始 Excel 后生成）
#
# 数据来源：表码数据表（每行一个电表，包含日期+正反向有功读数）
# 这些值是累计表码读数（本月表数），不是差值（电表用理）
# ============================================================

LLM_FIXTURES = [
    # ================================================================
    # 文件1: 耀嵘12月表码数据.xlsx (Sheet1)
    # 列: 用户名称|用户编号|表计资产编号|用户类别|数据时间|
    #      正向有功总(kWh)|正向有功尖(kWh)|正向有功峰(kWh)|正向有功平(kWh)|正向有功谷(kWh)|
    #      正向无功总(kVarh)|
    #      反向有功总(kWh)|反向有功尖(kWh)|反向有功峰(kWh)|反向有功平(kWh)|反向有功谷(kWh)|...
    #
    # 注意：行1（公线专变客户）和行2（地方电厂户）共享同一个表计资产编号
    # 03001SG00011312000002494，程序按资产号合并为一条记录。
    # 行3（光伏发电客户）对应资产 03591SF00000002402908536。
    # ================================================================
    {
        "file": "耀嵘12月表码数据.xlsx",
        "file_path": "output/archive/unknown/耀嵘/耀嵘12月表码数据.xlsx",
        "meter_number": "03001SG00011312000002494",
        "reading_month": "2026-01",
        "fwd_total": 11403.54, "fwd_sharp_peak": 1149.03, "fwd_peak": 2149.65,
        "fwd_flat": 4437.61, "fwd_valley": 3667.24,
        "rev_total": 31.95, "rev_sharp_peak": 8.31, "rev_peak": 7.98,
        "rev_flat": 14.79, "rev_valley": 0.86,
        "description": "耀嵘表码 资产03001SG00011312000002494 2026-01",
    },
    {
        "file": "耀嵘12月表码数据.xlsx",
        "file_path": "output/archive/unknown/耀嵘/耀嵘12月表码数据.xlsx",
        "meter_number": "03591SF00000002402908536",
        "reading_month": "2026-01",
        "fwd_total": 2421.86, "fwd_sharp_peak": 653.03, "fwd_peak": 624.5,
        "fwd_flat": 1053.65, "fwd_valley": 90.67,
        "rev_total": 1.04, "rev_sharp_peak": 0.0, "rev_peak": 0.04,
        "rev_flat": 0.44, "rev_valley": 0.55,
        "description": "耀嵘表码 资产03591SF00000002402908536 2026-01",
    },

    # ================================================================
    # 文件2: 耀嵘.xls (用户表码1)
    # 列: 电表资产号|用户编号|统计日期|正向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)|
    #      反向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)
    # 含2个月数据 (2026-02 和 2026-01)
    # 电表资产号直接作为电表唯一标识
    # ================================================================
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "09001SG00000061804340887",
        "reading_month": "2026-02",
        "fwd_total": 10828.33, "fwd_sharp_peak": 940.39, "fwd_peak": 3061.03,
        "fwd_flat": 4092.24, "fwd_valley": 2734.65,
        "rev_total": 32.05, "rev_sharp_peak": 7.21, "rev_peak": 6.28,
        "rev_flat": 18.51, "rev_valley": 0.03,
        "description": "耀嵘.xls 资产09001SG00000061804340887 2026-02",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "09001SF00000042207349302",
        "reading_month": "2026-02",
        "fwd_total": 2628.87, "fwd_sharp_peak": 717.44, "fwd_peak": 682.36,
        "fwd_flat": 1137.93, "fwd_valley": 91.12,
        "rev_total": 1.01, "rev_sharp_peak": 0.0, "rev_peak": 0.07,
        "rev_flat": 0.42, "rev_valley": 0.51,
        "description": "耀嵘.xls 资产09001SF00000042207349302 2026-02",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "09001SG00000061804340887",
        "reading_month": "2026-01",
        "fwd_total": 10671.06, "fwd_sharp_peak": 924.71, "fwd_peak": 3034.93,
        "fwd_flat": 4034.88, "fwd_valley": 2676.52,
        "rev_total": 27.92, "rev_sharp_peak": 6.4, "rev_peak": 5.7,
        "rev_flat": 15.77, "rev_valley": 0.03,
        "description": "耀嵘.xls 资产09001SG00000061804340887 2026-01",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "09001SF00000042207349302",
        "reading_month": "2026-01",
        "fwd_total": 2419.41, "fwd_sharp_peak": 657.21, "fwd_peak": 626.48,
        "fwd_flat": 1046.07, "fwd_valley": 89.63,
        "rev_total": 0.9, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.38, "rev_valley": 0.46,
        "description": "耀嵘.xls 资产09001SF00000042207349302 2026-01",
    },

    # ================================================================
    # 文件3: 1月用户表码（全部） (洲千).xls
    # Sheet "用户表码1": 26条记录
    # Sheet "Sheet1": 2条记录（与用户表码1重复，程序合并）
    # 列: 用户编号|用户名称|用户类型|用电地址|电表资产号|终端资产编号|终端地址|测量点号|
    #      统计日期|正向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)|
    #      反向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)
    # 统计日期=2026-02-01，电表资产号作为唯一标识
    # 全部 26 条记录
    # ================================================================
    # --- 公变客户 (13条) ---
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042207356622",
        "reading_month": "2026-02",
        "fwd_total": 1076.68, "fwd_sharp_peak": 241.35, "fwd_peak": 308.29,
        "fwd_flat": 476.10, "fwd_valley": 50.93,
        "rev_total": 156.39, "rev_sharp_peak": 25.54, "rev_peak": 25.08,
        "rev_flat": 95.17, "rev_valley": 10.58,
        "description": "华尔特 公变 09001SF00000042207356622",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042207356623",
        "reading_month": "2026-02",
        "fwd_total": 1620.91, "fwd_sharp_peak": 370.55, "fwd_peak": 475.16,
        "fwd_flat": 667.70, "fwd_valley": 107.48,
        "rev_total": 176.99, "rev_sharp_peak": 40.86, "rev_peak": 40.17,
        "rev_flat": 84.44, "rev_valley": 11.50,
        "description": "华尔特 公变 09001SF00000042207356623",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042207356624",
        "reading_month": "2026-02",
        "fwd_total": 1410.05, "fwd_sharp_peak": 36.71, "fwd_peak": 165.14,
        "fwd_flat": 629.49, "fwd_valley": 578.70,
        "rev_total": 838.66, "rev_sharp_peak": 221.49, "rev_peak": 231.28,
        "rev_flat": 382.59, "rev_valley": 3.29,
        "description": "华尔特 公变 09001SF00000042207356624",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042207356625",
        "reading_month": "2026-02",
        "fwd_total": 371.47, "fwd_sharp_peak": 31.14, "fwd_peak": 69.35,
        "fwd_flat": 172.66, "fwd_valley": 98.30,
        "rev_total": 1001.56, "rev_sharp_peak": 260.07, "rev_peak": 248.52,
        "rev_flat": 463.97, "rev_valley": 28.98,
        "description": "华尔特 公变 09001SF00000042207356625",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942178",
        "reading_month": "2026-02",
        "fwd_total": 408.23, "fwd_sharp_peak": 135.90, "fwd_peak": 144.04,
        "fwd_flat": 117.98, "fwd_valley": 10.30,
        "rev_total": 66.69, "rev_sharp_peak": 9.16, "rev_peak": 9.57,
        "rev_flat": 46.44, "rev_valley": 1.50,
        "description": "华尔特 公变 09001SF00000042508942178",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942190",
        "reading_month": "2026-02",
        "fwd_total": 766.83, "fwd_sharp_peak": 103.04, "fwd_peak": 141.72,
        "fwd_flat": 289.62, "fwd_valley": 232.44,
        "rev_total": 45.95, "rev_sharp_peak": 7.58, "rev_peak": 6.95,
        "rev_flat": 31.26, "rev_valley": 0.14,
        "description": "华尔特 公变 09001SF00000042508942190",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942191",
        "reading_month": "2026-02",
        "fwd_total": 21.86, "fwd_sharp_peak": 2.09, "fwd_peak": 6.67,
        "fwd_flat": 9.59, "fwd_valley": 3.49,
        "rev_total": 193.30, "rev_sharp_peak": 50.93, "rev_peak": 49.56,
        "rev_flat": 90.47, "rev_valley": 2.32,
        "description": "华尔特 公变 09001SF00000042508942191",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942193",
        "reading_month": "2026-02",
        "fwd_total": 266.66, "fwd_sharp_peak": 55.41, "fwd_peak": 74.91,
        "fwd_flat": 121.56, "fwd_valley": 14.78,
        "rev_total": 146.24, "rev_sharp_peak": 36.62, "rev_peak": 33.62,
        "rev_flat": 74.64, "rev_valley": 1.35,
        "description": "华尔特 公变 09001SF00000042508942193",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942212",
        "reading_month": "2026-02",
        "fwd_total": 226.12, "fwd_sharp_peak": 41.83, "fwd_peak": 58.31,
        "fwd_flat": 86.90, "fwd_valley": 39.07,
        "rev_total": 86.39, "rev_sharp_peak": 18.17, "rev_peak": 16.74,
        "rev_flat": 50.54, "rev_valley": 0.92,
        "description": "华尔特 公变 09001SF00000042508942212",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942222",
        "reading_month": "2026-02",
        "fwd_total": 461.04, "fwd_sharp_peak": 63.43, "fwd_peak": 89.08,
        "fwd_flat": 168.17, "fwd_valley": 140.35,
        "rev_total": 153.71, "rev_sharp_peak": 33.20, "rev_peak": 30.56,
        "rev_flat": 89.85, "rev_valley": 0.09,
        "description": "华尔特 公变 09001SF00000042508942222",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942223",
        "reading_month": "2026-02",
        "fwd_total": 188.90, "fwd_sharp_peak": 47.50, "fwd_peak": 61.70,
        "fwd_flat": 68.41, "fwd_valley": 11.27,
        "rev_total": 19.33, "rev_sharp_peak": 3.49, "rev_peak": 3.44,
        "rev_flat": 12.03, "rev_valley": 0.36,
        "description": "华尔特 公变 09001SF00000042508942223",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942224",
        "reading_month": "2026-02",
        "fwd_total": 673.75, "fwd_sharp_peak": 78.10, "fwd_peak": 115.78,
        "fwd_flat": 242.06, "fwd_valley": 237.79,
        "rev_total": 76.29, "rev_sharp_peak": 18.72, "rev_peak": 11.07,
        "rev_flat": 46.47, "rev_valley": 0.01,
        "description": "华尔特 公变 09001SF00000042508942224",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942225",
        "reading_month": "2026-02",
        "fwd_total": 24.53, "fwd_sharp_peak": 5.09, "fwd_peak": 6.05,
        "fwd_flat": 10.90, "fwd_valley": 2.47,
        "rev_total": 332.39, "rev_sharp_peak": 96.45, "rev_peak": 89.54,
        "rev_flat": 143.52, "rev_valley": 2.86,
        "description": "华尔特 公变 09001SF00000042508942225",
    },
    # --- 地方电厂户 (2条) ---
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042408595672",
        "reading_month": "2026-02",
        "fwd_total": 906.69, "fwd_sharp_peak": 230.01, "fwd_peak": 234.41,
        "fwd_flat": 410.87, "fwd_valley": 31.40,
        "rev_total": 0.33, "rev_sharp_peak": 0.0, "rev_peak": 0.04,
        "rev_flat": 0.13, "rev_valley": 0.16,
        "description": "华尔特 地方电厂 09001SF00000042408595672",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042408595673",
        "reading_month": "2026-02",
        "fwd_total": 1378.73, "fwd_sharp_peak": 363.93, "fwd_peak": 357.39,
        "fwd_flat": 613.76, "fwd_valley": 43.62,
        "rev_total": 0.34, "rev_sharp_peak": 0.0, "rev_peak": 0.08,
        "rev_flat": 0.13, "rev_valley": 0.11,
        "description": "华尔特 地方电厂 09001SF00000042408595673",
    },
    # --- 光伏发电客户 (11条) ---
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042408595704",
        "reading_month": "2026-02",
        "fwd_total": 1331.19, "fwd_sharp_peak": 361.24, "fwd_peak": 345.87,
        "fwd_flat": 580.72, "fwd_valley": 43.34,
        "rev_total": 0.36, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.15, "rev_valley": 0.14,
        "description": "华尔特 光伏发电 09001SF00000042408595704",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042408595705",
        "reading_month": "2026-02",
        "fwd_total": 1486.62, "fwd_sharp_peak": 398.77, "fwd_peak": 382.91,
        "fwd_flat": 656.04, "fwd_valley": 48.88,
        "rev_total": 0.42, "rev_sharp_peak": 0.0, "rev_peak": 0.08,
        "rev_flat": 0.17, "rev_valley": 0.16,
        "description": "华尔特 光伏发电 09001SF00000042408595705",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942175",
        "reading_month": "2026-02",
        "fwd_total": 400.71, "fwd_sharp_peak": 100.93, "fwd_peak": 100.15,
        "fwd_flat": 195.01, "fwd_valley": 4.61,
        "rev_total": 0.20, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508942175",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942176",
        "reading_month": "2026-02",
        "fwd_total": 435.15, "fwd_sharp_peak": 118.77, "fwd_peak": 113.40,
        "fwd_flat": 198.02, "fwd_valley": 4.95,
        "rev_total": 0.20, "rev_sharp_peak": 0.0, "rev_peak": 0.05,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508942176",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942177",
        "reading_month": "2026-02",
        "fwd_total": 336.26, "fwd_sharp_peak": 97.80, "fwd_peak": 90.53,
        "fwd_flat": 144.91, "fwd_valley": 3.00,
        "rev_total": 0.21, "rev_sharp_peak": 0.0, "rev_peak": 0.07,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508942177",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942194",
        "reading_month": "2026-02",
        "fwd_total": 404.97, "fwd_sharp_peak": 106.88, "fwd_peak": 103.75,
        "fwd_flat": 190.25, "fwd_valley": 4.07,
        "rev_total": 0.28, "rev_sharp_peak": 0.0, "rev_peak": 0.08,
        "rev_flat": 0.08, "rev_valley": 0.11,
        "description": "华尔特 光伏发电 09001SF00000042508942194",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942195",
        "reading_month": "2026-02",
        "fwd_total": 386.25, "fwd_sharp_peak": 106.01, "fwd_peak": 99.85,
        "fwd_flat": 175.90, "fwd_valley": 4.48,
        "rev_total": 0.22, "rev_sharp_peak": 0.0, "rev_peak": 0.07,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508942195",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508942196",
        "reading_month": "2026-02",
        "fwd_total": 377.96, "fwd_sharp_peak": 101.48, "fwd_peak": 97.84,
        "fwd_flat": 174.04, "fwd_valley": 4.59,
        "rev_total": 0.21, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508942196",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042508943351",
        "reading_month": "2026-02",
        "fwd_total": 366.68, "fwd_sharp_peak": 101.42, "fwd_peak": 95.44,
        "fwd_flat": 165.76, "fwd_valley": 4.05,
        "rev_total": 0.21, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特 光伏发电 09001SF00000042508943351",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042509164565",
        "reading_month": "2026-02",
        "fwd_total": 7074.07, "fwd_sharp_peak": 1846.27, "fwd_peak": 1849.74,
        "fwd_flat": 3291.94, "fwd_valley": 86.11,
        "rev_total": 6.16, "rev_sharp_peak": 0.0, "rev_peak": 1.11,
        "rev_flat": 2.0, "rev_valley": 3.04,
        "description": "华尔特 光伏发电 09001SF00000042509164565",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "09001SF00000042509164566",
        "reading_month": "2026-02",
        "fwd_total": 5468.16, "fwd_sharp_peak": 1430.97, "fwd_peak": 1403.80,
        "fwd_flat": 2571.47, "fwd_valley": 61.91,
        "rev_total": 6.91, "rev_sharp_peak": 0.0, "rev_peak": 2.21,
        "rev_flat": 1.88, "rev_valley": 2.81,
        "description": "华尔特 光伏发电 09001SF00000042509164566",
    },
]


# ============================================================
# 比较逻辑
# ============================================================

READING_FIELDS = [
    ("fwd_total", "total_kwh"),
    ("fwd_sharp_peak", "sharp_peak"),
    ("fwd_peak", "peak"),
    ("fwd_flat", "flat"),
    ("fwd_valley", "valley"),
    ("rev_total", "rev_total"),
    ("rev_sharp_peak", "rev_sharp_peak"),
    ("rev_peak", "rev_peak"),
    ("rev_flat", "rev_flat"),
    ("rev_valley", "rev_valley"),
]


def compare_values(llm_val, prog_val, field: str) -> tuple[bool, str]:
    """比较 LLM 值和程序值。"""
    if llm_val is None and prog_val is None:
        return True, ""
    if llm_val is None and prog_val is not None:
        return False, f"  {field}: LLM=None, 程序={prog_val}"
    if llm_val is not None and prog_val is None:
        return False, f"  {field}: LLM={llm_val}, 程序=None（缺失）"
    try:
        lv, pv = float(llm_val), float(prog_val)
        if abs(lv - pv) <= TOLERANCE:
            return True, ""
        return False, f"  {field}: LLM={lv}, 程序={pv}, 差={abs(lv - pv):.4f}"
    except (ValueError, TypeError):
        return False, f"  {field}: LLM={llm_val}, 程序={prog_val}（类型不匹配）"


def find_program_record(prog_records: list[dict], fixture: dict) -> dict | None:
    """在程序结果中找到匹配的记录。"""
    month = fixture["reading_month"]
    meter = fixture["meter_number"]
    for r in prog_records:
        if r["meter_number"] == meter and r["reading_month"] == month:
            return r
    return None


def run_tests():
    """运行全部测试。"""
    # 按文件分组加载
    files_to_test = {}
    for f in LLM_FIXTURES:
        fp = f["file_path"]
        if fp not in files_to_test:
            files_to_test[fp] = f["file"]

    # 逐文件提取程序结果
    dispatcher = SmartDispatcher()
    prog_results = {}  # file_path -> list[dict]

    for fpath, fname in files_to_test.items():
        full_path = ROOT / fpath
        if not full_path.exists():
            print(f"[WARN] 文件不存在: {fpath}")
            continue
        sheets = dispatcher.load_as_dataframes(str(full_path))
        if not sheets:
            continue
        extractor = MultiPassExtractor()
        extractor.load_dataframes(sheets)
        records = extractor.extract_all()
        prog_results[fpath] = [
            r for r in records
            if r.get("reading_month") != "unknown"
        ]

    # 逐条对比
    total = 0
    passed = 0
    failed = 0
    failed_details = []

    print(f"\n{'='*80}")
    print("表码数据校验：大模型提取 vs 程序提取")
    print(f"{'='*80}\n")

    for fixture in LLM_FIXTURES:
        total += 1
        fpath = fixture["file_path"]
        desc = fixture.get("description", "")

        if fpath not in prog_results:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  文件 {fpath} 程序未提取到结果\n")
            failed_details.append(f"{desc}: 文件未提取")
            continue

        match = find_program_record(prog_results[fpath], fixture)
        if not match:
            failed += 1
            meter = fixture["meter_number"]
            print(f"[FAIL] {desc}")
            print(f"  未找到匹配记录: {meter} / {fixture['reading_month']}")
            print(f"  程序提取到的记录:")
            for r in prog_results[fpath]:
                print(f"    {r['meter_number']} {r['reading_month']}: "
                      f"正总={r.get('total_kwh')}, 反总={r.get('rev_total')}")
            print()
            failed_details.append(f"{desc}: 未找到匹配记录")
            continue

        # 逐字段对比
        mismatches = []
        for llm_field, prog_field in READING_FIELDS:
            ok, detail = compare_values(fixture.get(llm_field), match.get(prog_field), llm_field)
            if not ok:
                mismatches.append(detail)

        if mismatches:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  电表: {match['meter_number']}, 月份: {match['reading_month']}")
            for m in mismatches:
                print(m)
            print()
            failed_details.append(f"{desc}: {len(mismatches)} 个字段不匹配")
        else:
            passed += 1
            # 打印双方数据以供人工确认
            print(f"[PASS] {desc}")
            print(f"  LLM: 正={fixture['fwd_total']}/{fixture['fwd_sharp_peak']}/{fixture['fwd_peak']}/{fixture['fwd_flat']}/{fixture['fwd_valley']}"
                  f" 反={fixture['rev_total']}/{fixture['rev_sharp_peak']}/{fixture['rev_peak']}/{fixture['rev_flat']}/{fixture['rev_valley']}")
            print(f"  程序: 正={match.get('total_kwh')}/{match.get('sharp_peak')}/{match.get('peak')}/{match.get('flat')}/{match.get('valley')}"
                  f" 反={match.get('rev_total')}/{match.get('rev_sharp_peak')}/{match.get('rev_peak')}/{match.get('rev_flat')}/{match.get('rev_valley')}")

    # 汇总
    print(f"\n{'='*80}")
    print(f"汇总: {total} 条测试, {passed} 通过, {failed} 失败")
    print(f"{'='*80}")

    if failed_details:
        print("\n失败详情:")
        for d in failed_details:
            print(f"  - {d}")

    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
