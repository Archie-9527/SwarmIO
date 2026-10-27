#!/usr/bin/env bash
# 单轮随机读加压：复用统一入口的 GPU 选择、产物校验、日志和失败处理。
# 参数：构建目录 线程数 每线程请求数 队列数 每队列深度 [监控间隔秒]。
set -euo pipefail

usage() {
    echo "用法：sudo bash scripts/run_random_read.sh [build_dir [threads [reqs [queues [depth [monitor_interval]]]]]]"
    echo "默认：build 1024 8 8 512；512 B 随机读，共 8192 次逻辑操作。"
    echo "可选第六参数启用资源与吞吐采样，例如 0.1 表示每 100 ms 打点；默认不启用。"
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
fi
if (( $# > 6 )); then
    usage >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo "请使用 sudo bash scripts/run_random_read.sh；需要访问 BaM 设备及内核日志。" >&2
    exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd -- "$script_dir/.."
build_dir=${1:-build}
threads=${2:-1024}
reqs=${3:-8}
queues=${4:-8}
depth=${5:-512}

# 只执行一次；失败或中断后不重新加载、不重试、不自动卸载。
monitor_args=()
if [[ -n ${6:-} ]]; then
    monitor_args=(--monitor-interval "$6")
fi
exec python3 "$script_dir/swarmio_bam.py" --build-dir "$build_dir" run \
    --random --access read \
    --threads "$threads" --reqs "$reqs" \
    --num-queues "$queues" --queue-depth "$depth" "${monitor_args[@]}"
