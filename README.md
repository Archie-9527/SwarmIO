# SwarmIO + CoPilotIO / BaM

本分支使用 `copilotio/copilot-io` 作为默认 GPU I/O 引擎，同时构建
`copilotio/bam` 作为对比基线。两套用户库各自放在独立输出目录，共用同一份
`libnvm.ko` 和 SwarmIO CPU full-I/O 配置。默认使用 CPU 映射内存通知。原命令名 `swarmio_bam.py` 保留。

本地回归检查不等于 GPU 实测通过。当前开发机无法编译 Linux/CUDA 或执行 GPU I/O；
随机读的运行稳定性、吞吐和内容正确性仍需在目标机验证。本基准检查 NVMe 完成状态，
不做逐字节内容比对，也不声称已解决此前所有随机读停顿或故障后的卸载问题。

## 代码适配

- 入口从已移除的 `bam/` 改为 `copilotio/`，一次构建两套隔离库与可执行文件，
  并构建随附 GDRCopy 用户库；仅在选择 GDR 通知模式时构建 `gdrdrv.ko`。不构建或加载 `nv-p2p-dummy`，使用实际 NVIDIA DKMS CRC。
- 两套 libnvm 继承原先的 miscdevice、Linux 6.5+ get_user_pages 和 NVIDIA P2P 兼容实现。
  两份内核源码必须一致，否则 doctor 拒绝共用驱动。
- 用 CUDA/CCCL 原子操作替换旧 freestanding libc++；显式选择架构，CUDA C++17、CMake >= 3.22。
- 统一有限次读基准 `copilotio/microbenchmark/swarmio_read.cu` 编译两次。
  BaM 使用 GPU SQ/CQ，CoPilotIO 使用 CPU SQ/CQ 和可选择的 CPU 映射内存或 GDRCopy 完成通知。
  每次逻辑操作只发一条 NVMe READ，不含原 `read_data` 路径的额外读和页缓存队列记账。
  因此这里的结果不能直接与旧版含额外读的 BaM 数字比较。
- 相同线程、请求数、确定性随机序列、读范围、请求大小、QD、队列数，无隐藏预热、
  强制 16 队列或 4 GiB 缓冲区。每个线程独占缓冲页，`pages >= threads`。
- 修正 CPU 通知元数据先于 ready 发布、原子停止标志、线程创建失败回收，
  保留 CPU CQ 的真实主机地址，加入 SQ 发布系统屏障和 CQ volatile 读取。
  轮询线程使用普通调度，CPU 编号必须可用且不与 SwarmIO 服务核重叠。

## 目标机准备

当前目标环境：Linux `6.18.5`、NVIDIA 驱动 `580.196.07`、CUDA Toolkit/nvcc `12.4.131`/CCCL、
支持 P2P 的 Tesla/数据中心 GPU，Python >= 3.9。
`nvidia-smi` 显示的 CUDA 13.0 是驱动支持上限，实际编译工具链由 `nvcc --version` 确定。
命令中的 `--cuda-root` 应指向已安装的 Toolkit；当前日志确认是 `/usr/local/cuda-12.4`。
doctor 通过 `nvcc --list-gpu-code` 核对目标 GPU。该环境仍需目标机编译和运行验证。
需要当前内核构建树和对应 NVIDIA DKMS `Module.symvers`、`nv-p2p.h`。
默认模式不需要加载 GDRCopy 内核模块；用户库仍从子仓库构建，供同一二进制的可选 GDR 路径链接。

`configs/bam-cpu.json` 仍是唯一配置。当前配置预留 `memmap=24G$4G`、
4 个 dispatcher + 4 个 worker 使用 CPU 8–15，禁用 DSA，保留诊断，512 B 时序单位。
换机器须先核对预留地址、CPU 和 GPU，不能在未预留的普通内存上加载仿真器。

CoPilotIO 的连续 CPU SQ/CQ 需要空闲 2 MiB hugepages。默认随机读 8 队列、QD=512
需要 16 页（32 MiB）；64 队列需要 128 页。先在目标机检查：

```bash
grep -E 'HugePages|Hugepagesize' /proc/meminfo
# 示例：系统未被其他应用使用时，预留 128 个 2 MiB 页；不会自动修改系统配置。
sudo sysctl -w vm.nr_hugepages=128
```

确认 `Hugepagesize: 2048 kB` 且 `HugePages_Free` 足够。脚本会在启动 CoPilotIO 前检查。
QD<=64 时两个队列均能放在普通页中，不要求 hugepages。
默认 CoPilot 轮询使用 CPU 0–3，可用 `--copilot-base-core` 和 `--copilot-cores` 更改。

