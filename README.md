# PowerStat-Analytics

光伏电站电费数据自动化采集、解析与管理系统。

从 QQ 邮箱自动抓取电费相关邮件附件，智能解析 Excel / PDF / 图片等多种格式，以电表为核心组织数据，自动计算电费账单。

## 系统架构

```
QQ邮箱 (IMAP)
    │
    ▼
邮件附件下载 ──→ 文件类型检测（魔数 + 扩展名）
    │
    ├── Excel / PDF / CSV / HTML ──→ DataFrame 加载
    │       │                           │
    │       │                    MultiPassExtractor（6轮扫描）
    │       │                           │
    │       │                    提取电表档案 + 抄表读数
    │       │                           │
    │       ▼                           ▼
    │   meters 表              monthly_readings 表
    │   （电表号、用户编号、        （月份、尖/峰/平/谷
    │     资产号、倍率、项目）        用电量、表码数据）
    │
    ├── 图片（JPG/PNG/BMP）──→ OCR 引擎（RapidOCR）
    │                               │
    │                        提取电价单价 + 用户编号
    │                               │
    │                               ▼
    │                       price_records 表
    │                       （尖/峰/平/谷单价）
    │
    └── ZIP / RAR ──→ 解压后重新入队处理

                        │
                        ▼
              数据关联（user_id 匹配）
                        │
                        ▼
              v_monthly_bill 视图
              用电量 × 倍率 × 单价 × 折扣 = 电费
```

## 数据分工

| 数据来源 | 提取内容 | 存储位置 |
|---------|---------|---------|
| Excel / PDF 表格 | 电表档案（表号、用户编号、资产号、倍率、项目名） | `meters` 表 |
| Excel / PDF 表格 | 月度抄表读数（正向/反向 × 尖/峰/平/谷，共10个读数 + 日期） | `monthly_readings` 表 |
| 图片 OCR | 分时电价（尖/峰/平/谷单价）、用户编号、月份 | `price_records` 表 |

电价通过 `user_id` 与电表关联，账单视图 `v_monthly_bill` 自动关联用量和单价计算金额。

## 快速开始

### 安装

```bash
# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置邮箱凭据
cp .env.example .env
vim .env  # 填写 EMAIL_ACCOUNT 和 EMAIL_AUTH_CODE
```

### Web 界面（推荐）

```bash
python run_web.py --port 5000
# 浏览器打开 http://localhost:5000
```

启动后可在页面上直接操作，无需命令行。首页提供「增量更新」和「全部更新」按钮。

### 命令行

```bash
python main.py fetch    # 抓取邮件并处理
python main.py local    # 处理本地附件（不抓邮件）
python main.py export   # 导出 CSV
python main.py info     # 查看统计信息
```

### Docker

```bash
docker build -t powerstat .
docker run -v $(pwd)/output:/app/output -v $(pwd)/.env:/app/.env powerstat fetch
```

## Web 界面功能

| 页面 | 路径 | 功能 |
|------|------|------|
| 仪表盘 | `/` | 数据概览、触发邮件抓取（增量/全量） |
| 电表管理 | `/meters` | 查看/编辑/新建/删除电表，按项目和用户筛选 |
| 抄表数据 | `/readings` | 月度读数查看/编辑/批量锁定 |
| 账单计算 | `/bill-calc` | 按项目/月份计算电费，编辑单价 |
| 邮件附件 | `/email-files` | 浏览已下载的附件，预览 Excel/PDF/图片 |
| 导出 | `/export` | 导出 CSV 和 Excel 文件 |

## 数据模型

```
meters（电表主表）
├── meter_number     电表号（主键）
├── user_id          用户编号（关联单价的核心键）
├── asset_number     资产编号
├── meter_type       上网表 / 发电表
├── multiplier       倍率
├── discount         折扣
├── project_name     所属项目
├── paired_meter_id  配对电表（发电表 ↔ 上网表）
├── is_locked        锁定（防止自动更新覆盖手动修改）
└── source_file      数据来源文件

monthly_readings（月度抄表读数，来自 Excel/PDF）
├── meter_id         关联电表
├── reading_month    月份（YYYY-MM）
├── sharp_peak       尖峰用电量
├── peak             峰用电量
├── flat             平用电量
├── valley           谷用电量
├── total_kwh        总用电量
├── rev_sharp_peak   反向尖峰
├── rev_peak         反向峰
├── rev_flat         反向平
├── rev_valley       反向谷
├── rev_total        反向总
├── cur_*            本月表码（尖/峰/平/谷）
├── prev_*           上月表码（尖/峰/平/谷）
└── is_locked        锁定标志

price_records（分时电价，来自图片 OCR）
├── user_id          用户编号（关联电表）
├── reading_month    月份（YYYY-MM）
├── sharp_peak_price 尖峰单价（元/度）
├── peak_price       峰单价
├── flat_price       平单价
├── valley_price     谷单价
├── average_price    综合电价（备用）
├── is_locked        锁定标志
└── source_file      图片来源

v_monthly_bill（账单视图，自动计算）
└── 各时段用电量 × 倍率 × 对应单价 × 折扣 = 各时段金额
```

