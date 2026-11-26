#!/usr/bin/env bash
# Read-only BAR1/Resizable BAR inventory. Never unbind or resize a live GPU.
set -euo pipefail
out="${1:-results/bar1-check-$(date +%Y%m%d-%H%M%S).log}"
mkdir -p "$(dirname "$out")"
(
    set -euo pipefail
    printf '===== GPU identity =====\n'
    nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,driver_version --format=csv
    printf '\n===== BAR1 reported by the running driver =====\n'
    nvidia-smi -q -d MEMORY
    printf '\n===== Physical Resizable BAR capabilities =====\n'
    while IFS= read -r bdf; do
        bdf="${bdf/00000000:/0000:}"
        bdf="${bdf//[[:space:]]/}"
        printf '\n===== PCI device %s =====\n' "$bdf"
        lspci -Dvv -s "$bdf"
        resource="/sys/bus/pci/devices/${bdf,,}/resource1_resize"
        if [[ -r "$resource" ]]; then
            printf 'Supported BAR1 size bitmask (read-only): '
            cat "$resource"
        fi
    done < <(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader)
    if [[ -x ./displaymodeselector ]]; then
        printf '\n===== Installed NVIDIA Display Mode Selector help =====\n'
        ./displaymodeselector --help
    fi
) 2>&1 | tee "$out"
printf '\nBAR1 检查结果：%s\n' "$out"
