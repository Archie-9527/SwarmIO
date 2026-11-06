#!/usr/bin/env bash
# 固定工作集扫描；任何传输/校验失败立即停止，不自动重跑或卸载。
set -euo pipefail
cd -- "$(dirname -- "$0")"
path=${DG_PATH:-replay}
direction=${DG_DIRECTION:-read}
seconds=${DG_SECONDS:-10}
repeats=${DG_REPEATS:-3}
mb=${DG_MB:-64}
gpu=${DG_GPU:-0}
[[ $path == pipeline || $path == payload || $path == replay ]] || { echo '无效DG_PATH'; exit 1; }
[[ $direction == read || $direction == write || $direction == mixed ]] || { echo '无效DG_DIRECTION'; exit 1; }
for value in "$seconds" "$repeats" "$mb" "$gpu"; do
    [[ $value =~ ^[0-9]+$ ]] || { echo '参数需要非负整数'; exit 1; }
done
((seconds>0 && repeats>0 && mb>0 && mb<=256 && !(mb & (mb-1)))) || { echo '无效测量时间/工作集'; exit 1; }
./dsa-gpu-bench --info
groups=$(cat /sys/module/dsa_gpu_test/parameters/groups)
pin_budget=$(cat /sys/module/dsa_gpu_test/parameters/max_pinned_mb)
for size in ${DG_SIZES:-512 4096 16384 65536}; do
    [[ $size =~ ^[0-9]+$ ]] && ((size>=512 && size<=65536 && !(size & (size-1)))) || exit 1
    if [[ $path == pipeline && $size != 512 && $size != 4096 ]]; then continue; fi
    sq_bytes=0
    if [[ $path == pipeline ]]; then
        sq_bytes=$((groups * ((mb*1048576/groups/size*64+65535)/65536*65536)))
    fi
    if ((mb*1048576+sq_bytes > pin_budget*1048576)); then
        echo "SKIP size=$size: exceeds module pin budget ${pin_budget}MiB"; continue
    fi
    for batch in ${DG_BATCHES:-8 32 128 1024}; do
        [[ $batch =~ ^[0-9]+$ ]] && ((batch>=2 && batch<=1024 && !(batch & (batch-1)))) || exit 1
        for depth in ${DG_DEPTHS:-1 4 8 16}; do
            [[ $depth =~ ^[0-9]+$ ]] && ((depth>=1 && depth<=16)) || exit 1
            if ((groups*batch*depth*size > mb*1048576)); then
                echo "SKIP size=$size batch=$batch depth=$depth: working set cannot cover all in-flight requests"
                continue
            fi
            for ((repeat=1;repeat<=repeats;repeat++)); do
                printf '\npath=%s size=%s batch=%s depth=%s mb=%s repeat=%s\n' "$path" "$size" "$batch" "$depth" "$mb" "$repeat"
                ./dsa-gpu-bench --path "$path" --size "$size" --mode batch --batch "$batch" \
                    --depth "$depth" --direction "$direction" --mb "$mb" \
                    --seconds "$seconds" --warmup-ms 1000 --gpu "$gpu"
            done
        done
    done
done
