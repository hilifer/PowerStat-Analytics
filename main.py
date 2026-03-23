#!/usr/bin/env python3
"""PowerStat-Analytics CLI 入口。

用法:
    python main.py fetch         # 抓取邮件并完整处理
    python main.py local         # 处理本地已下载的附件
    python main.py export        # 导出 CSV
    python main.py info          # 查看数据库统计
"""

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent))

import click
from rich.console import Console
from rich.table import Table

from src.config_loader import config
from src.data.models import Database
from src.pipeline import Pipeline

console = Console()


@click.group()
@click.option("--config", "-c", "config_path", default=None, help="配置文件路径")
def cli(config_path):
    """PowerStat-Analytics: 电费数据自动化采集与解析系统"""
    config.load(config_path)


@cli.command()
def fetch():
    """从 QQ 邮箱抓取电费邮件，解析附件，入库归档并生成图表。"""
    console.print("[bold green]启动完整处理流程...[/]")
    pipeline = Pipeline()
    pipeline.run_full(skip_fetch=False)
    console.print("[bold green]完成！[/]")


@cli.command()
def local():
    """处理本地已下载的附件（跳过邮件抓取）。"""
    console.print("[bold cyan]处理本地附件...[/]")
    pipeline = Pipeline()
    pipeline.run_full(skip_fetch=True)
    console.print("[bold green]完成！[/]")


@cli.command()
def export():
    """导出所有数据为 CSV 文件。"""
    db = Database()
    db.export_csv()
    console.print(f"[bold green]CSV 已导出到 {config.get('storage', 'csv_export_dir')}[/]")


@cli.command()
def info():
    """显示数据库统计信息。"""
    db = Database()

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

    table = Table(title="数据库统计", show_lines=True)
    table.add_column("指标", style="bold")
    table.add_column("值", justify="right")

    table.add_row("电表数量", str(meter_count))
    table.add_row("抄表记录数", str(reading_count))
    table.add_row("单价记录数", str(price_count))
    table.add_row("项目数量", str(project_count))
    table.add_row("用户编号数", str(user_count))
    if month_range[0]:
        table.add_row("数据范围", f"{month_range[0]} ~ {month_range[1]}")

    console.print(table)

    # 展示项目列表
    projects = db.get_projects()
    if projects:
        console.print("\n[bold]项目列表:[/]")
        for p in projects:
            users = db.get_user_ids(project_name=p)
            meters = db.get_meters(project_name=p)
            console.print(f"  - {p}: {len(meters)} 块电表, {len(users)} 个用户")


if __name__ == "__main__":
    cli()
