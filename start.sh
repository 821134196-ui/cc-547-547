#!/usr/bin/env bash
# 单条本地命令启动：./start.sh [--reseed]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m app --port "${PORT:-8000}" "$@"
