# lab230-s6：SwarmIO + BaM 的 DSA payload 基线

本文为同步对照模式。异步BATCH实现和新配置见 [异步批量测试说明](dsa-bam-async.zh-CN.md)。

目标环境：Ubuntu `6.8.0-138-generic`，NVIDIA `595.99.02`，CUDA Toolkit/nvcc `13.2.86`。
已确认启动参数 `intel_iommu=off memmap=24G$4G`；GPU0 为 RTX PRO 5000 Blackwell，
PCI `0000:2a:00.0`、NUMA0；DSA0 为 `0000:75:01.0`、NUMA0。

配置文件 `configs/bam-dsa-lab230.json` 使用 CPU8–15，分别为 NUMA0 的8个不同物理核；
其 SMT 同胞48–55没有被隔离，服务器其它业务仍可能造成竞争。本流程不修改 GRUB、隔离核或设备配置。

## 本版执行的路径

* GPU 使用 BaM 的 GPU SQ/CQ、原生 MMIO doorbell，每线程提交一条 READ 后等待 CQ。
* SwarmIO dispatcher 在 CPU 上获取 SQE；4个 worker 各借一个 payload WQ：`wq0.1/0.3/0.5/0.7`。
* payload 由 DSA MEMMOVE 执行，512B/4KiB 每条请求一条 DMA。每个 worker 同步等待 DMA 完成，
  是4条同步链并行的正确性/性能基线，不是DSA BATCH或异步多描述符流水线。
* GPU 内存通过 persistent P2P API 钉住，明确针对 **DSA 的 PCI设备** 建立 DMA 映射。
  软件 GPU pin 预算为32MiB，包含数据缓冲区和GPU SQ/CQ，不自动增大。
* 预留区前4GiB为内部区域，后20GiB为易失仿真存储。每次模块加载只初始化 namespace 前64MiB的验证模式。
* DSA失败不回退CPU。记录实际 submitted/completed/bytes/errors/timeouts，并核对本轮增量。
* 仅借用已启用的 DMAengine 通道，卸载仅归还通道；不解绑、停用、重置整颗DSA，也不重配WQ。

不直接使用已有 `tests/dsa_gpu` 的1024项BATCH计数来推断本基准能力。
这是一套明确标记的同步 payload 基线，后续流水线优化应保持单独版本和结果标识。

## 1. 同步源码和检查设备

同步主项目的更改以及 **bam 子模块的工作区更改**；只同步父仓库提交不会携带未提交的子模块修改。
新文件包括本说明、`benchmarks/bam_dsa_read.cu`、`src/swarmio_pattern.h` 和 DSA 配置。

```bash
cd ~/SwarmIO
cat /proc/cmdline
bash tests/dsa_gpu/diagnose.sh
sudo accel-config list -i
bash tests/dsa_gpu/prepare_dsa.sh dsa0 --check
```

不能有 `dsa_gpu_test` 残留、going/Unloading模块或内核Oops。此前异常必须在维护重启后再测试；不强制卸载。
WQ应已配置为 dedicated/kernel、绑定dmaengine，每组有engine及WQ。此处只借用奇数WQ，
偶数WQ不会被本版申请。若设备空闲且完全disabled，准备脚本会提示可以进行一次性配置；
核对设备可用于实验后才手工执行：

```bash
sudo bash tests/dsa_gpu/prepare_dsa.sh dsa0 --apply
```

该准备脚本采用4group/4engine/8WQ、batch1024的独立测试配置，足以支持本版single DMA。
已enabled但配置不匹配时它拒绝修改，不自动停用其它业务。DSA基线仅支持已经验证的`intel_iommu=off`，
不把`iommu=pt`或PASID地址当作物理地址使用。

## 2. 检查并构建到新目录

先复用此前成功编译 `tests/dsa_gpu` 时的 IDXD 目录，它必须匹配发行版6.8.0-138的内部结构。
`/usr/src/linux-headers-6.8.0-138-generic` 本身一般不包含私有的 `drivers/dma/idxd/idxd.h`。

