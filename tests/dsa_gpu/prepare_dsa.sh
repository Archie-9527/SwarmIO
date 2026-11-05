#!/usr/bin/env bash
# 管理员一次性预配置。默认只检查；--apply只允许对健康内核中的停用设备配置。
# 永不disable-device/disable-wq、解绑、复位或卸载任何模块。
set -euo pipefail
dev=${1:-dsa0}
apply=${2:---check}
[[ $dev =~ ^dsa[0-9]+$ ]] || { echo '设备名必须是dsa加编号'; exit 1; }
[[ $apply == --check || $apply == --apply ]] || { echo '用法: bash prepare_dsa.sh dsa0 [--check|--apply]'; exit 1; }
[[ $(uname -s) == Linux ]] || { echo '需要在Linux服务器执行'; exit 1; }
# 任何已有Oops都拒绝实验性重配置；该标志不代表正常的专有模块taint。
tainted=$(cat /proc/sys/kernel/tainted)
if ((tainted & 128)); then
    echo '内核已记录Oops(D标志)：不进行设备配置；可暂停测试并留待维护窗口处理。'
    exit 2
fi
if grep -q '^dsa_gpu_test ' /proc/modules; then
    echo 'dsa_gpu_test已存在：不并发修改其设备。若initstate=going，不可用此脚本恢复。'
    exit 2
fi
root=/sys/bus/dsa/devices
base=$root/$dev
[[ -d $base ]] || { echo "找不到$base"; exit 1; }
id=${dev#dsa}
g=4
total=$(cat "$base/max_work_queues_size")
size=$((total / 8))
transfer=$(cat "$base/max_transfer_size")
[[ $(cat "$base/max_groups") -ge 4 && $(cat "$base/max_engines") -ge 4 &&
   $(cat "$base/max_work_queues") -ge 8 && $(cat "$base/max_batch_size") -ge 1024 &&
   $size -ge 1 && $transfer -ge 65536 ]] || { echo '硬件不满足4group/4engine/8WQ配置'; exit 1; }
state=$(cat "$base/state")
matches=1
for ((i=0;i<8;i++)); do
    q=$base/wq$id.$i
    for pair in "group_id:$((i/2))" "size:$size" 'mode:dedicated' 'type:kernel' \
                'name:dmaengine' 'driver_name:dmaengine' 'max_batch_size:1024' "max_transfer_size:$transfer"; do
        attr=${pair%%:*}; wanted=${pair#*:}
        actual=$(cat "$q/$attr" 2>/dev/null || true)
        [[ $actual == "$wanted" ]] || matches=0
    done
    [[ $(cat "$q/state" 2>/dev/null || true) == enabled ]] || matches=0
done
for ((i=0;i<4;i++)); do
    [[ $(cat "$base/engine$id.$i/group_id" 2>/dev/null || true) == "$i" ]] || matches=0
done
if [[ $state == enabled && $matches == 1 ]]; then
    echo "${dev}已有匹配配置：4group/4engine/8个DMAengine WQ，每WQ容量${size}；未修改。"
    exit 0
fi
if [[ $state != disabled ]]; then
    echo "${dev}状态为${state}且配置不匹配；拒绝自动停用/重配。请管理员评估，不能用此脚本解开故障设备。"
    exit 1
fi
shopt -s nullglob
for q in "$base"/wq*; do
    [[ $(cat "$q/state") == disabled && ! -L $q/driver ]] || { echo "${q}并非完全停用，拒绝配置"; exit 1; }
done
if [[ $apply != --apply ]]; then
    echo "${dev}可准备配置：4group/4engine/8WQ，每WQ容量${size}，batch=1024。"
    echo '当前仅检查；确认该设备获准用于测试后，由管理员使用--apply。'
    exit 0
fi
((EUID == 0)) || { echo '--apply需要root权限'; exit 1; }
command -v accel-config >/dev/null || { echo '需要accel-config'; exit 1; }
# 先归零旧reservation，再设置每组预算，避免中途总预算超出设备能力。
rb=$(cat "$base/max_read_buffers")
for ((i=0;i<4;i++)); do printf '0\n' > "$base/group$id.$i/read_buffers_reserved"; done
for ((i=0;i<4;i++)); do
    printf '%s\n' "$((rb/g))" > "$base/group$id.$i/read_buffers_allowed"
    printf '%s\n' "$((rb/g))" > "$base/group$id.$i/read_buffers_reserved"
    accel-config config-engine "$dev/engine$id.$i" --group-id="$i"
done
for ((i=0;i<8;i++)); do
    accel-config config-wq "$dev/wq$id.$i" --group-id="$((i/2))" --wq-size="$size" \
        --priority=1 --mode=dedicated --type=kernel --name=dmaengine --driver-name=dmaengine \
        --max-batch-size=1024 --max-transfer-size="$transfer" --block-on-fault=0
done
accel-config enable-device "$dev"
for ((i=0;i<8;i++)); do accel-config enable-wq "$dev/wq$id.$i"; done
echo "${dev}配置完成。之后测试模块只借还DMA通道，保留该配置和驱动绑定。"
