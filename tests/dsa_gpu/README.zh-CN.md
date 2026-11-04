# GPU SQE 获取与 CPU/GPU DSA 搬运测试

本目录实现独立测试模块 `dsa_gpu_test.ko` 和 CUDA 程序 `dsa-gpu-bench`。
目标是复现 SwarmIO 的搬运流程：**GPU 中的 64B SQE → DSA → CPU 命令缓冲区，解析后通过 DSA 在 CPU DRAM 与 GPU 数据区之间搬运 512B/4KiB payload**。

它没有 NVMe doorbell、CQ、FTL、SSD 时序模型或 GPU 持续轮询：CUDA 先生成一批命令，CPU 通过一次 ioctl 驱动整个实验。这是搬运微基准，报告的 IOPS 不代表 SwarmIO/NVMe 的端到端 IOPS。

## 1. 文件及复用关系

| 文件 | 作用 |
|---|---|
| `bench.cu` | CUDA 分配、初始化、GPU 生成 SQE、触发测试、最终逐字校验 |
| `kernel.c` | GPU persistent P2P 映射、主机 DMA 缓冲区、MEMMOVE/BATCH 提交与回收 |
| `uapi.h` | 64B 简化 SQE、ioctl ABI、两端共用的数据模式与地址生成 |
| `backend.c` / `config.c` | 直接编入当前 SwarmIO 的 `dsa/dsa.c`、`dsa/config.c` |
| `run_matrix.sh` | 运行 2 种大小 × 2 种发布模式 × 3 种方向的 12 组测试 |
| `test_model.py` | 无硬件的共用头编译和数据/地址边界检查 |

DSA 描述符操作参考当前 `dsa/single.c`、`dsa/batch.c`，在测试模块中实现独立的资源生命周期。GPU 映射参考 BaM 的 NVIDIA P2P 用法，但将映射目标明确设为 **DSA 自身的 PCI device**；没有把 CUDA 指针、GPU 物理地址或其它设备的 IOVA 直接当成 DSA 地址。

## 2. 两种发布模式

| 模式 | SQE 获取 | payload 搬运 |
|---|---|---|
| `sync` | 每条 64B SQE 提交一个 MEMMOVE 并等待完成 | 每个 512B/4KiB 请求提交一个 MEMMOVE 并等待完成 |
| `batch` | 一个窗口的连续 SQE 合并成一个 MEMMOVE，完成后解析 | 多个 MEMMOVE 子描述符构成硬件 BATCH，最多 `depth` 个 batch 在途，复用 slot 前等待对应完成 |

窗口最多 128 条命令；批量模式每个 batch 默认 32 项，默认最多 4 个 batch 在途。
批量异步指 **DSA payload 的异步发布**，不是异步返回用户态的 ioctl，也没有与下一窗口 SQE 获取重叠。
尾批只有一项时补一个 NOOP，以满足硬件 BATCH 至少两项的约束。NOOP 不计入操作数。

`--direction` 沿用存储读写方向：

* `read`：CPU → GPU，模拟读取主机 DRAM 中的 SSD 数据；SQE 始终仍是 GPU → CPU。
* `write`：GPU → CPU，模拟写入 SSD。
* `mixed`：按 block 奇偶划分固定读/写区域，各占一半。不同方向不访问同一个 block，避免混合异步测试发生数据依赖。

随机地址使用带 seed 的仿射置换。在 2 的幂个 block 上按打散顺序遍历，遍历完重复；**不是独立均匀随机采样，也不是 Zipf 负载**。窗口内不重复，读写按 block 对齐，payload 不跨 64KiB GPU 映射页。固定模式测试数据不随轮次改变，校验确认最终数据与 SQE 正确，不能检测每一次重复访问是否都发生了独立的数据变化。

## 3. 目标机器要求

* x86-64 Linux，具有 Intel DSA 的 Xeon 平台，BIOS 已启用 DSA。
* 支持 GPUDirect RDMA 和 persistent P2P API 的 NVIDIA GPU/驱动；本实现不支持 MIG/vGPU。
* CUDA 12 或更新版本，`nvcc`，C/C++ 编译工具、内核构建工具。
* 与**正在运行的内核**匹配的完整内核源码、配置和构建输出，包括 `drivers/dma/idxd/idxd.h`、`registers.h` 等内部头及导出的 IDXD 符号。
* 与**当前内核和已加载 NVIDIA 驱动**匹配的 `nv-p2p.h` 和 NVIDIA `Module.symvers`。仅安装同名版本的源码不一定具有相同的符号 CRC。
* DSA 到 GPU 的 PCIe P2P 路径必须可达，IOMMU/ACS 配置必须允许该访问。`nvidia-smi topo -m`、`lspci -tv` 可用于检查拓扑；映射成功也不能单独证明实际 P2P 传输可达。

