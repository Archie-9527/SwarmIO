#!/usr/bin/env bash
# 默认4group完整资源扫描。任何编译/设备/校验错误都停止，日志不会被过滤。
set -euo pipefail
cd -- "$(dirname -- "$0")"
seconds=${DG_SECONDS:-5}
repeats=${DG_REPEATS:-3}
direction=${DG_DIRECTION:-read}
# groups只用于计算最小工作集，必须与已加载模块的groups一致。
groups=${DG_GROUPS:-4}
pin_budget=$(cat /sys/module/dsa_gpu_test/parameters/max_pinned_mb)
for size in 512 4096; do
    for batch in 32 64 128 256 512 1024; do
        for depth in 1 2 4 8 16; do
            # block数覆盖在途项，并计算data+SQE的总映射预算。
            required=$((groups * batch * depth * size))
            mb=8
            while ((mb * 1048576 < required)); do mb=$((mb * 2)); done
            stride=$(((mb * 1048576 / groups / size * 64 + 65535) / 65536 * 65536))
            if ((mb * 1048576 + groups * stride > pin_budget * 1048576)); then
                printf 'SKIP size=%s batch=%s depth=%s: data+SQE exceeds pin budget %s MiB\n' \
                    "$size" "$batch" "$depth" "$pin_budget"
                continue
            fi
            for ((repeat=1; repeat<=repeats; repeat++)); do
                printf '\nsize=%s batch=%s depth=%s repeat=%s mb=%s\n' \
                    "$size" "$batch" "$depth" "$repeat" "$mb"
                ./dsa-gpu-bench --size "$size" --mode batch \
                    --batch "$batch" --depth "$depth" --direction "$direction" \
                    --seconds "$seconds" --warmup-ms 1000 --mb "$mb" "$@"
            done
        done
    done
done
