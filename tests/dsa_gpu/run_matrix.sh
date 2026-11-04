#!/usr/bin/env bash
# 从本脚本所在目录调用；任意一次校验/设备错误都会停止，不吞掉失败。
set -euo pipefail
cd -- "$(dirname -- "$0")"
for size in 512 4096; do
    for mode in sync batch; do
        for direction in read write mixed; do
            ./dsa-gpu-bench --size "$size" --mode "$mode" \
                --direction "$direction" --batch 32 --depth 4 "$@"
        done
    done
done
