#!/usr/bin/env python3
"""PowerStat-Analytics CLI 入口。

用法:
    python main.py fetch         # 抓取邮件并完整处理
    python main.py local         # 处理本地已下载的附件
    python main.py export        # 导出 CSV
    python main.py info          # 查看数据库统计
    python main.py diagnose      # 诊断邮箱连接和邮件匹配情况
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


@cli.command()
def diagnose():
    """诊断邮箱连接、邮件过滤、去重状态，排查新邮件未被处理的原因。"""
    import email as email_mod
    from src.email_fetcher.fetcher import (
        EmailFetcher, _decode_header_value, _matches_filter
    )

    console.print("[bold yellow]===== 邮箱诊断 =====[/]\n")

    # 1. 检查配置
    email_cfg = config.get("email")
    account = email_cfg.get("account", "")
    filter_cfg = email_cfg.get("filter", {})
    console.print(f"[bold]邮箱账号:[/] {account}")
    console.print(f"[bold]发件人过滤:[/] {filter_cfg.get('sender_keywords', [])}")
    console.print(f"[bold]主题过滤:[/] {filter_cfg.get('subject_keywords', [])}")

    if not account or account.startswith("${"):
        console.print("[bold red]错误: .env 文件未配置邮箱凭据！[/]")
        return

    # 2. 连接邮箱
    console.print("\n[bold]连接邮箱...[/]")
    fetcher = EmailFetcher()
    try:
        fetcher.connect()
        console.print("[green]连接成功[/]")
    except Exception as e:
        console.print(f"[bold red]连接失败: {e}[/]")
        return

    try:
        fetcher._conn.select("INBOX")

        # 3. 搜索邮件
        criteria = fetcher._build_search_criteria()
        console.print(f"\n[bold]IMAP 搜索条件:[/] {criteria}")
        status, msg_ids = fetcher._imap_search_utf8(criteria)
        ids = msg_ids[0].split() if msg_ids[0] else []
        console.print(f"[bold]服务端返回:[/] {len(ids)} 封邮件")

        # 4. 检查已处理记录
        import sqlite3
        db_path = config.get("storage", "database", default="output/data/powerstat.db")
        processed_count = 0
        try:
            with sqlite3.connect(db_path) as conn:
                processed_count = conn.execute(
                    "SELECT COUNT(*) FROM processed_emails"
                ).fetchone()[0]
        except Exception:
            pass
        console.print(f"[bold]已处理指纹数:[/] {processed_count}")

        # 5. 逐封检查最近 20 封邮件
        console.print(f"\n[bold yellow]--- 最近 {min(20, len(ids))} 封邮件的匹配情况 ---[/]")

        table = Table(show_lines=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("日期", width=12)
        table.add_column("发件人", width=25)
        table.add_column("主题", width=35)
        table.add_column("过滤", width=6)
        table.add_column("去重", width=6)
        table.add_column("状态", width=10)

        for i, mid in enumerate(ids[-20:], 1):
            try:
                st, data = fetcher._conn.fetch(mid, "(RFC822)")
                if st != "OK":
                    continue
                msg = email_mod.message_from_bytes(data[0][1])
                subj = _decode_header_value(msg.get("Subject", ""))
                sender = _decode_header_value(msg.get("From", ""))
                date_str = msg.get("Date", "")[:16]

                matched = _matches_filter(msg, filter_cfg)
                fp = fetcher._compute_fingerprint(msg)
                is_dup = fetcher._is_email_processed(fp)

                filter_icon = "[green]通过[/]" if matched else "[red]拒绝[/]"
                dup_icon = "[yellow]已处理[/]" if is_dup else "[green]新邮件[/]"
                if matched and not is_dup:
                    status_str = "[bold green]待处理[/]"
                elif matched and is_dup:
                    status_str = "[dim]已完成[/]"
                else:
                    status_str = "[red]被过滤[/]"

                table.add_row(
                    str(i), date_str, sender[:25], subj[:35],
                    filter_icon, dup_icon, status_str
                )
            except Exception as e:
                table.add_row(str(i), "?", "?", f"读取失败: {e}", "?", "?", "?")

        console.print(table)

        # 6. 给出诊断建议
        console.print("\n[bold yellow]--- 诊断建议 ---[/]")
        console.print("• 如果新邮件 [bold]过滤=拒绝[/]：检查发件人是否匹配 sender_keywords，主题是否包含 subject_keywords")
        console.print("• 如果新邮件 [bold]去重=已处理[/]：邮件之前已下载过，processed_emails 表中有记录")
        console.print("• 如果新邮件显示 [bold green]待处理[/]：说明过滤和去重都没问题，再次运行 fetch 即可处理")
        console.print("• 如果完全看不到新邮件：检查新邮件的发件人地址是否在 sender_keywords 列表中")

    finally:
        fetcher.disconnect()


if __name__ == "__main__":
    cli()