## 与原 BaM 相同的流程

从 SwarmIO 根目录操作，使用新构建目录，不混入旧产物：

```bash
python3 scripts/swarmio_bam.py doctor --cuda-root /usr/local/cuda-12.4
python3 scripts/swarmio_bam.py --build-dir build-copilotio build --jobs 8 --cuda-root /usr/local/cuda-12.4
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio load
# 默认小顺序读，CoPilotIO 引擎
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio run
# 原随机读快捷入口，默认 1024×8、8 队列、QD=512，默认引擎 CoPilotIO
sudo bash scripts/run_random_read.sh build-copilotio 1024 8 8 512
```

构建目录包含 `swarmio/swarmio.ko`、`libnvm/libnvm.ko`、
`bam/{lib/libnvm.so,bin/nvm-block-bench}`、
`copilotio/{lib/libnvm.so,bin/nvm-block-bench}` 和 `gdrcopy/src/` 下的用户库；仅 GDR 通知模式还生成其内核驱动。
`manifest.json` 记录源码、Git 状态、命令、环境和全部产物哈希。
内核、驱动版本和 DKMS 输入路径从运行环境读取，不固定为旧版本。

### 内核 6.18.5 的 BTF 构建失败

若出现 `failed to find '.BTF' ELF section ... vmlinux`，说明模块的 BTF 生成阶段
使用了缺少 `.BTF` 段的内核镜像。统一入口对 SwarmIO、libnvm、gdrdrv 均传入
`CONFIG_DEBUG_INFO_BTF_MODULES=`，在构建阶段关闭模块 BTF，无需重编内核或更换驱动。
不能写成 `CONFIG_DEBUG_INFO_BTF_MODULES=n`：Kbuild 使用 `ifdef` 判断，非空的 `n`
仍会开启该步骤；事后 objcopy 去除 BTF 无法挽救此前已经失败的构建。

更新源码后用新目录重建，保留旧失败日志：

```bash
python3 scripts/swarmio_bam.py --build-dir build-copilotio-6185 build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4
```

后续 `load/run/compare/unload` 也必须指定 `--build-dir build-copilotio-6185`。

### 用户库报 `__forceinline__ does not name a type`

`nvm_util.h` 的 `lane_id`、`warp_id`、`get_smid` 是含 PTX 的 GPU 函数。
BaM 和 CoPilotIO 用户库的 `.cpp` 文件由普通 C++ 编译器处理，不能直接解析这些函数。
两份头文件已加上 `#ifdef __CUDACC__`，只在 CUDA 编译时暴露这些函数。
不要通过把 `__forceinline__`、`__device__` 定义为空来绕过报错，否则会把 GPU 汇编暴露给 CPU 编译器。
回归测试会实际编译两套公共头文件（仅替代 SDK 依赖），并检查 CUDA 分支仍保留这些函数；
这项检查不能替代实际 nvcc 编译。

加载仍只绑定 vendor/device=`0c51:0110` 的 SwarmIO 虚拟控制器，不接管物理 SSD。
已有 swarmio/libnvm 时拒绝自动替换；GDR 模式也拒绝替换已有 gdrdrv。`status`、失败保留现场、SIGINT 和正常
`unload` 的原有规则保留；不强杀、不自动重跑、不强制卸载。

### CoPilotIO 报通知结构不完整或队列表类型未定义

若 BaM 已链接成功，而 CoPilotIO 在 `notify[cid].ready` 等位置报
`pointer to a complete object type` 或 `copilot_cq_table is undefined`，原因是
`page_cache.h` 使用了只有前向声明的协议结构。
CPU/GPU 共用结构现放在 `copilotio/copilot-io/include/copilot_types.h`，
由 `page_cache.h` 和 `copilot.h` 直接包含，消除对包含顺序的依赖。
同步源码时须包含这个新增头文件。它不依赖 GDRCopy 或 CUDA 运行时，结构布局保持不变。

## 通知模式和当前内核限制

`configs/bam-cpu.json` 的 `notify_memory` 当前为 `host`：
CPU 轮询 CPU CQ，并向 `cudaHostAllocMapped` 分配的主机内存发布通知；
GPU 使用对应的设备地址读取通知，仍由 GPU 提交请求和回收完成。
构建不生成 `gdrdrv.ko`，加载不插入该模块，因此不依赖 `__vma_start_write`。
模式写入构建 manifest，不能在旧构建中只改 JSON 或手改 manifest 来切换；需要重新构建。
旧 manifest 未记录模式时按原来的 `gdr` 解释，不会静默改变其含义。

