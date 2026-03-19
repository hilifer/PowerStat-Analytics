"""数据归档模块：按月份和项目组织文件目录结构。

目录结构:
    archive_root/
    ├── 2024-01/
    │   ├── 项目A/
    │   │   ├── 附件文件...
    │   │   └── summary.csv
    │   └── 项目B/
    │       └── ...
    └── 2024-02/
        └── ...
"""

import csv
import shutil
from pathlib import Path

from src.config_loader import config
from src.data.models import Database
from src.logger import log


class Archiver:
    """数据归档管理器。"""

    def __init__(self, db: Database):
        self.db = db
        self.archive_root = Path(
            config.get("storage", "archive_root", default="output/archive")
        )

    def archive_attachment(self, filepath: str, reading_month: str,
                           project_name: str = None) -> str:
        """将附件文件归档到对应的月份/项目目录，返回归档路径。"""
        src = Path(filepath)
        if not src.exists():
            log.warning("归档源文件不存在: %s", filepath)
            return ""

        month_dir = self.archive_root / reading_month
        if project_name:
            dest_dir = month_dir / self._safe_dirname(project_name)
        else:
            dest_dir = month_dir / "_未分类"

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        # 处理重名
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1

        shutil.copy2(str(src), str(dest))
        log.info("归档: %s -> %s", src.name, dest)
        return str(dest)

    def generate_monthly_summaries(self):
        """为每个月份/项目生成汇总 CSV。"""
        bills = self.db.get_monthly_bill()
        if not bills:
            log.info("无账单数据，跳过汇总生成")
            return

        # 按月份和项目分组
        groups = {}
        for bill in bills:
            month = bill.get("reading_month", "unknown")
            project = bill.get("project_name") or "_未分类"
            key = (month, project)
            groups.setdefault(key, []).append(bill)

        for (month, project), records in groups.items():
            dest_dir = self.archive_root / month / self._safe_dirname(project)
            dest_dir.mkdir(parents=True, exist_ok=True)

            csv_path = dest_dir / "summary.csv"
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                if records:
                    writer = csv.DictWriter(f, fieldnames=records[0].keys())
                    writer.writeheader()
                    writer.writerows(records)

            log.info("汇总 CSV: %s (%d 条)", csv_path, len(records))

    def _safe_dirname(self, name: str) -> str:
        """清理目录名，保留中文和常用字符。"""
        import re
        # 只去掉文件系统非法字符，保留中文、字母、数字、常用符号
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        cleaned = cleaned.strip('. _')
        return cleaned[:100] if cleaned else "_未命名"
