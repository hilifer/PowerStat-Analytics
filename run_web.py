#!/usr/bin/env python3
"""启动 PowerStat-Analytics Web 服务。

用法:
    python run_web.py                    # 默认 0.0.0.0:5000
    python run_web.py --port 8080        # 指定端口
    python run_web.py --host 127.0.0.1   # 仅本地访问
"""

import os, sys
from pathlib import Path

# 确保 tesseract/pdftoppm 可用
os.environ["TESSERACT_CMD"] = "/usr/bin/tesseract"
os.environ["PATH"] = "/usr/bin:" + os.environ.get("PATH", "")

sys.path.insert(0, str(Path(__file__).parent))

import click
from src.web.app import create_app


@click.command()
@click.option("--host", default="0.0.0.0", help="监听地址")
@click.option("--port", default=5000, type=int, help="监听端口")
@click.option("--debug", is_flag=True, help="调试模式")
def main(host, port, debug):
    """启动 PowerStat Web 服务"""
    app = create_app()
    print(f"\n  PowerStat-Analytics Web 服务启动")
    print(f"  地址: http://{host}:{port}")
    print(f"  按 Ctrl+C 停止\n")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
