# SwarmIO + BaM：编译与运行

SwarmIO 通过 Linux 内核模块创建虚拟 PCI/NVMe SSD，BaM 通过 GPU 发起 NVMe I/O。
本仓库统一维护一套 **CPU 实际数据复制、512 B I/O、NVIDIA P2P** 的操作流程，
对应当前 AMD 7950X、`6.8.0-multistream`、NVIDIA 专有驱动 `550.163.01` 实验机。
DSA 后端源码保留，但不属于本入口的支持配置。

兼容修改已经直接合入源文件，包含中文注释。无需打补丁、修改二进制或复制旧构建目录。
只维护以下入口：

```text
configs/bam-cpu.json         唯一配置：构建选项、模块参数、默认负载
scripts/swarmio_bam.py       唯一命令：doctor / build / load / run / status / unload
scripts/run_random_read.sh   单轮随机读加压快捷入口，仅转发到上述 run
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
当前入口限制为 512 B 读/写，避免把未整理的多 LBA 随机地址边界路径当作已支持功能。

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
| `nvmev/admin.c`、`src/worker.c`、`src/committer.c` | GPU BAR 非缓存映射、CQ 发布顺序、CPU PRP 偏移及映射释放 |
| `src/main.c`、`src/worker.c` | 只读 full-I/O 编译模式 |
| `bam/module/ctrl.c`、`ctrl.h`、`pci.c`、`map.c` | miscdevice、新内核和 NVIDIA P2P 兼容 |
| `bam/CMakeLists.txt`、`bam/include/`、`bam/benchmarks/`、`bam/src/` | CUDA CCCL/C++17、API 与 benchmark 适配 |

开发机本地已通过 23 项源码与入口回归检查，不加载 Linux 模块或执行 GPU I/O：

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
