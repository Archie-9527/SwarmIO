# SwarmIO + BaM：编译与运行

SwarmIO 通过 Linux 内核模块创建虚拟 PCI/NVMe SSD，BaM 通过 GPU 发起 NVMe I/O。
本仓库统一维护一套 **CPU 实际数据复制、512 B/4 KiB I/O、NVIDIA P2P** 的操作流程，
对应当前 AMD 7950X、`6.8.0-multistream`、NVIDIA 专有驱动 `550.163.01` 实验机。
DSA 后端源码保留，但不属于本入口的支持配置。

兼容修改已经直接合入源文件，包含中文注释。无需打补丁、修改二进制或复制旧构建目录。
只维护以下入口：

```text
configs/bam-cpu.json         唯一配置：构建选项、模块参数、默认负载
scripts/swarmio_bam.py       唯一命令：doctor / build / load / run / status / unload
scripts/run_random_read.sh   单轮随机读加压快捷入口，仅转发到上述 run
scripts/swarmio_monitor.py   资源采样、HTML/SVG 图表与历史 CSV 重绘（Python 标准库）
tests/test_swarmio_bam.py    源码回归检查，不是历史性能实验
README.md                   当前完整操作说明与问题汇总
```

**目前尚未解决随机读/队列循环复用卡顿，以及中断负载后的 worker 空指针和卸载等待。**
此前小顺序读有成功记录，不能据此声称任意负载均可正常完成。本入口的目标是消除构建、
库路径、驱动绑定和现场管理中的错误；脚本无法代替内核与 GPU 队列算法的后续修复。
本版本尚需目标机验证，不能将“编译/加载成功”视为“I/O 验证通过”。

## 1. 提交并重新拉取源码

`bam/` 是独立 Git 子模块。先在 BaM 中提交、推送修改，再在父仓库记录新版本。
仅提交父仓库不能保存 BaM 工作区中的修改。

```bash
# 在当前 SwarmIO 目录中检查、提交 BaM；detached HEAD 时先创建自己的分支。
git -C bam status --short
git -C bam diff --check
git -C bam add -A
git -C bam commit -m "Consolidate SwarmIO BaM runtime baseline"
git -C bam push -u origin HEAD

# 确认 .gitmodules 指向能获取上述提交的 BaM 仓库，再提交父仓库。
git add -A
git diff --cached --check
git diff --cached --stat
git diff --cached --submodule=short -- bam
git commit -m "Unify SwarmIO BaM build and runtime workflow"
git push
```

若 `origin` 不是可写仓库，先设置可写远程；若 BaM 处于 detached HEAD，先执行
`git -C bam switch -c <自己的分支名>`。不要把未推送的子模块 commit 留在父仓库中。
脚本不会代替你提交、推送或修改 Git 暂存区。

在服务器保留旧目录作为故障现场，重新克隆到新目录：

```bash
git clone <你的SwarmIO仓库URL> ~/SwarmIO-clean
cd ~/SwarmIO-clean
# 若提交在非默认分支，先切换到对应分支。
git submodule update --init bam
git status --short
git -C bam status --short
git submodule status
```

两个工作区应干净，子模块状态行前不应有 `+` 或 `-`。
不需要递归初始化已经不用的 freestanding 子模块。不要复制旧 `build-*`、`Release`、`.ko` 或 `.so`。

## 2. 准备服务器

需要 Python >= 3.9、CMake >= 3.22、make、C/C++ 编译器、可用 CUDA/CCCL 工具链、
binutils、kmod、udev、Git，以及当前内核对应的构建树和 NVIDIA DKMS 构建符号表。
沿用当前已经可用的 CUDA 工具链；不为本流程重装驱动或切换内核。

当前实验机配置：

| 参数 | 配置 |
| --- | --- |
| 运行内核 | `6.8.0-multistream` |
| 物理内存预留 | `memmap=24G$4G`，4 GiB 起预留 24 GiB |
| 虚拟 namespace | 预留内存扣除内部 4 GiB，约 20 GiB，非持久数据 |
| 服务线程 | 4 个 dispatcher + 4 个 worker，CPU 8-15 |
| 时序模型 | 512 B 单位；调度 400 ns，基础延迟 49600 ns，8 个时序资源 |
| 后端数据搬运 | CPU 实际复制，dispatcher/worker DSA 均关闭 |
| GPU | 配置中 `gpu=0` 是 nvidia-smi 的设备选择；运行时固定其 UUID |

