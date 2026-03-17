# PowerStat-Analytics

电费数据自动化采集、解析与可视化系统。从 QQ 邮箱 IMAP 抓取电费邮件附件（Excel/PDF/图片），以**电表**为核心提取结构化数据，归档到月份/项目目录，并生成可视化图表。

## 系统架构

```
QQ邮箱(IMAP) → 附件下载 → 解析引擎 → SQLite数据库 → 归档/CSV/图表
                           ├── Excel多Sheet解析
                           ├── PDF表格/文本提取
                           └── 图片OCR(单价提取)
```

## 核心特性

- **电表中心化**：所有数据围绕电表组织，每条记录可追溯到具体电表
- **多格式解析**：Excel（含多Sheet）、PDF、图片（OCR提取单价）
- **智能字段映射**：配置化的表头别名匹配，适应不同文件的表述差异
- **项目/用户筛选**：按项目名称或用户编号精确过滤电表及历史数据
- **自动归档**：按月份→项目的目录结构组织附件和汇总CSV
- **可视化图表**：折线图（月度趋势）、柱状图（上网/发电对比）、堆叠图（时段构成）
- **高鲁棒性**：重试机制、错误隔离、UPSERT幂等写入、完善日志

## 快速开始

### Web 界面（推荐）

```bash
# 创建虚拟环境并激活
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置邮箱（编辑 .env）
cp .env.example .env   # 如已有 .env 可跳过
vim .env

# 启动 Web 服务
python run_web.py --port 5000

# 浏览器打开
# http://localhost:5000
```

> **提示**：后续每次使用前需先激活虚拟环境：`source venv/bin/activate`

启动后即可在浏览器中操作，**无需提前运行 `main.py fetch`**。
首页仪表盘提供「增量更新」和「全部更新」按钮，可直接在页面上触发邮件抓取和数据解析。

| 页面 | 路径 | 功能 |
|------|------|------|
| 仪表盘 | `/` | 数据概览、触发邮件抓取 |
| 电表管理 | `/meters` | 查看/编辑/新建/删除电表 |
| 账单查询 | `/bills` | 按项目/用户/月份筛选账单 |
| 归档浏览 | `/archive` | 按月份查看已归档附件 |
| 图表 | `/charts` | 可视化图表 |
| 导出 | `/export` | 导出 CSV 文件 |

### 命令行

```bash
# 确保已激活虚拟环境（见上方步骤）

# 抓取邮件并处理
python main.py fetch

# 或处理本地附件
python main.py local

# 查询数据
python main.py query --project "项目A" --user-id "123456"

# 生成图表
python main.py viz --project "项目A"

# 查看统计
python main.py info

# 导出CSV
python main.py export
```

## Docker 部署

```bash
docker build -t powerstat .
docker run -v $(pwd)/output:/app/output -v $(pwd)/.env:/app/.env powerstat fetch
```

## 数据模型

```
meters (电表主表)
├── meter_number   电表号
├── asset_number   资产编号
├── user_id        用户编号（关联核心键）
├── meter_type     上网表/发电表
├── multiplier     倍率
└── project_name   所属项目

monthly_readings (月度抄表)
├── reading_month  YYYY-MM
├── sharp_peak     尖峰电量
├── peak           峰电量
├── flat           平电量
├── valley         谷电量
└── total_kwh      总电量

price_records (单价，OCR提取)
├── user_id        关联用户编号
├── reading_month  YYYY-MM
├── sharp_peak/peak/flat/valley_price
└── source_file    图片来源

v_monthly_bill (账单视图，自动计算金额)
└── 电量 × 倍率 × 单价 = 金额
```

## 配置说明

所有规则均在 `config/settings.yaml` 中配置：

- **字段映射** (`field_mapping`)：表头别名 → 标准字段
- **电表类型判定** (`meter_type_rules`)：根据关键词判定上网/发电表
- **OCR提取规则** (`ocr_extraction_rules`)：正则匹配单价和用户编号
- **邮件过滤** (`email.filter`)：发件人/主题/收件人关键词

## 目录结构

```
output/
├── data/
│   ├── powerstat.db          # SQLite 数据库
│   ├── meters.csv            # 电表导出
│   ├── monthly_readings.csv  # 抄表导出
│   ├── price_records.csv     # 单价导出
│   └── monthly_bill.csv      # 账单汇总导出
├── charts/                   # 可视化图表
├── archive/                  # 按月份/项目归档
│   ├── 2024-01/
│   │   ├── 项目A/
│   │   └── 项目B/
│   └── 2024-02/
└── temp_attachments/         # 临时附件下载
```