```bash
cd ~/SwarmIO
python3 scripts/swarmio_bam.py --config configs/bam-dsa-lab230.json \
  --build-dir build-dsa-68 doctor \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd

python3 scripts/swarmio_bam.py --config configs/bam-dsa-lab230.json \
  --build-dir build-dsa-68 build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd
```

以上路径按实际安装位置替换。NVIDIA输入默认查找：

```text
/usr/src/nvidia-595.99.02/nvidia/nv-p2p.h
/var/lib/dkms/nvidia/595.99.02/6.8.0-138-generic/x86_64/module/Module.symvers
```

非标准路径可在doctor/build后加 `--nvidia-include`、`--nvidia-symvers`。
构建核对架构、NVIDIA符号CRC、DSA模块标记和实际DMA接口；不加载、安装或重配模块。
IDXD内部头无稳定ABI，文件存在并不证明与运行驱动匹配，最终仍需真实编译和小规模运行验证。

## 3. 加载并检查

如果已有旧SwarmIO/libnvm，先使用其原构建目录正常unload；失败则保留现场，不能叠加加载。
本版CPU SQ fetch、DSA payload不可与 `dsa_gpu_test` 同时占用相同队列。

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 load
cat /sys/module/swarmio/parameters/dsa_state
cat /sys/module/swarmio/parameters/worker_using_dma
cat /sys/module/libnvm/parameters/dma_peer_bdf
```

预期 `device=0 borrowed=4 fault=0`、worker为`Y`，GPU DMA peer为`0000:75:01.0`。
初始计数不要求为0，以每轮运行前后的增量为准。load只绑定SwarmIO虚拟NVMe，不接管真实SSD。

## 4. 先内容校验

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 run \
  --threads 64 --reqs 1 --num-queues 1 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --verify

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 run \
  --threads 64 --reqs 1 --num-queues 1 --queue-depth 512 \
  --page-size 4096 --num-blks 16384 --random --verify
```

每条请求使用独占GPU缓冲页，`--verify`强制每线程仅一次请求，CUDA kernel结束后校验每个请求的全部字节。
必须看到 `Verification: PASS` 及DSA增量64条完成，字节数分别32768、262144，errors/timeouts均为0。
访问范围必须落在64MiB验证区，不能把未初始化存储当作已知模式校验。

## 5. 随机读性能

两次内容校验成功后再运行，开始用64MiB地址范围，两轮各1048576条READ：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 run \
  --threads 1024 --reqs 1024 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --monitor-interval 0.1

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 run \
  --threads 1024 --reqs 1024 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 4096 --num-blks 16384 --random --monitor-interval 0.1
```

性能轮次不逐字节校验，但DSA实际完成数必须1048576，字节数分别536870912和4294967296。
`run.json`包含 `payload_backend=dsa-borrowed-single`、`sq_backend=cpu`、负载参数、GPU信息和DSA计数边界/增量。
不包含BaM历史helper的额外READ，因此不能不加说明地对比旧历史逻辑IOPS。
CPU基线需要另行构建/正常装卸后测量，固定请求数、地址范围、队列、CPU/NUMA位置和监控配置。

## 6. 正常退出与失败现场

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 status --output results/dsa-state.log
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 unload
sudo accel-config list -i
```

正常卸载后DSA和WQ应保持原来的enabled状态。若运行失败或DMA超时，脚本拒绝复用失败会话，
不自动重试、不强杀、不卸载或复位设备。DMA尚在途时保留描述符并等待真实完成；永久无响应时，
线程/正常卸载仍可能等待，需要维护恢复。GPU清理失败时保留拥有的句柄和模块引用，拒绝新的映射。
这些措施避免已知的整DSA解绑和提前释放路径，不承诺在硬件/驱动故障后可以在线恢复。

开发机是macOS，不能执行真实Linux/nvcc编译和DSA/GPU传输。接口替身测试仅验证控制流和资源所有权，
服务器上的构建、上述内容校验及正常卸载仍是必要验证。
