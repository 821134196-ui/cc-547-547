"""单命令启动：python3 -m renovation [--port 8000] [--no-browser] [--reseed]

首次启动自动生成「部分验收」样例数据（SQLite + 本地现场照片）。
"""
import argparse
import shutil
from pathlib import Path

from .seed import seed
from .web import DATA_DIR, main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="装修工程结算台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--reseed", action="store_true", help="删除 data/ 并重新生成样例")
    args = ap.parse_args()

    if args.reseed and Path(DATA_DIR).exists():
        shutil.rmtree(DATA_DIR)
    seed(DATA_DIR) if args.reseed or not (Path(DATA_DIR) / "renovation.db").exists() else None
    main(host=args.host, port=args.port, open_browser=not args.no_browser)