本实现针对传统离散 GPU 的 64KiB P2P 页面；其它页面大小会明确返回不支持。它不要求加载 `nvidia-peermem`，因为直接调用 `nvidia_p2p_*`；映射机制不是 dma-buf。

参考 NVIDIA 官方 [GPUDirect RDMA 文档](https://docs.nvidia.com/cuda/gpudirect-rdma/)和 [nv-p2p.h 接口定义](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/kernel-open/nvidia/nv-p2p.h)。persistent 映射会将页面保留到显式 put；相比带撤销回调的旧接口，便于测试在进程退出时保持在途 DMA 所需页面有效。

## 4. 编译

在完整 SwarmIO 工作区运行，本目录的 backend 依赖上级现有源码：

```bash
cd SwarmIO/tests/dsa_gpu

# 以下是示例路径，必须替换成目标服务器上与运行版本匹配的路径。
make all \
  KDIR=/lib/modules/$(uname -r)/build \
  IDXD_INCLUDE=/path/to/matching-linux-source/drivers/dma/idxd \
  NVIDIA_INCLUDE=/path/to/matching-nvidia-source/kernel-open/nvidia \
  NVIDIA_SYMVERS=/path/to/matching-nvidia-build/Module.symvers
```

某些专有驱动源码布局为 `kernel/nvidia` 而不是 `kernel-open/nvidia`。
可通过 `modinfo nvidia` 检查驱动，通过 `find /usr/src /var/lib/dkms -name nv-p2p.h -o -name Module.symvers` 查找候选文件；选择与已加载驱动及当前内核匹配的那一组。

分别编译：

```bash
make user
make module IDXD_INCLUDE=/path/to/linux/drivers/dma/idxd \
  NVIDIA_INCLUDE=/path/to/nvidia/header-dir \
  NVIDIA_SYMVERS=/path/to/nvidia/Module.symvers
make check
```

如果出现 `idxd_* undefined`，需检查运行内核是否导出了 SwarmIO 已使用的那些内部符号，不能通过忽略 modpost 错误解决。内部接口随内核版本变化，移植时应同时核对 `idxd` 头文件与运行内核实现。若出现 `nvidia_p2p_* disagrees about version`，应使用正确的 NVIDIA 符号表重新编译。

## 5. 设备准备与加载

该模块复用 SwarmIO 的 DSA 配置逻辑，接管扫描到的第一个 DSA，并配置 **1 group、1 engine、1 dedicated kernel WQ、WQ size 64**。它不是通过 `/dev/dsa/wq*` 提交的用户态程序。

先停止 SwarmIO 及其它 DSA 工作负载。用以下只读命令核对设备：

```bash
sudo modprobe idxd
accel-config list
lsmod | grep -E 'swarmio|idxd|nvidia'
```

在确认设备空闲后，用 `accel-config disable-wq dsaX/wqX.Y` 禁用所选 DSA 上所有已启用队列，再执行 `accel-config disable-device dsaX` 停用设备本身。将 X/Y 替换成实际编号；backend 选择编号最小的已发现 DSA。不要在其他服务使用 DSA 时进行该操作。模块检查所选设备已经解除驱动绑定、所有 WQ 均处于 disabled 状态，否则返回 `EBUSY`。设备随后由 backend 按新配置重新绑定启用。请在加载到卸载期间独占该 DSA，避免其它配置程序并发修改设备。

```bash
sudo insmod dsa_gpu_test.ko exclusive=1
ls -l /dev/dsa_gpu_test
sudo dmesg | tail -40
```

`exclusive=1` 表示调用者确认独占设备；不传会拒绝加载。设备节点默认 0600，ioctl 还要求 `CAP_SYS_RAWIO`。
卸载时复用的 backend 会停用/解绑其接管的 DSA，**不会自动恢复加载前的 DSA 配置**；需要重新配置再运行其它工作负载。

## 6. 运行例子

```bash
# 单次同步：512B 随机 CPU→GPU payload，每条先获取64B GPU SQE
sudo ./dsa-gpu-bench --size 512 --mode sync --direction read

# 批量异步：4KiB 随机 CPU→GPU，32项/batch，最多4个batch在途
sudo ./dsa-gpu-bench --size 4096 --mode batch --direction read --batch 32 --depth 4

# GPU→CPU 与固定50%混合方向
sudo ./dsa-gpu-bench --size 512 --mode batch --direction write
sudo ./dsa-gpu-bench --size 4096 --mode batch --direction mixed

# 检查非完整窗口和只有1项的尾批
sudo ./dsa-gpu-bench --size 4096 --mode batch --direction read --ops 129

# 完整12组矩阵，GPU编号和访问区域可选
sudo bash run_matrix.sh --gpu 0 --mb 2 --ops 65536 --seed 12345
```

参数：`size` 只允许 512/4096；`mb` 是2的幂，范围1～64MiB；`ops` 范围1～1048576；`batch` 范围2～32；`depth` 范围1～16。默认2MiB数据区、65536次操作。两种模式均按窗口完成后才进入下一窗口，实际并发受窗口和 batch 大小约束，`depth` 大于窗口内 batch 数不会增加实际并发。

## 7. 数据正确性与指标

CUDA 初始化 GPU 数据；内核用同一个位置相关的64位模式初始化主机 DRAM。每个目标区域初始取反，避免“未执行第一次复制但仍通过”这一类错误。内核逐条检查取回 SQE 的序号、地址、长度和方向，然后在计时结束后逐64位字校验全部主机区域。CUDA 端在 ioctl 成功返回后，通过 CUDA API 建立顺序，再读取全部 GPU 数据逐字校验，包括没有访问的区域。

计时区间内，SQE 与 payload 都由 DSA 搬运，无 CPU payload 复制回退。任何 DSA 或映射错误都会使测试失败。

输出包括：

* `SQE_ms`：SQE DMA 获取及等待耗时。
* `payload_ms`：payload 描述符构造、提交及等待耗时；不是纯 PCIe 线速时间。
* `total_ms`：窗口循环，包含 SQE 获取、命令校验、payload 搬运；不含分配/映射、初始化、最终 DRAIN、主机/GPU数据校验。
* `end_to_end_IOPS`：完成 payload 请求数 / `total_ms`，每个请求含其 SQE 获取。
* `payload_GBps`：payload字节数 / `payload_ms`，十进制GB/s，不包含 SQE 字节。
* `host_errors` / `gpu_errors`：按64位字统计的错误数，均为0且完成数正确时输出 `PASS`。

同步与批量模式同时改变了 SQE 获取粒度和 payload 提交方式，性能差异包含两项优化，不能全部归因于 BATCH。比较时保持 size、ops、mb、seed、GPU 和 CPU/NUMA 放置相同。可以用 `numactl --cpunodebind=N --membind=N` 固定运行位置；本测试的主机缓冲区由内核 DMA coherent allocator 分配，实际位置还受设备和内核分配策略影响。

## 8. 生命周期、错误处理与卸载

每次 ioctl 内部完成 pin → DMA map → 搬运 → 等待全部已提交任务 → DRAIN → unmap → put。用户程序必须等 ioctl 返回后才能释放 GPU 缓冲区。程序的 RAII 分配对象保证正常/异常退出路径在 ioctl 返回后释放；persistent pin 保障进程收到信号时不会直接撤销在途 DMA 所需的 GPU 页面。

等待记录每5秒告警，但**不会仅因超时就释放 DMA 引用的缓冲区**。若设备永久无响应，ioctl 可能一直等待；若 DRAIN 失败，模块会保留资源并进入等待状态。此时不要尝试强制卸载模块或复位 GPU，收集日志后安排重启。这里采用保留资源的测试策略，没有实现在线设备重置恢复。

正常结束后：

```bash
sudo rmmod dsa_gpu_test
```

映射失败：检查 GPU P2P 能力、persistent API、MIG/vGPU 状态、BAR空间、IOMMU与拓扑。加载失败：检查 `dmesg`、内核/驱动符号版本和其它 WQ 是否启用。校验失败：保留全部输出与日志，不把失败结果作为有效带宽。

## 9. 本次验证范围

开发环境为 macOS，未安装 nvcc，也没有运行 Linux 的 IDXD/DSA 或 NVIDIA GPU；**尚未进行 nvcc 编译、真实 Linux 模块编译、加载或硬件传输验证**。`make check` 会用本机C++编译器编译真实共用头，检查ABI大小、两种粒度、多个seed/方向、随机置换、窗口边界、GPU页边界、尾批补齐和数据模型。该检查不替代目标平台验收。

首次在目标机器运行时，先用 `--ops 1 --mode sync` 分别验证 read/write，再用 `--ops 129 --mode batch` 验证尾批，最后运行完整矩阵。只有编译成功、全部输出PASS并且内核无异常后，才进行性能比较。
