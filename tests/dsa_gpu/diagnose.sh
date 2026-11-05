#!/usr/bin/env bash
# 只读检查，不加载/卸载/复位/解绑设备，不清除内核日志。
set -eu
if [[ $(uname -s) != Linux ]]; then
    echo '需要在实际Linux测试服务器运行此脚本。'
    exit 1
fi
printf 'kernel=%s\n' "$(uname -r)"
if grep '^dsa_gpu_test ' /proc/modules; then
    state=$(cat /sys/module/dsa_gpu_test/initstate 2>/dev/null || true)
    printf 'module_initstate=%s\n' "$state"
    case "$state" in
        going)
            echo '旧模块卡在卸载中：停止重复insmod/rmmod，保存完整dmesg。'
            echo '可暂停DSA测试并保留故障模块至维护窗口；本脚本不尝试在线恢复。'
            echo '尚未检查设备sysfs，避免触碰异常卸载后可能遗留的设备锁。'
            exit 2
            ;;
        coming)
            echo '模块正在初始化，勿并发加载/卸载。'
            exit 2
            ;;
        *)
            echo '模块已存在；本脚本不执行自动重载。'
            exit 1
            ;;
    esac
fi
tainted=$(cat /proc/sys/kernel/tainted)
if ((tainted & 128)); then
    echo '内核记录过Oops；暂停新的DSA检查/实验，留待管理员评估，未读取DSA设备sysfs。'
    exit 2
fi
shopt -s nullglob
devices=(/sys/bus/dsa/devices/dsa*)
if ((${#devices[@]} == 0)); then
    echo '未发现DSA设备；检查idxd驱动和BIOS。'
    exit 1
fi
for dev in "${devices[@]}"; do
    printf '\ndevice=%s state=%s\n' "${dev##*/}" "$(cat "$dev/state")"
    readlink "$dev/driver" 2>/dev/null || true
    for q in "$dev"/wq*; do
        printf '  %s state=%s\n' "${q##*/}" "$(cat "$q/state")"
    done
done
echo '只读检查完成。新测试模块需要匹配的、已启用的DMAengine WQ，不自行配置或解绑DSA。'
