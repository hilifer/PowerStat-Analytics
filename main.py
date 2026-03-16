#!/usr/bin/env python3
"""PowerStat-Analytics CLI 入口。

用法:
    python main.py fetch         # 抓取邮件并完整处理
    python main.py local         # 处理本地已下载的附件
    python main.py viz           # 仅生成图表
    python main.py query         # 查询数据
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
from src.logger import log

console = Console()


@click.group()
@click.option("--config", "-c", "config_path", default=None, help="配置文件路径")
def cli(config_path):
    """PowerStat-Analytics: 电费数据自动化采集、解析与可视化系统"""
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
@click.option("--project", "-p", default=None, help="按项目筛选")
@click.option("--user-id", "-u", default=None, help="按用户编号筛选")
def viz(project, user_id):
    """生成可视化图表。"""
    console.print("[bold cyan]生成图表...[/]")
    db = Database()
    from src.visualization.charts import ChartGenerator
    gen = ChartGenerator(db)
    gen.generate_all(project_name=project, user_id=user_id)
    console.print(f"[bold green]图表已保存到 {config.get('visualization', 'output_dir')}[/]")


@cli.command()
@click.option("--project", "-p", default=None, help="按项目筛选")
@click.option("--user-id", "-u", default=None, help="按用户编号筛选")
@click.option("--meter", "-m", default=None, help="按电表号筛选")
@click.option("--from", "month_from", default=None, help="起始月份 (YYYY-MM)")
@click.option("--to", "month_to", default=None, help="截止月份 (YYYY-MM)")
def query(project, user_id, meter, month_from, month_to):
    """查询电费数据。"""
    db = Database()
    pipeline = Pipeline(db)
    results = pipeline.query(
        project_name=project,
        user_id=user_id,
        meter_number=meter,
        month_from=month_from,
        month_to=month_to,
    )

    if not results:
        console.print("[yellow]未查到匹配数据[/]")
        return

    table = Table(title="电费账单查询结果", show_lines=True)
    display_cols = [
        ("电表号", "meter_number"),
        ("用户编号", "user_id"),
        ("类型", "meter_type"),
        ("项目", "project_name"),
        ("月份", "reading_month"),
        ("倍率", "multiplier"),
        ("尖峰", "sharp_peak"),
        ("峰", "peak"),
        ("平", "flat"),
        ("谷", "valley"),
        ("总电量", "total_kwh"),
        ("尖峰价", "sharp_peak_price"),
        ("峰价", "peak_price"),
        ("平价", "flat_price"),
        ("谷价", "valley_price"),
        ("总金额", "total_amount"),
    ]
    for title, _ in display_cols:
        table.add_column(title, justify="right" if title not in ("电表号", "用户编号", "类型", "项目", "月份") else "left")

    for r in results:
        row = []
        for _, key in display_cols:
            val = r.get(key)
            if val is None:
                row.append("-")
            elif isinstance(val, float):
                row.append(f"{val:.2f}")
            else:
                row.append(str(val))
        table.add_row(*row)

    console.print(table)
    console.print(f"\n共 {len(results)} 条记录")


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
