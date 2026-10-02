"""命令行入口：python -m app [--port 8000] [--reseed]"""
from __future__ import annotations

import argparse

from .server import serve


def main():
    ap = argparse.ArgumentParser(description="装修工程结算系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/settlement.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reseed", action="store_true", help="清空并重新生成样例数据")
    args = ap.parse_args()

    if args.reseed:
        import os
        import shutil
        for f in (args.db, args.db + "-wal", args.db + "-shm"):
            if os.path.exists(f):
                os.remove(f)
        if os.path.isdir(args.photos):
            shutil.rmtree(args.photos)

    httpd = serve(args.db, args.photos, args.host, args.port, reseed=args.reseed)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
