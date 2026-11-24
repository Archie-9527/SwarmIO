#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -u
export LC_ALL=C

usage() {
    printf '用法：bash %s [输出文件]\n' "$0"
    printf '默认写入当前目录的 swarmio-env-<UTC时间>.txt。先执行 sudo -v 可读取受限的硬件信息。\n'
}

case ${1:-} in
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    usage >&2
    exit 2
fi

report=${1:-swarmio-env-$(date -u +%Y%m%dT%H%M%SZ).txt}
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
kernel=$(uname -r) || exit 1

umask 077
set -o noclobber
if ! exec 3> "$report"; then
    printf '无法创建报告 %s。请指定一个新文件名。\n' "$report" >&2
    exit 1
fi

use_sudo=0
if (( EUID != 0 )) && command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
    use_sudo=1
fi

privileged() {
    if (( use_sudo )); then
        sudo -n "$@"
    else
        "$@"
    fi
}

section() {
    printf '\n===== %s =====\n' "$1"
}

run() {
    printf '\n$'
    printf ' %q' "$@"
    printf '\n'
    "$@" || printf '[退出码 %d，继续采集]\n' "$?"
}

boot_options() {
    local option
    local -a args
    read -r -a args < /proc/cmdline || return
    for option in "${args[@]}"; do
        case $option in
            memmap=*|mem=*|*iommu=*|*iommu.*=*|pci=*|isolcpus=*|nohz_full=*|rcu_nocbs=*)
                printf '%s\n' "$option" ;;
        esac
    done
}

pci_devices() {
    local listing
    listing=$(lspci -Dnnk) || return
    printf '%s\n' "$listing" | awk '
        /^[^[:space:]]/ {
            selected = tolower($0) ~ /data streaming accelerator|non-volatile memory|vga compatible controller|3d controller|display controller/
        }
        selected
    '
}

dsa_topology() {
    local device path attribute parent group found=0
    for device in /sys/bus/dsa/devices/dsa[0-9]*; do
        [[ -e $device ]] || continue
        found=1
        path=$(readlink -f "$device") || continue
        parent=${path%/*}
        printf '%s PCI=%s\n' "${device##*/}" "${parent##*/}"
        for attribute in state max_groups max_work_queues max_engines max_work_queues_size max_batch_size; do
            [[ -r $device/$attribute ]] && printf '  %s=%s\n' "$attribute" "$(cat "$device/$attribute")"
        done
        if [[ -r $parent/numa_node ]]; then
            printf '  numa_node=%s\n' "$(cat "$parent/numa_node")"
        fi
        group=$parent/iommu_group
        if [[ -r $group/type ]]; then
            printf '  iommu_group_type=%s\n' "$(cat "$group/type")"
        fi
    done
    (( found )) || printf '未发现 DSA sysfs 设备。\n'
}

kernel_log() {
    local log
    log=$(privileged dmesg --color=never) || return
    printf '%s\n' "$log" |
        awk 'tolower($0) ~ /e820:|dmar|iommu|idxd|swarmio|libnvm|nvrm|bar 1:|aer:|unknown symbol|lockdown/' |
        tail -n 120
}

nvcc_path=${CUDACXX:-nvcc}
if ! command -v "$nvcc_path" >/dev/null && [[ -z ${CUDACXX:-} && -x /usr/local/cuda/bin/nvcc ]]; then
    nvcc_path=/usr/local/cuda/bin/nvcc
fi
kernel_build=/lib/modules/$kernel/build
symvers=$kernel_build/Module.symvers

{
    printf 'SwarmIO + BaM 环境报告\n采集时间（UTC） %s\n' "$(date -u +%FT%TZ)"
    if (( EUID != 0 && ! use_sudo )); then
        printf '当前没有免交互 sudo 权限。若硬件信息受限，可先执行 sudo -v 再采集。\n'
    fi

    section '仓库与系统'
    run git -C "$repo" rev-parse HEAD
    run git -C "$repo" ls-tree HEAD bam
    run git -C "$repo" submodule status -- bam
    run grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
    run uname -srm
    run mokutil --sb-state
    run boot_options
    run privileged grep -iE 'System RAM|reserved|ACPI' /proc/iomem

    section 'CPU、NUMA、PCIe 与 DSA'
    run lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
    run numactl -H
    run lspci -Dtv
    run pci_devices
    run dsa_topology
    run privileged accel-config list -i

    section 'NVIDIA 和 CUDA'
    run cat /proc/driver/nvidia/version
    run nvidia-smi --query-gpu=index,name,pci.bus_id,compute_cap,driver_version --format=csv
    run nvidia-smi topo -m
    run nvidia-smi -q -d MEMORY
    run "$nvcc_path" --version
    run "$nvcc_path" --list-gpu-code

    section '模块构建依赖'
    run gcc --version
    run cmake --version
    run dkms status
    run ls -ld "$kernel_build" "$symvers"
    run grep -E '^CONFIG_(INTEL_IDXD|INTEL_IOMMU|MODVERSIONS|MODULE_SIG)' "/boot/config-$kernel"
    run modinfo -F filename idxd
    run grep -wE 'e820__mapped_any|dsa_bus_type|idxd_drv_enable_wq|idxd_drv_disable_wq|__idxd_wq_quiesce' "$symvers"
    run privileged find /usr/src /var/lib/dkms -type f \( -name nv-p2p.h -o -name Module.symvers -o -path '*/drivers/dma/idxd/idxd.h' \) -print

    section '相关内核日志（最近 120 行）'
    run kernel_log
} >&3 2>&1

exec 3>&-
printf '报告已生成：%s\n' "$report"
