#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
export LC_ALL=C

usage() {
    cat <<'EOF'
在 Linux 无 DSA 主机上构建 SwarmIO + BaM，运行 CPU 搬运路径的冒烟测试。

用法
  bash scripts/smoke_no_dsa.sh --memmap-start SIZE --memmap-size SIZE --cpus A,B [--gpu N] [--trace-io]
  bash scripts/smoke_no_dsa.sh --build-only

环境变量
  NVIDIA          与已安装驱动匹配的 nv-p2p.h 所在目录
  NVIDIA_SYMVERS  对应当前内核的 NVIDIA Module.symvers 路径
  CUDACXX         nvcc 路径，默认从 PATH 查找。V100 建议 CUDA 12.9
  CUDA_ARCH       CUDA 编译目标，默认 70（V100）
  KERNELDIR       内核构建目录，默认 /lib/modules/$(uname -r)/build

SwarmIO 编译为纯 CPU 模式。测试需要 sudo 和已通过 memmap=<大小>$<起点> 预留的真实 DRAM。
容量和起点可用字节数或 K/M/G 后缀。A,B 是两个可用的 CPU 编号。
默认测试 GPU 0，64 线程、8 个 QP、QD 64，分别运行 512 B 和 4 KiB 读。
成功时排空并卸载，异常时保留现场。日志在 results/cpu-smoke.*。
--build-only 执行构建并安装 SwarmIO 模块产物，跳过模块装载和 I/O。
--trace-io 为 BaM 生成 CUDA 源码行号，并在绑定设备后开启首次 I/O 进度日志。
EOF
}

fail() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

build_only=0
trace_io=0
memmap_start=''
memmap_size=''
cpus=''
gpu=0
while (( $# )); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --build-only) build_only=1; shift ;;
        --trace-io) trace_io=1; shift ;;
        --memmap-start|--memmap-size|--cpus|--gpu)
            (( $# >= 2 )) && [[ $2 != --* ]] || fail "$1 需要一个值"
            case $1 in
                --memmap-start) memmap_start=$2 ;;
                --memmap-size) memmap_size=$2 ;;
                --cpus) cpus=$2 ;;
                --gpu) gpu=$2 ;;
            esac
            shift 2
            ;;
        *) fail "未知参数 $1，请用 --help 查看用法" ;;
    esac
done

[[ $(uname -sm) == 'Linux x86_64' ]] || fail '请在目标 x86_64 Linux 主机上运行'
[[ $gpu =~ ^[0-9]+$ ]] || fail '--gpu 需要 CUDA 设备编号'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ -f bam/CMakeLists.txt ]] || fail '请先准备 BaM 子模块源码'
[[ ! -d /sys/module/swarmio && ! -d /sys/module/libnvm ]] ||
    fail 'SwarmIO 或 libnvm 已加载，请先按基线的排空顺序卸载'

kernel_release=$(uname -r)
export KERNELDIR=${KERNELDIR:-/lib/modules/$kernel_release/build}
nvidia_include=${NVIDIA:-}
nvidia_symvers=${NVIDIA_SYMVERS:-}
cuda_arch=${CUDA_ARCH:-70}
[[ $cuda_arch =~ ^[0-9]+$ ]] || fail 'CUDA_ARCH 应是数值，如 70'
for cmd in sudo make cmake modinfo git getopt numfmt realpath; do
    command -v "$cmd" >/dev/null || fail "缺少 $cmd"
done
getopt_status=0
getopt --test >/dev/null 2>&1 || getopt_status=$?
(( getopt_status == 4 )) || fail '现有构建和测试脚本需要 GNU getopt'
nvcc=$(command -v "${CUDACXX:-nvcc}") || fail '找不到 nvcc，请设置 CUDACXX 或更新 PATH'
nvcc=$(realpath -- "$nvcc")