## 解析引擎

### MultiPassExtractor（6轮扫描，处理 Excel/PDF）

| 轮次 | 功能 | 提取内容 |
|------|------|---------|
| Pass 1 | 电表与资产 | 电表号、资产编号 |
| Pass 2 | 电表类型 | 判定上网表/发电表 |
| Pass 3 | 倍率与折扣 | CT倍率、折扣系数 |
| Pass 4 | 用户编号 | 用户编号关联 |
| Pass 5 | 补充属性 | 项目名称、配对关系 |
| Pass 6 | 抄表读数 | 月度用电量（完整性校验：日期 + 正向5 + 反向5 = 11字段） |

### OCR 引擎（处理图片）

按优先级自动选择：RapidOCR（推荐）→ PaddleOCR → Tesseract

图片预处理：对比度增强（1.3x）→ 锐化 → 小图放大（<2000px 时 2x）

提取内容：
- 分时电价：尖峰/峰/平/谷单价（元/度）
- 用户编号：用于关联到电表
- 月份：从 OCR 文本或文件名推断

## 配置说明

所有规则在 `config/settings.yaml` 中配置：

| 配置项 | 说明 |
|--------|------|
| `email` | 邮箱 IMAP 连接和过滤规则（发件人/主题关键词） |
| `attachments` | 支持的格式、临时目录、大小限制 |
| `ocr` | OCR 引擎选择和置信度阈值 |
| `field_mapping` | 表头别名映射（40+别名 → 标准字段名） |
| `meter_type_rules` | 电表类型判定关键词（上网/发电） |
| `ocr_extraction_rules` | 电价和用户编号的正则匹配模式 |
| `reconciliation` | 数据关联补齐规则（配对传递、同源共享） |
| `storage` | 数据库路径、CSV 导出路径、归档目录 |
| `logging` | 日志级别、文件轮转 |

邮箱凭据通过 `.env` 文件配置（不入库）：

```
EMAIL_ACCOUNT=your_email@qq.com
EMAIL_AUTH_CODE=your_imap_auth_code
```

## 目录结构

```
PowerStat-Analytics/
├── main.py                 # CLI 入口
├── run_web.py              # Web 服务启动
├── config/
│   └── settings.yaml       # 配置文件
├── src/
│   ├── pipeline.py         # 处理管线（文件分发 + 流程编排）
│   ├── config_loader.py    # 配置加载
│   ├── logger.py           # 日志
│   ├── parsers/
│   │   ├── multi_pass.py   # 6轮扫描提取引擎
│   │   ├── text_extractor.py  # 文本解析
│   │   └── validators.py   # 数据校验
│   ├── ocr/
│   │   └── ocr_engine.py   # OCR 引擎（电价提取）
│   ├── data/
│   │   └── models.py       # SQLite 数据库模型
│   ├── email_fetcher/
│   │   └── fetcher.py      # IMAP 邮件抓取
│   ├── archive/
│   │   └── archiver.py     # 附件归档
│   └── web/
│       ├── app.py          # Flask Web 应用
│       └── bill_export.py  # Excel 导出
├── templates/              # Jinja2 页面模板
├── static/                 # CSS / JS 静态资源
├── output/
│   ├── data/               # 数据库 + CSV 导出
│   ├── archive/            # 按 月份/项目 归档的附件
│   └── temp_attachments/   # 邮件附件临时下载目录
└── logs/                   # 运行日志
```

## 依赖

- Python 3.11+
- Flask — Web 框架
- pandas / openpyxl / xlrd — Excel 解析
- pdfplumber — PDF 表格提取
- RapidOCR / PaddleOCR / Tesseract — 图片 OCR
- Pillow — 图片预处理
- IMAPClient — 邮件抓取
- click / rich — CLI 界面
