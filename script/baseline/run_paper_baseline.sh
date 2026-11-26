#!/usr/bin/env bash
# Run from the SwarmIO project directory. No implicit change of directory.
set -euo pipefail
if [[ ! -f Kbuild || ! -f script/baseline/paper_baseline.py ]]; then
    printf '请在 SwarmIO 项目目录执行本脚本。\n' >&2
    exit 2
fi
exec python3 script/baseline/paper_baseline.py "$@"
