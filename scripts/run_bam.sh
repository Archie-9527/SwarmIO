#!/bin/bash

set -e

script_dir=$(dirname $(realpath $0))
cd "${script_dir}/.."

############ CONFIG ############
VDEV_BDF="0001:10:00.0"
############ CONFIG ############

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -i, --input-config FILE       Path to build/load config file (default: configs/defconfig.conf)"
    echo "  -j, --num-gpu-threads LIST    GPU threads list (e.g., 4096,8192)"
    echo "  -q, --num-queues NUM          # of I/O queues (e.g., 64)"
    echo "  -n, --num-service-units LIST    Dispatchers list (e.g., 1,2,4)"
    echo "  -l, --block-size NUM          I/O block size (e.g., 512)"
    echo "      --target-miops NUM        Target MIOPS (default: 10)"
    echo "      --build                   Build SwarmIO module before running"
    echo "  -h, --help                    Display this help message"
    exit 1
}

cleanup() {
    if [[ -e "/sys/bus/pci/devices/${VDEV_BDF}" ]]; then
        if lsmod | grep -q "^libnvm"; then
            scripts/load_bam.sh -u "${VDEV_BDF}" || true
        fi
        scripts/load.sh --unload || true
    fi
}

trap cleanup EXIT

OPTIONS="i:j:n:l:q:h"
LONGOPTS="input-config:,num-gpu-threads:,num-service-units:,num-queues:,block-size:,help,target-miops:,build"

PARSED_ARGS=$(getopt -o $OPTIONS --long "$LONGOPTS" --name "$0" -- "$@")
if [[ $? -ne 0 ]]; then exit 1; fi
eval set -- "$PARSED_ARGS"

num_gpu_threads_list=()
num_service_units_list=()
num_queues=128
block_size=512
target_miops=10
config_file="configs/defconfig.conf"
build=0

while true; do
    case "$1" in
        -j|--num-gpu-threads)   IFS=',' read -r -a num_gpu_threads_list <<< "$2"; shift 2 ;;
        -n|--num-service-units) IFS=',' read -r -a num_service_units_list <<< "$2"; shift 2 ;;
        -l|--block-size)        block_size="$2"; shift 2 ;;
        -q|--num-queues)        num_queues="$2"; shift 2 ;;
        -i|--input-config)      config_file="$2"; shift 2 ;;
        --target-miops)         target_miops="$2"; shift 2 ;;
        --build)                build=1; shift ;;
        -h|--help)              usage ;;
        --) shift; break ;;
        *) echo "Error: Internal error"; exit 1 ;;
    esac
done

if [[ $build -eq 1 ]]; then
    echo "Building SwarmIO with config: ${config_file}"
    scripts/build.sh -i $config_file --clean
    scripts/build_bam.sh --clean
fi

OUTDIR=results/bam/trace
mkdir -p $OUTDIR

type=$(basename $config_file | sed 's/\.[^.]*$//')
if grep -q CONFIG_SWARMIO_PROFILE_REQ .main.o.cmd; then
    show_breakdown=1
    csv_header="block_size,num_queueus,num_gpu_threads,ssd_type,target(us),disp(us),wait_issue(us),copy(us),wait_cpl(us),fill_cpl(us),error(us),MIOPS,slat(us),clat(us)"
else
    show_breakdown=0
	csv_header="block_size,num_queueus,num_gpu_threads,ssd_type,MIOPS,slat(us),clat(us)"
fi
echo $csv_header >> "${OUTDIR}/summary.csv"

for num_service_units in "${num_service_units_list[@]}"; do
    echo "Loading SwarmIO with $num_service_units service units"
    scripts/load.sh -i "$config_file" --num-service-units "$num_service_units" --block-size "$block_size" \
        --target-miops "$target_miops"

    echo "Loading BaM"
    scripts/load_bam.sh "${VDEV_BDF}"

    sync && sleep 0.5
    cat /proc/swarmio/stat > /dev/null

    for num_gpu_threads in "${num_gpu_threads_list[@]}"; do
        OUTFILE="${OUTDIR}/bam_l${block_size}_q${num_queues}_j${num_gpu_threads}_${type}_n${num_service_units}.log"

        echo " Running bam-block-bench with N=$num_service_units, J=$num_gpu_threads"

        # assuming swarmio is running on node 2,3
        sudo numactl \
            --cpunodebind=0,1 --membind=0,1 \
            scripts/run_bam-block-bench.sh \
                -j $num_gpu_threads -q $num_queues -l $block_size > $OUTFILE

        sync && sleep 0.5
        
        proc_stats=$(
            cat /proc/swarmio/stat | tail -n 1 |
            awk '
            {
                for (i = 1; i <= NF; i++)     {
                    printf "%.3f", $i / 1e3
                    if (i < NF)
                        printf ","
                }
                printf "\n"
            }'
        )

        perf_stats=$(
            awk '
            BEGIN { m=0; s=0; c=0 }
            /Ops\/sec:/ { m=$2/1e6 }
            /slat \(usec\):/ { match($0, /avg=([0-9.]+)/, a); s=a[1] }
            /clat \(usec\):/ { match($0, /avg=([0-9.]+)/, a); c=a[1] }
            END { printf "%.3f,%s,%s", m, s, c }
            ' "$OUTFILE"
        )

        # write csv
        if [[ "$show_breakdown" -eq 1 ]]; then
            echo "${block_size},${num_queues},${num_gpu_threads},${type}_n${num_service_units},${proc_stats},${perf_stats}" >> "${OUTDIR}/summary.csv"
        else
            echo "${block_size},${num_queues},${num_gpu_threads},${type}_n${num_service_units},${perf_stats}" >> "${OUTDIR}/summary.csv"
        fi
    done

    echo "Unloading BaM"
    scripts/load_bam.sh -u "${VDEV_BDF}"

    echo "Unloading SwarmIO"
    scripts/load.sh --unload
done