启动参数尚未预留内存时，先按本机内存布局设置 GRUB 并正常重启。
例如 `/etc/default/grub` 的双引号内需要保留字面量 `$`，写作 `memmap=24G\$4G`。
不要在尚未预留的普通 RAM 地址上加载仿真器。换机器时先修改 `configs/bam-cpu.json`，
不能直接套用这里的地址和 CPU 编号。

若之前已经发生 Oops 或 `rmmod` 卡在 `D` 状态，先保存原现场并正常重启。
不能在这类状态上继续加载、运行或强制卸载。

## 3. 检查环境

从新克隆的 SwarmIO 根目录执行。通常使用普通用户，前提是内核构建树和 DKMS 输入对该用户可读：

```bash
python3 scripts/swarmio_bam.py doctor
```

它会核对工具、内核构建输入、NVIDIA 驱动版本、DKMS 表、P2P 导出和 miscdevice 接口，
输出目标 GPU 名称、UUID、PCI 地址、CUDA 架构及启动预留/CPU 在线状态。
确认选中 Tesla 卡，避免选到 Quadro M2000。选另一块卡时：

```bash
python3 scripts/swarmio_bam.py doctor --gpu 0
```

如果普通用户出现 `/lib/modules/.../build/Makefile: Permission denied`，
先检查软链接目标及沿途目录权限；当前账号可能无法遍历另一账号的源码目录：

```bash
sudo namei -l "/lib/modules/$(uname -r)/build/Makefile"
```

需要先用 root 完成只读环境检查时，显式指定已经安装的 CUDA 目录，避免 sudo 的 PATH 隐藏 nvcc：

```bash
sudo python3 scripts/swarmio_bam.py doctor --cuda-root /usr/local/cuda-12.4
```

对尚未更新、没有 `--cuda-root` 选项的脚本，可临时使用
`sudo env PATH="$PATH" python3 scripts/swarmio_bam.py doctor`。
普通用户构建前，仍需由内核源码的管理者根据 `namei` 输出开放必要的目录遍历和文件读取权限。
脚本不会擅自更改内核树或其他用户目录权限。

当前服务器若使用 `zhaohq` 构建、内核树为 `/mnt/sda-data/home/yanhan/linux-6.8`，
由管理员为该用户授予所需的只读权限即可，不需要给内核树写权限：

```bash
# 缺少 setfacl 时先安装 acl 软件包。
sudo apt-get install acl
sudo setfacl -m u:zhaohq:--x /mnt /mnt/sda-data /mnt/sda-data/home /mnt/sda-data/home/yanhan
sudo setfacl -R -m u:zhaohq:rX /mnt/sda-data/home/yanhan/linux-6.8
python3 scripts/swarmio_bam.py doctor --cuda-root /usr/local/cuda-12.4
```