[[ -r $KERNELDIR/Module.symvers ]] || fail "缺少内核符号表 $KERNELDIR/Module.symvers"
[[ -n $nvidia_include && -r $nvidia_include/nv-p2p.h ]] || fail '请将 NVIDIA 指向 nv-p2p.h 所在目录'
[[ ${nvidia_symvers##*/} == Module.symvers && -r $nvidia_symvers ]] ||
    fail '请将 NVIDIA_SYMVERS 指向当前内核对应的 NVIDIA Module.symvers'
KERNELDIR=$(realpath -- "$KERNELDIR")
nvidia_include=$(realpath -- "$nvidia_include")
nvidia_symvers=$(realpath -- "$(dirname -- "$nvidia_symvers")")/Module.symvers
for path in "$PWD" "$nvcc" "$nvidia_include" "$nvidia_symvers" "$KERNELDIR"; do
    [[ $path != *[[:space:]\*\?\[]* ]] || fail "路径含有现有构建脚本无法处理的字符：$path"
done
gpu_targets=$("$nvcc" --list-gpu-code) || fail "无法查询 $nvcc 支持的架构"
grep -Fxq "sm_$cuda_arch" <<< "$gpu_targets" || fail "$nvcc 无法编译 sm_$cuda_arch。V100 请使用 CUDA 12.9"

vdev_bdf=0001:10:00.0
pci_root=/sys/bus/pci/devices
vdev=$pci_root/$vdev_bdf
mod_root=/sys/module

if (( ! build_only )); then
    for cmd in bc dmesg nvidia-smi taskset modprobe; do
        command -v "$cmd" >/dev/null || fail "缺少 $cmd"
    done
    [[ $memmap_start =~ ^[0-9]+[KMG]?$ && $memmap_size =~ ^[0-9]+[KMG]?$ ]] ||
        fail '请提供 --memmap-start 和 --memmap-size，使用字节数或 K/M/G 后缀'
    start_bytes=$(numfmt --from=iec -- "$memmap_start")
    size_bytes=$(numfmt --from=iec -- "$memmap_size")
    # BAR 起点按 16 KiB 对齐。前 4 GiB 留给元数据，128 MiB 测试范围外保留请求余量。
    (( start_bytes > 0 && start_bytes % 16384 == 0 && size_bytes % 4096 == 0 &&
       size_bytes >= 4 * 1024**3 + 128 * 1024**2 + 4096 &&
       start_bytes <= 9223372036854775807 - size_bytes )) || fail '预留区起点、大小或对齐不符合测试要求'
    # 接受内核命令行中的等值单位，例如 6144M 和 6G。
    read -r -a boot_args < /proc/cmdline
    reservation_found=0
    for arg in "${boot_args[@]}"; do
        [[ $arg == memmap=* ]] || continue
        IFS=, read -r -a reservations <<< "${arg#memmap=}"
        for reservation in "${reservations[@]}"; do
            if [[ $reservation =~ ^([0-9]+[KMG]?)\$([0-9]+[KMG]?)$ ]] &&
               [[ $(numfmt --from=iec -- "${BASH_REMATCH[1]}") == "$size_bytes" &&
                  $(numfmt --from=iec -- "${BASH_REMATCH[2]}") == "$start_bytes" ]]; then
                reservation_found=1
            fi
        done
    done
    (( reservation_found )) || fail '当前启动参数中没有与输入相符的 memmap=<大小>$<起点> 预留项'

    [[ $cpus =~ ^(0|[1-9][0-9]*),(0|[1-9][0-9]*)$ ]] || fail '请用 --cpus A,B 指定两个 CPU 编号'
    IFS=, read -r dispatcher_cpu worker_cpu <<< "$cpus"
    [[ $dispatcher_cpu != "$worker_cpu" ]] && taskset -c "$dispatcher_cpu" true && taskset -c "$worker_cpu" true ||
        fail "CPU $cpus 必须是两个不同且可用的 CPU"
    [[ ! -e $vdev ]] || fail "PCI 地址 $vdev_bdf 已被占用"
    nvidia-smi --query-gpu=index,name,pci.bus_id,driver_version --format=csv || fail 'NVIDIA GPU 或驱动不可用'
fi

sudo -v
mkdir -p results
log_dir=$(mktemp -d "$PWD/results/cpu-smoke.XXXXXX")
printf '日志目录 %s\n' "$log_dir"
exec 3>&1 > "$log_dir/run.log" 2>&1

report() {
    printf '%s\n' "$*"
    printf '%s\n' "$*" >&3
}

# 从装载前开始采集内核日志。异常路径只采集日志，保留模块和设备状态。
started=''
capture_kernel_log() {
    sudo dmesg --since "$started" --color=never > "$log_dir/kernel.tmp" &&
        mv -f "$log_dir/kernel.tmp" "$log_dir/kernel.log"
}
on_exit() {
    local status=$?
    trap - EXIT
    if [[ -n $started ]] && ! capture_kernel_log; then
        report '内核日志采集失败'
        (( status != 0 )) || status=1
    fi
    if (( status != 0 )); then
        report "执行失败（退出码 $status），日志目录 $log_dir"
        tail -n 15 "$log_dir/run.log" >&3
    fi
    exit "$status"
}
check_kernel_log() {
    capture_kernel_log
    if grep -Eiq 'Oops:|BUG:|kernel panic|general protection fault|blocked for more than|SwarmIO:.*(fail|error|timeout|timed out)' "$log_dir/kernel.log"; then
        fail "发现内核异常，请检查 $log_dir/kernel.log"
    fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '内核 %s，SwarmIO %s，BaM %s\n' "$kernel_release" "$(git rev-parse --short HEAD)" "$(git -C bam rev-parse --short HEAD)"
"$nvcc" --version

cpu_config=$log_dir/cpu.conf
cat > "$cpu_config" <<EOF
[build]
coalesce_sqe=1
skip_io=0
batch_worker_dma=0
use_agg_timing_update=1
use_cas_timing_update=1
use_disp_dma=0
use_worker_dma=0

[load]
block_size=512
memmap_start=$memmap_start
memmap_size=$memmap_size
cpus=$cpus
num_service_units=1
num_workers_per_service_unit=1
use_disp_dma=0
use_worker_dma=0
EOF

report '正在编译 SwarmIO 和 BaM'
bash scripts/build.sh -i "$cpu_config"
swarmio_deps=$(modinfo -F depends ./swarmio.ko)
swarmio_namespaces=$(modinfo -F import_ns ./swarmio.ko)
case ",$swarmio_deps," in
    *,idxd,*|*,idxd_bus,*) fail 'CPU 构建产物仍声明 IDXD 模块依赖' ;;
esac
if grep -Fxq IDXD <<< "$swarmio_namespaces"; then
    fail 'CPU 构建产物仍声明 IDXD 符号依赖'
fi
bam_cmake_args="-DKERNEL=$KERNELDIR -DCMAKE_CUDA_COMPILER=$nvcc -DCUDA_NVCC_EXECUTABLE=$nvcc -DCMAKE_CUDA_ARCHITECTURES=$cuda_arch -DNVIDIA=$nvidia_include -DNVIDIA_SYMVERS=$nvidia_symvers"
if (( trace_io )); then
    bam_cmake_args+=' -DCMAKE_CUDA_FLAGS=-lineinfo'
fi
CMAKE_ARGS="$bam_cmake_args" bash scripts/build_bam.sh --clean

bam_module=bam/Release/module/libnvm.ko
for module in swarmio.ko "$bam_module"; do
    vermagic=$(modinfo -F vermagic "$module")
    [[ ${vermagic%% *} == "$kernel_release" ]] || fail "$module 与运行内核 $kernel_release 不匹配"
done
report '编译完成'
(( ! build_only )) || exit 0

installed_module=$(modinfo -n swarmio)
cmp -s swarmio.ko "$installed_module" || fail "modprobe 会使用不同的模块 $installed_module"
started=$(date '+%Y-%m-%d %H:%M:%S')
capture_kernel_log || fail '无法读取本轮内核日志'

report '正在装载 SwarmIO 和 BaM 模块'
bash scripts/load.sh -i "$cpu_config"
for flag in disp_using_dma worker_using_dma; do
    [[ $(< "$mod_root/swarmio/parameters/$flag") == N ]] || fail "$flag 未关闭"
done
[[ -r $vdev/resource && $(< "$vdev/vendor") == 0x0c51 && $(< "$vdev/device") == 0x0110 ]] ||
    fail "$vdev_bdf 不是预期的 SwarmIO 虚拟设备"
read -r bar_start _ < "$vdev/resource"
[[ $bar_start =~ ^0x[0-9a-fA-F]+$ ]] && (( bar_start == start_bytes )) || fail 'SwarmIO BAR0 起点与预留地址不一致'

# libnvm 注册时会探测所有未绑定的 NVMe 设备。确保只有测试控制器可能接管。
for device in "$pci_root"/*; do
    if [[ $device != "$vdev" && -r $device/class && $(< "$device/class") == 0x010802 && ! -L $device/driver ]]; then
        fail "检测到其他未绑定的 NVMe 设备 ${device##*/}，停止加载 libnvm"
    fi
done
check_kernel_log
bash scripts/load_bam.sh "$vdev_bdf"
if command -v udevadm >/dev/null; then
    udevadm settle --timeout=10
fi
[[ -c /dev/libnvm0 && -e /sys/class/misc/libnvm0/device && /sys/class/misc/libnvm0/device -ef $vdev ]] ||
    fail '/dev/libnvm0 未关联本次测试的 SwarmIO 虚拟控制器'
check_kernel_log

if (( trace_io )); then
    trace_param=$mod_root/swarmio/parameters/trace_first_io
    [[ -e $trace_param ]] || fail 'SwarmIO 模块缺少 trace_first_io 参数'
    printf '1\n' | sudo tee "$trace_param" > /dev/null
    report '已开启首次 I/O 跟踪，可在另一个终端运行 sudo dmesg -w | grep --line-buffered "SwarmIO: trace_io"'
fi

for block_size in 512 4096; do
    report "正在运行 $block_size B 读测试"
    bench_log=$log_dir/read-${block_size}B.log
    BAM_GPU=$gpu BAM_CACHE_SIZE=64M BAM_DATA_SIZE=128M BAM_REQS=8 BAM_QUEUE_DEPTH=64 BAM_RANDOM=true \
        bash scripts/run_bam-block-bench.sh -j 64 -q 8 -l "$block_size" 2>&1 | tee "$bench_log"
    grep -Eq 'Number of Ops:[[:space:]]*512([[:space:]]|$)' "$bench_log" || fail "缺少预期的 $block_size B 测试结果"
    check_kernel_log
done

report '正在排空 I/O 并卸载模块'
bash scripts/load.sh --prepare-unload
bash scripts/load_bam.sh -u --no-rebind "$vdev_bdf"
bash scripts/load.sh --unload
[[ ! -d $mod_root/swarmio && ! -d $mod_root/libnvm && ! -e $vdev ]] || fail '测试模块或虚拟设备仍在系统中'
check_kernel_log
report 'CPU 路径冒烟测试通过，512 B 和 4 KiB 读均完成，两个模块已卸载'