保留 `notify_memory=gdr` 选项用于兼容环境。Linux 6.18.5 与专有 NVIDIA 580 驱动下，
当前 GDRCopy 的 `vm_flags_set` 会引入 GPL-only 的 `__vma_start_write`。
引用专有 nvidia 模块的 gdrdrv 不能同时使用它，即使 gdrdrv 自身声明了 GPL 兼容许可；
构建及加载前审计会拒绝这种组合。不会绕过 VMA 锁或修改模块许可证来隐藏问题。
参见 [GDRCopy 上游问题 #335](https://github.com/NVIDIA/gdrcopy/issues/335)。

加载失败会自动把 `error.log` 和完整只读现场写到 `results/load-failed-*/`。
`insmod libnvm` 返回成功不代表设备 probe 成功，最终必须通过 `/dev/libnvm0` 归属校验。
服务器定制内核的 `pci_fixup_nvmev_cxl_bars` 会按同一个 PCI ID 错套 CXL 布局，
把 BAR0 缩到 64 KiB，并创建 BAR2/BAR4。SwarmIO 的 MSI-X 表在 BAR0+0x81000，
实际配置只提供一个 1 MiB BAR0；仅切换通知模式无法解决这个独立问题。
`nvmev/pci.c` 已在扫描完成、驱动绑定之前恢复真实布局并清空额外 BAR，保留 PCI ID，
不需要修改或重编服务器内核。若资源已被其他代码认领则拒绝改写。
日志可能仍先显示内核的 CXL fixup，随后应出现
`Restored NVMe BAR0 [0x100000000-0x1000fffff]; BAR2/BAR4 disabled`，
最终必须显示 `BDF -> /dev/libnvm0`。

对已经部分加载的旧现场先运行 `status` 保存日志，然后正常重启清理。
同步父仓库及 `copilotio/` 内的修改后，使用新目录构建默认 host 模式：

```bash
python3 scripts/swarmio_bam.py --build-dir build-copilotio-host build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio-host load
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio-host compare
```

这仍需目标机编译与 GPU 实测验证。本地检查包含恢复被 CXL fixup 改写的资源布局、
已认领资源拒绝改写、host 通知分配/释放，以及专有驱动下的 GPL-only 符号拒绝。

## 两个引擎对比

同一次加载后，顺序运行 BaM、CoPilotIO，参数相同，任一轮失败立即停止：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio compare \
  --threads 1024 --reqs 8 --num-queues 8 --queue-depth 512
```

`comparison.csv/json` 含 `notify_memory=host/gdr/none` 标签；原始日志和每轮 `run.json` 也记录通知位置。
CPU 映射通知模式的 GPU 经 PCIe 读取主机内存，不能把其数字当作原始 GDRCopy GPU 通知模式的结果。

`compare` 固定随机读；默认参数为上面这组。结果写入
`results/compare-*/comparison.csv` 和 `comparison.json`，包含两轮路径、IOPS 和
CoPilotIO/BaM 比值。每轮另有 `run.json`、`bench.log`、`before.log`、`after.log`。
短测试适合路径检查；性能比较需增加请求数并重复测量，考虑运行顺序和资源采样开销。

单独选择 BaM：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio run --engine bam \
  --random --threads 1024 --reqs 8 --num-queues 8 --queue-depth 512
```

4 KiB 对比和监控示例，地址范围仍为 1 GiB：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio compare \
  --threads 4096 --reqs 256 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 4096 --num-blks 262144 --monitor-interval 0.1
```

`num_blks` 以请求大小为单位。4 KiB 请求不改变 namespace 的 512 B LBA。
使用多于 4096 线程时同时增加 `--pages`；仅支持读测试。
监控继续生成 CSV、HTML/SVG 和全队列 NVMe 吞吐，`--bar-latency` 仍需配合监控。

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio status --output results/state.log
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio unload
python3 -m unittest discover -s tests -v
```

## 同步源码

`copilotio/` 是独立 Git 工作区，修改必须随该仓库一起上传；仅上传父仓库文件或仅记录
子模块引用不会携带未提交的子仓库修改。父仓库保留你原有的 `.gitmodules` 修改。
若通过 Git 分发，请先提交并推送子仓库，再在 SwarmIO 中记录其引用；若复制文件，
同时复制整个 `copilotio/` 源码。不要复制旧构建目录。
