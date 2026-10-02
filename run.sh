#!/usr/bin/env bash
# 单条本地命令启动装修工程结算台
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m renovation "$@"
