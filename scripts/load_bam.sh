#!/bin/bash

set -e

script_dir=$(dirname $(realpath $0))
cd "${script_dir}/.."

usage() {
    echo "Usage: $0 [-u] <BDF1> [<BDF2> ...]"
    exit 1
}

bind_nvme_device() {
    local bdf="$1"
    local device_path="/sys/bus/pci/devices/$bdf"

    if [[ -e "$device_path" ]]; then
        if [ ! -e "/sys/bus/pci/drivers/nvme/$bdf" ]; then
            echo "Binding device $bdf to nvme driver..."
            sudo sh -c "echo '$bdf' > '/sys/bus/pci/drivers/nvme/bind'"
        fi
    else
        echo "WARNING: Device $bdf not found." >&2
    fi
}

unbind_nvme_device() {
    local bdf="$1"
    local device_path="/sys/bus/pci/devices/$bdf"
    local unbind_path="$device_path/driver/unbind"

    if [[ -e "$device_path" ]]; then
        if [[ -e "$unbind_path" ]]; then
            echo "Unbinding device $bdf from current driver..."
            sudo sh -c "echo -n '$bdf' > '$unbind_path'"
        fi
    else
        echo "WARNING: Device $bdf not found." >&2
    fi
}

load_bam_module() {
    local bam_module

    if lsmod | grep -q "^libnvm"; then
        return
    fi

    bam_module=$(find ${script_dir}/../bam/Release/module -name libnvm.ko -type f)
    if [[ -z "$bam_module" ]]; then
        echo "Error: libnvm.ko not found in ${script_dir}/../bam/Release/module"
        exit 1
    fi
    sudo insmod "$bam_module" max_num_ctrls=64
}

unload_bam_module() {
    if lsmod | grep -q "^libnvm"; then
        sudo rmmod libnvm
    fi
}


# ================================
bdf_list=()
unload=0

for arg in "$@"; do
    if [[ "$arg" == "-u" ]]; then
        unload=1
    else
        bdf_list+=("$arg")
    fi
done

if [[ "${#bdf_list[@]}" -eq 0 ]]; then
    usage
fi

if [[ "$unload" == "1" ]]; then
    unload_bam_module
    for bdf in "${bdf_list[@]}"; do
        bind_nvme_device "$bdf"
    done
else
    for bdf in "${bdf_list[@]}"; do
        unbind_nvme_device "$bdf"
    done
    load_bam_module
fi