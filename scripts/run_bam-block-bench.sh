#!/bin/bash

set -e

script_dir=$(dirname $(realpath $0))
cd "${script_dir}/.."

num_threads=64
num_queues=8
page_size=512

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -j, --num-threads NUM      Number of threads (default: $num_threads)"
    echo "  -q, --num-queues NUM       Number of queues (default: $num_queues)"
    echo "  -l, --block-size NUM       Page size (default: $page_size)"
    echo "  -h, --help                 Display this help message"
    exit 1
}

OPTIONS="j:q:l:h"
LONGOPTS="num-threads:,num-queues:,block-size:,help"

PARSED_ARGS=$(getopt -o $OPTIONS --long "$LONGOPTS" --name "$0" -- "$@")
if [[ $? -ne 0 ]]; then exit 1; fi
eval set -- "$PARSED_ARGS"

while true; do
    case "$1" in
        -j|--num-threads) num_threads="$2"; shift 2 ;;
        -q|--num-queues)  num_queues="$2"; shift 2 ;;
        -l|--block-size)  page_size="$2"; shift 2 ;;
        -h|--help)        usage ;;
        --) shift; break ;;
        *) echo "Internal error!"; exit 1 ;;
    esac
done

# Environment overrides change workload geometry, not the author's GPU kernel.
cache_size_bytes=$(numfmt --from=iec "${BAM_CACHE_SIZE:-32G}")
num_pages=$((cache_size_bytes / page_size))
data_size_bytes=$(numfmt --from=iec "${BAM_DATA_SIZE:-112G}")
num_blocks=$((data_size_bytes / page_size))

set -x

export BAM_SWARMIO_FAULT_EXIT=1
sudo --preserve-env=BAM_SWARMIO_FAULT_EXIT bam/Release/bin/nvm-block-bench \
    --threads=$num_threads --blk_size=64 --reqs="${BAM_REQS:-32}" \
    --pages=$num_pages --page_size=$page_size \
    --num_blks=$num_blocks \
    --queue_depth="${BAM_QUEUE_DEPTH:-1024}" --num_queues=$num_queues \
    --gpu="${BAM_GPU:-0}" --n_ctrls=1 \
    --random="${BAM_RANDOM:-true}"

set +x