这是针对本机已经确认的路径和账号的授权，换账号/内核目录时需要替换。
如果剩余 DKMS 输入仍不可读，doctor 会同时列出检查到的输入权限问题；只对这些输入补充读取权限。
当前入口显式传入 CUDA 架构和编译器，已经移除 `native` 默认值，父/子工程统一支持 CMake 3.22 起。
显式架构和 CUDA C++17 所需接口见 [CMake 3.22 架构文档](https://cmake.org/cmake/help/v3.22/prop_tgt/CUDA_ARCHITECTURES.html)
与 [CUDA 标准文档](https://cmake.org/cmake/help/v3.22/prop_tgt/CUDA_STANDARD.html)。

NVIDIA 路径默认根据已加载驱动版本和当前内核推导。当前实验机对应：

```text
/var/lib/dkms/nvidia/550.163.01/6.8.0-multistream/x86_64/module/Module.symvers
/usr/src/nvidia-550.163.01/nvidia/nv-p2p.h
```

路径非标准时，`doctor` 和 `build` 均可显式传入
`--nvidia-symvers <DKMS构建表>` 和 `--nvidia-include <包含nv-p2p.h的目录>`。
不要使用 `/usr/src/nvidia-550.163.01/Module.symvers` 中此前不匹配的符号 CRC。
构建前需确保普通用户可读取所需 DKMS 文件。

## 4. 编译全部组件

```bash
python3 scripts/swarmio_bam.py build --jobs 8
```

构建不安装、不加载、不卸载模块。输入权限处理好后使用普通用户构建，自动采用目标 GPU 的 CUDA 架构，
避免 CMake `native` 同时选中其他旧 GPU。构建使用源码的逐字副本隔离对象文件，不改写 C/CUDA 源码。
编译后会核对模块名、vermagic、full-I/O 标记、CPU 构建的 idxd 依赖、
NVIDIA P2P 导入 CRC、GPL-only 导入及 benchmark 的 `libnvm.so` 路径。

所有输出集中到一个目录：

```text
build/
  manifest.json              源码哈希、两个 Git 提交/工作区状态、配置、环境、命令和产物哈希
  build.log
  swarmio/swarmio.ko         实际模块名 swarmio；已去除 BTF 段
  swarmio/source/            源码副本与 Kbuild 对象，不提交
  libnvm/libnvm.ko           实际模块名 libnvm；已去除 BTF 段
  libnvm/source/             BaM 内核模块源码副本与对象，不提交
  bam/lib/libnvm.so
  bam/bin/nvm-block-bench
```

构建拒绝已有输出目录，避免旧缓存或旧二进制混入。失败时保留 `build.log`，修正源码后，
使用新目录构建。全局选项必须放在子命令之前，例如：

```bash
python3 scripts/swarmio_bam.py --build-dir build-next build --jobs 8
```

后续 `load/run/unload` 也必须使用相同的 `--build-dir build-next`。
运行参数取自构建时保存的配置；修改 JSON 后需重新构建，不会悄悄改变已有构建的加载参数。

## 5. 加载仿真 SSD 和 BaM

在没有旧模块和旧 I/O 进程的干净状态下执行：

```bash
sudo python3 scripts/swarmio_bam.py load
```

此命令会核对当前内核、驱动、GPU 和四个产物的哈希，确认物理内存预留与 CPU 在线，
先加载 SwarmIO，再识别唯一的虚拟控制器。它只接受 PCI vendor/device 为
`0c51:0110` 的 SwarmIO 设备，不能指定物理 SSD。

目标原本绑定 `nvme` 时，仅解绑该虚拟控制器并加载 `libnvm`；插入失败时尝试恢复原绑定。
发现其他未绑定 NVMe 控制器时拒绝接管，已有 `swarmio/libnvm` 时拒绝自动替换。
有多个虚拟设备时可显式指定：

```bash
sudo python3 scripts/swarmio_bam.py load --bdf 0001:10:00.0
```

成功必须显示目标 `BDF -> /dev/libnvm0`，且节点属于目标虚拟控制器、动态设备号正确，
`skip_io=0`、`sq_diag_faulted=N`。加载记录保存在 `build/loaded.json` 并关联当前启动和构建。
中途失败时可能已有一个模块加载成功，脚本不会擅自回滚卸载；先使用 `status` 保存现场。
如果模块已经部分加载，修正问题后在正常重启得到的干净状态中重新加载，不反复执行 load。

## 6. 执行一轮 I/O

默认只运行一个小顺序读：64 线程、每线程 1 次逻辑操作、单队列、QD=512、
2 MiB 缓冲区、1 GiB 地址范围、512 B 请求。

```bash
sudo python3 scripts/swarmio_bam.py run
```

脚本使用构建记录中的 GPU UUID 设置 `CUDA_VISIBLE_DEVICES`，benchmark 内部使用 `--gpu=0`，
因此不会因 CUDA 与 nvidia-smi 的索引顺序不同而选错卡。
动态库搜索优先使用本次 `build/bam/lib`，并检查实际链接路径。
只有进程正常退出、输出 `Ops/sec` 且没有记录内核异常，才记为本轮完成。
**这只是 I/O 路径和吞吐检查，不包含逐字节数据内容校验。**

每轮创建唯一的目录：

```text
results/<时间>/run.json      命令、GPU、PID、构建指纹、完成状态
results/<时间>/bench.log     benchmark 原始输出
results/<时间>/before.log    运行前状态
results/<时间>/after.log     运行后状态
```

长时间没有 benchmark 输出时，脚本每 10 秒报告仍在运行的 PID，不自动重试。
在另一终端保存现场：

```bash
sudo python3 scripts/swarmio_bam.py status --output results/state.log
```

状态包括进程、模块状态、dispatcher/worker 栈、SQ/CQ 快照和内核日志；
不读取硬件寄存器，不在故障采集中额外调用可能阻塞的 nvidia-smi。

按 Ctrl+C 时只向 benchmark 发送一次 SIGINT，不强杀、不自动卸载；
5 秒后仍未退出则报告 PID 并保留现场。任何未正常完成的本轮记录都会阻止后续运行/卸载，
需保存状态并正常重启，避免重复使用半清理的队列。

可以显式选择写请求或调整线程、请求数、QD 和队列数；具体选项见：

```bash
python3 scripts/swarmio_bam.py run --help
```

小顺序读正常完成后，可使用单轮随机读加压脚本：

```bash
sudo bash scripts/run_random_read.sh
```

默认复用 `build` 的加载记录：1024 线程 × 每线程 8 次请求、8 个队列、每队列 QD=512，
仍为 512 B、2 MiB 缓冲区、1 GiB 地址范围。共 8192 次逻辑读，是默认小检查的 128 倍操作数；
这是一次短负载加压，不是持续时长或数据内容校验。底层 NVMe 命令数可能包含额外读取。
不重新编译、加载或卸载模块，GPU 和日志沿用统一入口。

位置参数依次为 `构建目录 线程数 每线程请求数 队列数 QD`。例如使用其他已加载构建：

```bash
sudo bash scripts/run_random_read.sh build-cpu-fixed 1024 8 8 512
```

也可不复制快捷脚本，直接执行等价命令：

```bash
sudo python3 scripts/swarmio_bam.py run --random --access read \
  --threads 1024 --reqs 8 --num-queues 8 --queue-depth 512
```

`--random` 以及增加 `--reqs/--threads/--num-queues` 可能触发尚未解决的停顿。
不将原来的 4096×32 随机负载作为默认步骤，也不自动执行参数矩阵或性能实验。
入口支持 `--page-size 512` 或 `--page-size 4096`，默认仍为 512 B。
`--num-blks` 表示请求大小单位的地址范围块数，因此 4 KiB 下 `262144` 块对应 1 GiB。
容量检查按 `num_blks × page_size` 计算；BaM 随机起始 LBA 的上限扣除整条请求占用的 LBA 数，
避免多 LBA 请求越过范围末尾。更新 BaM 后需要重新编译 benchmark。

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-4k run \
  --random --access read --threads 4096 --reqs 256 \
  --num-queues 16 --queue-depth 512 --page-size 4096 --num-blks 262144
```

4 KiB 请求不改变 namespace 的 512 B LBA 或 `io_unit_shift=9` 时序单位。
`run_random_read.sh` 继续使用构建记录中的请求大小；已有 512 B 构建用上面的 `run` 选项覆盖。
本地参数及 CPU 复制检查通过不代表目标 GPU 的 4 KiB 性能或稳定性已验证。

### 资源与吞吐打点

统一入口增加 `--monitor-interval 秒数`，支持 0.01..60 秒，默认关闭。
例如 100 ms 打点的 4 KiB 随机读：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-metrics run \
  --random --access read --threads 4096 --reqs 512 \
  --num-queues 16 --queue-depth 512 --page-size 4096 --num-blks 262144 \
  --monitor-interval 0.1
```

512 B 快捷脚本的可选第六参数也可以启用打点：

```bash
sudo bash scripts/run_random_read.sh build-metrics 4096 512 16 512 0.1
```

GPU/主机资源采样只依赖 Python 标准库、Linux `/proc` 和现有 `nvidia-smi`，不用安装绘图库。
**全队列 NVMe IOPS/带宽曲线需要新增的 `src/swarmio_io_metrics.h` 和 worker 调用点。**
同步源码后使用新目录构建一次：

```bash
python3 scripts/swarmio_bam.py --build-dir build-metrics build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4
```

用当前已加载构建的目录执行正常 `unload`，再执行
`sudo python3 scripts/swarmio_bam.py --build-dir build-metrics load`。
旧模块没有 `/sys/module/swarmio/parameters/io_metrics` 时，资源采样仍能工作，
报告会明确说明吞吐曲线缺失，不用单个 QID 的诊断计数冒充全部 I/O。

每轮在已有 `results/<时间>/` 中增加：

| 文件 | 内容 |
| --- | --- |
| `resources.csv` | 时间戳、GPU/主机/进程资源、全队列累计完成计数与区间吞吐 |
| `cpu-cores.csv` | 主机逐 CPU 核 busy/iowait |
| `tasks.csv` | BaM 进程、SwarmIO 各内核线程及监控进程的 CPU/RSS 明细 |
| `gpu-raw.csv`、`gpu-sampler.log` | 收到的 GPU 指标及采样进程错误输出 |
| `monitor-summary.json` | 实际采样间隔、采样耗时、告警、BaM 平均逻辑吞吐、实际吞吐及可选 BAR 延迟汇总 |
| `monitor.html`、`*.svg` | 可离线打开的完整图表报告，以及可单独导出的矢量图 |

正常完成或统一入口处理 SIGINT/失败后都会尝试生成报告；原始 CSV 逐次刷新。
若进程被外部强制终止，保留 CSV 后可重新绘图：

```bash
python3 scripts/swarmio_monitor.py results/<实际目录名>
```

所有图表坐标使用普通小数，不使用科学计数法；`nvme-iops.svg` 采用 **MIOPS**，
即 `1 MIOPS = 1,000,000 次/秒`。CSV 的 `nvme_iops` 仍保留原始 ops/s。
重绘历史 CSV 即可更新坐标格式，不需要重跑 I/O；历史未采集的延迟无法补算。

### GPU BAR 映射、复制与解映射延迟

显式增加 `--bar-latency`，并配合 `--monitor-interval`：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-bar-latency run \
  --random --access read --threads 4096 --reqs 512 \
  --num-queues 16 --queue-depth 512 --page-size 4096 --num-blks 262144 \
  --monitor-interval 0.1 --bar-latency
```

延迟功能需要新的 `io_metrics version=2` 模块、dispatcher 和 worker 修改，
以及 `src/swarmio_io_timing.h`。同步完整源码后，在新目录编译并切换模块：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-bar-latency build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4
# 旧测试已正常完成时，以实际加载目录执行 unload；例如：
sudo python3 scripts/swarmio_bam.py --build-dir build-metrics unload
sudo python3 scripts/swarmio_bam.py --build-dir build-bar-latency load
```

脚本在启动 benchmark 前开启 `io_latency`，退出测试范围后恢复原开关；
不清零模块累计计数，而是在 `run.json` 保存启动前/结束后的计数边界。
这样最终汇总覆盖这次进程范围内成功完成的 BAR 请求，而非仅首末资源采样点之间的请求。
发生失败/中断时也恢复开关；已在途请求使用其入队时保存的开关快照。
默认不打延迟时间戳，开启后会增加计时及计数开销，应与关闭时的结果比较。

`monitor.html` 增加延迟汇总表，`monitor-summary.json` 的 `gpu_bar_latency` 保存原始累计纳秒与计算结果：

| 指标 | 统计含义 |
| --- | --- |
| ioremap / memcpy_toio（写入 GPU）或 memcpy_fromio / iounmap | 分别累计 CPU 调用耗时，输出累计 ms 和平均 μs/成功 NVMe 命令 |
| 三段调用合计 | 上述三项累计值之和 |
| BAR span | 每个 GPU 数据段从 ioremap 开始到 iounmap 返回，包含段内其他处理；多段请求取所有段之和 |
| CPU xfer | 整个 CPU 数据搬运函数，含 PRP 解析、主机/设备页处理等 |
| host request | dispatcher 开始复制该 SQE 至 CPU 发布 CQE，含本地队列等待、搬运及仿真延迟 |

平均值分母为成功且开启计时的 **NVMe 命令数**，包含 BaM 的额外读；不是 `threads × reqs`。
失败命令、未完成请求、完全使用主机 RAM 的请求不进入 BAR 成功延迟汇总。
数据段数量另外记录；跨页命令的每段映射/复制/解映射都计入该命令，避免把调用次数当作请求次数。
不输出逐请求明细，仅维护队列累计计数和采样区间平均值。
`bar-latency.svg` 展示区间平均调用耗时，`bar-latency-share.svg` 展示区间累计占比。

报告同时给出三种不同的比值：

1. `三段累计耗时 / 同批请求的 CPU xfer 累计耗时`：定位数据搬运函数内部开销。
2. `三段累计耗时 / 同批请求的 host request 累计延迟`：包含排队与仿真延迟的主机请求口径。
3. `三段累计耗时 / BaM 整轮墙钟时间`：多 worker 会并行，结果可超过 100%，**不是串行墙钟占比**。

不会把累计耗时误当成墙钟上可相加的分段；也不会把主机侧请求延迟称为 GPU 端到端延迟。
若需所有 worker 的区间并集/真实墙钟覆盖比例，需要另外记录事件时间线，本功能不采集逐请求事件。
测量仅包围 GPU **数据页**的三个 CPU 调用，不包含 SQ/CQ 常驻映射和 PRP 列表映射。
CPU 调用耗时含可能的抢占和等待；`memcpy_toio` 返回并不证明 PCIe posted writes 已到达 GPU，
本功能没有插入读回/flush 来改变原测试的行为。参考
[Linux 设备 I/O 文档](https://docs.kernel.org/6.8/driver-api/device-io.html)。

图表口径：

- GPU `busy` 是采样窗口内执行 kernel 的时间比例，包含轮询等待，**不是 SM occupancy**。
  `memory busy` 是显存读写忙碌率，显存 `used/total` 为整卡容量占用，不单独归因于 BaM。
- GPU 使用一个常驻 `nvidia-smi --loop-ms` 子进程；驱动刷新窗口可能慢于打点间隔，
  因此 100 ms 打点不保证每 100 ms 都得到一份独立的 GPU 硬件测量。
  缺失、N/A、过期数据留空，不伪造为零。口径见
  [NVIDIA 官方说明](https://docs.nvidia.com/deploy/nvidia-smi/index.html)。
- 整机 CPU busy 归一到 100%；BaM、SwarmIO 和监控进程以一个核为 100%，合计可以超过 100%。
  SwarmIO 空闲轮询也可能持续占用 CPU。主机 RAM 数据不包括 `memmap` 预留内存。
- NVMe 曲线按全队列成功发布的 CQE 和真实命令长度差分，包含 BaM 额外读及其 512 B 数据量，
  失败命令单独累计为 errors。不是 `threads × reqs` 的逻辑 IOPS，也不证明 GPU 已看到 CQE。
  当前仅支持已有的 single-owner CPU 配置；每队列计数使用 seqcount，避免共享全局原子热点。
- 图表覆盖进程初始化、kernel 执行和清理；BaM 末尾平均吞吐仅覆盖它的 GPU kernel 计时区间。
  报告把两者分别标注。极短测试应增加 reqs，以便取得足够采样点。
- 采样和内核计数本身都有开销。CSV 保留真实时间与采样耗时，错过的点不补造；
  需要比较带监控和不带监控的运行，才能评估监控对性能的影响。

## 7. 正常结束或切换构建

仅在本轮已经正常完成、没有 I/O 进程、没有内核 Oops、模块均为 Live 时执行：

```bash
sudo python3 scripts/swarmio_bam.py unload
```

脚本按 `libnvm -> swarmio` 顺序正常卸载；不使用 `rmmod -f` 或 SIGKILL。
每个 `rmmod` 等待超过 15 秒时，保存状态并报告其 PID，**不终止正在内核中执行的卸载，也不继续下一步**。
若已出现内核异常、未完成负载或模块处于 Unloading，直接拒绝继续卸载；保存状态后正常重启。
`kill swarmio_dispatcher_0` 不能替代内核线程的正常退出。

正常卸载也仍受现有内核退出路径缺陷影响，脚本只负责检测和保留现场，不宣称已修复此缺陷。

## 8. 已遇到的问题统一说明

| 现象 | 已确认的原因/证据 | 当前处理 |
| --- | --- | --- |
| `insmod: Invalid parameters` | 单凭此消息无法定因；此前有 NVIDIA CRC 不匹配与 GPL-only 符号解析问题 | 结合本次 `dmesg`；构建前后核对匹配内核的 DKMS 表和模块导入 |
| `nvidia_p2p_* disagrees about version` | 使用了不匹配的 NVIDIA 符号表；驱动版本号相同不足以证明 CRC 一致 | 使用运行内核对应 DKMS 表；审计编译输出 CRC；最终仍以实际加载为准 |
| `class_create/device_create` 解析失败 | 当前内核将其导出为 GPL-only，依赖 NVIDIA 专有模块的 BaM 无法使用 | BaM 源码改用 miscdevice 和动态设备号，不改许可证、内核导出或 CRC |
| `get_user_pages` 编译错误 | Linux 6.5+ 移除了旧的最后一个参数 | `bam/module/map.c` 中按内核版本选择接口 |
| BTF 校验失败 | 自定义内核环境中曾无法加载带 BTF 的模块 | 构建关闭模块 BTF并生成去除 BTF 的加载产物，不修改代码段 |
| 无 DSA 仍依赖 idxd 内部头/符号 | 原 Kbuild 无条件链接 DSA 实现 | CPU 配置只链接 `dsa/stub.c`；误调 DMA 返回错误，实际数据走 CPU 复制 |
| `invalid use of undefined type struct idxd_wq` | DMA 初始化日志仍访问 `wq->name`；运行时关闭 DMA 不能跳过编译期类型检查 | dispatcher/worker 日志改用已知的设备/队列编号，CPU 模式无需 idxd 私有结构定义 |
| 不确定是否真实复制数据 | `skip_io` 是编译开关，加载参数不能覆盖 | `skip_io` 只读状态由 worker 编译单元提供；加载验证必须为 0 |
| dispatcher 聚合调度访问非法 namespace | 之前的栈定位在 SQ 命令到 namespace 的访问路径 | SQ 按页读取，BAR 使用 memcpy_fromio；在访问 namespace 前校验 NSID/LBA/PRP1 等，异常锁存并停止提交 |
| 小顺序读成功，循环/随机负载仍卡住 | GPU SQ/CQ 使用 WT 缓存映射；CQ 用普通 memcpy 发布；CPU PRP 复制重复叠加页内偏移 | 已修正这些源码缺陷，**随机读卡顿是否消除仍待目标机验证**；增大 QD 不能替代修复 |
| CQ acquire 读取实验没有解决卡顿 | 加入后仍复现 | 已移除该对照项，不作为当前源码基线的已验证修复 |
| `__fill_cq_result` NULL 指针、worker 异常退出 | 日志含内核 Oops 和 `exited with irqs disabled` | **未解决**。停止实验，保存现场并重启；不以反复卸载恢复 |
| `rmmod swarmio` 卡住 | 栈为 `swarmio_dispatcher_exit -> kthread_stop`，模块 Unloading | 不强杀、不重复卸载；状态采集后正常重启 |
| `tee: No such file or directory` / 不清楚运行了哪份库 | 日志目录未建立、旧 Release/build 路径混用 | 脚本先建唯一结果目录，统一输出路径并记录源码/产物哈希 |

SQ 诊断当前仅接受读/写；队列快照要求 single-owner，不支持 committer/worker RR。
诊断计数中的 `cqe_writes` 表示 CPU 已执行完成项写入，不证明 GPU 已观察到它。
`cq_head_abs` 累计观察到的 doorbell 前进量，不能还原两次观察之间恰好转过完整一圈的变化。
BaM 的一次逻辑读可能发起额外底层命令，不能直接用逻辑操作数与 NVMe 完成数不相等判错。

GPU BAR 上的 SQ/CQ 现在使用 `ioremap/iounmap`，保留主机 RAM 命令缓冲区的 WB 映射。
完成项先写 SQHD/SQID，再经过写屏障，最后写包含 CID/phase 的 DWORD；BAR 使用 `writel`，
RAM 使用 `WRITE_ONCE`。不能用普通 `memcpy` 的实现顺序保证 GPU 轮询看到的发布顺序。
CPU 数据路径及 PRP 列表同样区分 RAM/BAR，并映射物理页基址后只叠加一次页内偏移。
例如 PRP1 为 `页基址+512` 时，原实现可能实际复制到 `页基址+1024`；现在复制到正确的 `+512`。
列表释放使用原始映射地址；映射失败返回内部错误完成项，复用 work 时清除上次状态。
这些修正遵循 [Linux 6.8 设备 I/O 接口规则](https://docs.kernel.org/6.8/driver-api/device-io.html)。
源码缺陷与卡顿现象的因果关系仍需目标机复测，CPU 模拟检查不能验证 PCIe/GPU 可见性。

## 9. 修改位置与验证范围

| 源码 | 用途 |
| --- | --- |
| `Kbuild`、`dsa/dsa.h`、`dsa/stub.c` | CPU 构建不依赖 idxd 私有实现 |
| `src/dispatcher.c`、`src/swarmio_sq_diagnostics.h` | SQ 读取与非法命令保护 |
| `src/dispatcher.c`、`src/worker.c`、`src/swarmio_queue_diagnostics.h` | 队列状态与回绕计数 |
| `src/swarmio_io_metrics.h`、`src/swarmio_io_timing.h`、`scripts/swarmio_monitor.py` | 全队列完成计数、BAR 延迟累计、资源采样和离线图表 |
| `nvmev/admin.c`、`src/worker.c`、`src/committer.c` | GPU BAR 非缓存映射、CQ 发布顺序、CPU PRP 偏移及映射释放 |
| `src/main.c`、`src/worker.c` | 只读 full-I/O 编译模式 |
| `bam/module/ctrl.c`、`ctrl.h`、`pci.c`、`map.c` | miscdevice、新内核和 NVIDIA P2P 兼容 |
| `bam/CMakeLists.txt`、`bam/include/`、`bam/benchmarks/`、`bam/src/` | CUDA CCCL/C++17、API 与 benchmark 适配 |

开发机本地已通过 32 项源码与入口回归检查，不加载 Linux 模块或执行 GPU I/O：

```bash
python3 -m unittest discover -s tests -v
```

覆盖 miscdevice 注册与分派、CPU/DSA 构建选择、无 idxd 私有头的 CPU backend 编译、
SQ 边界及跨页复制、真实 CQ producer 的满/回绕/phase 与 MMIO 发布顺序、
CPU PRP 全部 512 B 页内位置的读写哨兵、跨页/列表复制及映射失败、CRC/GPL-only 拒绝路径、
物理 NVMe 选择拒绝、产物哈希变化拒绝等。完整 Linux/CUDA 编译和运行仍需在目标机完成。
初始化日志检查在不定义 idxd_wq 内部结构、启用日志参数类型检查的条件下编译实际日志语句，
分别覆盖 -O0/-O2，并确认旧写法失败、当前写法通过。
生命周期检查使用构建工具替身和 CPU 子进程，覆盖精确源码复制、失败绑定恢复、运行状态记录和
卸载超时不强杀；这些检查不能替代目标机验证。
旧实验报告、性能矩阵、重建/打补丁脚本和重复入口已移除；`build/`、`build-*/`、`results/`、
对象文件与 Python 缓存均不进入源码提交。
监控检查覆盖 20 ms 采样、流式 GPU 子进程替身、CPU guest 时间去重、PID stat 解析、
实际内核计数实现的跨 QID/不同请求大小/错误计数、SVG 格式与缺失指标重绘。
还覆盖 MIOPS 与普通小数坐标、实际 CPU 搬运函数的三阶段时钟、关闭计时不读时钟、
加权平均与累计占比、并行累计时间/墙钟比超过 100%、异常退出恢复延迟开关。
这些检查没有在开发机加载 NVIDIA 驱动或 Linux 模块，目标机计数与监控开销仍需实测。
