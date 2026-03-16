#!/usr/bin/env python3
"""PowerStat-Analytics CLI 入口。

用法:
    python main.py fetch         # 抓取邮件并完整处理
    python main.py local         # 处理本地已下载的附件
    python main.py viz           # 仅生成图表
    python main.py query         # 查询数据
    python main.py export        # 导出 CSV
    python main.py info          # 查看数据库统计
    python main.py graph         # 知识图谱：构建、检测异常、可视化
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


@cli.command()
@click.option("--project", "-p", default=None, help="按项目筛选图谱")
@click.option("--anomalies", "-a", is_flag=True, default=False, help="仅显示异常检测结果")
@click.option("--trace", "-t", default=None, help="追溯指定电表号的完整关系链")
@click.option("--export-json", "export_json", is_flag=True, default=False, help="导出图谱为 JSON")
def graph(project, anomalies, trace, export_json):
    """知识图谱：构建关系网络、检测数据异常、可视化。"""
    db = Database()
    from src.knowledge.graph import KnowledgeGraph
    kg = KnowledgeGraph(db)

    console.print("[bold cyan]构建知识图谱...[/]")
    stats = kg.build()

    # 图谱统计
    stat_table = Table(title="知识图谱统计", show_lines=True)
    stat_table.add_column("指标", style="bold")
    stat_table.add_column("值", justify="right")

    full_stats = kg.get_stats()
    stat_table.add_row("总节点数", str(full_stats["total_nodes"]))
    stat_table.add_row("总关系数", str(full_stats["total_edges"]))
    for ntype, count in full_stats["node_types"].items():
        stat_table.add_row(f"  {ntype} 节点", str(count))
    for rtype, count in full_stats["edge_types"].items():
        stat_table.add_row(f"  {rtype} 关系", str(count))
    stat_table.add_row("连通分量数", str(full_stats["connected_components"]))
    stat_table.add_row("最大分量大小", str(full_stats["largest_component_size"]))
    stat_table.add_row("孤立节点数", str(full_stats["isolated_nodes"]))
    console.print(stat_table)

    # 追溯电表
    if trace:
        console.print(f"\n[bold]追溯电表: {trace}[/]")
        info = kg.trace_meter(trace)
        if "error" in info:
            console.print(f"[red]{info['error']}[/]")
        else:
            console.print(f"  电表号: {info['meter']}")
            console.print(f"  类型: {info.get('meter_type', '未知')}")
            console.print(f"  倍率: {info.get('multiplier', 1.0)}")
            console.print(f"  所属项目: {', '.join(info['projects']) or '无'}")
            console.print(f"  关联用户: {', '.join(info['users']) or '无'}")
            console.print(f"  读数月份: {len(info['readings'])} 条")
            for r in info["readings"]:
                console.print(f"    {r['month']}: {r['total_kwh']:.1f} kWh")
            console.print(f"  单价记录: {len(info['prices'])} 条")
        return

    # 异常检测
    console.print("\n[bold]数据异常检测...[/]")
    anomaly_result = kg.detect_anomalies()

    anomaly_names = {
        "orphan_meters": "孤立电表（缺项目/用户）",
        "missing_prices": "缺少单价记录",
        "missing_readings": "缺少读数记录",
        "reading_spikes": "电量异常突变",
        "multi_project_users": "用户跨项目关联",
    }

    total = anomaly_result["total_issues"]
    if total == 0:
        console.print("[green]未检测到数据异常[/]")
    else:
        console.print(f"[yellow]检测到 {total} 个异常[/]")
        for key, label in anomaly_names.items():
            items = anomaly_result.get(key, [])
            if items:
                console.print(f"\n  [bold red]{label} ({len(items)})[/]")
                for item in items[:10]:
                    issue = item.get("issue", str(item))
                    meter = item.get("meter", "")
                    month = item.get("month", "")
                    prefix = f"{meter} {month}" if meter else ""
                    console.print(f"    - {prefix} {issue}")
                if len(items) > 10:
                    console.print(f"    ... 还有 {len(items) - 10} 条")

    if not anomalies:
        # 项目排名
        ranking = kg.get_project_ranking()
        if ranking:
            console.print("\n")
            rank_table = Table(title="项目电量排名", show_lines=True)
            rank_table.add_column("项目", style="bold")
            rank_table.add_column("电表数", justify="right")
            rank_table.add_column("总电量 (kWh)", justify="right")
            rank_table.add_column("数据月份", justify="right")
            rank_table.add_column("时间范围")
            for r in ranking:
                rank_table.add_row(
                    r["project"], str(r["meter_count"]),
                    f"{r['total_kwh']:,.2f}", str(r["month_count"]),
                    r["month_range"])
            console.print(rank_table)

        # 导出图谱图片
        console.print("\n[bold cyan]导出知识图谱可视化...[/]")
        path = kg.export_graph_image(project_name=project)
        if path:
            console.print(f"[green]图谱已保存: {path}[/]")

    # 导出 JSON
    if export_json:
        json_path = kg.export_json()
        console.print(f"[green]JSON 已导出: {json_path}[/]")


if __name__ == "__main__":
    cli()
