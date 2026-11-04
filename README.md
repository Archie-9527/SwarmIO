# SwarmIO + CoPilotIO / BaM

本分支使用 `copilotio/copilot-io` 作为默认 GPU I/O 引擎，同时构建
`copilotio/bam` 作为对比基线。两套用户库各自放在独立输出目录，共用同一份
`libnvm.ko` 和 SwarmIO CPU full-I/O 配置。原命令名 `swarmio_bam.py` 保留。

本地回归检查不等于 GPU 实测通过。当前开发机无法编译 Linux/CUDA 或执行 GPU I/O；
随机读的运行稳定性、吞吐和内容正确性仍需在目标机验证。本基准检查 NVMe 完成状态，
不做逐字节内容比对，也不声称已解决此前所有随机读停顿或故障后的卸载问题。

## 代码适配

- 入口从已移除的 `bam/` 改为 `copilotio/`，一次构建两套隔离库与可执行文件，
  并构建随附 GDRCopy 用户库、`gdrdrv.ko`。不构建或加载 `nv-p2p-dummy`，使用实际 NVIDIA DKMS CRC。
- 两套 libnvm 继承原先的 miscdevice、Linux 6.8/NVIDIA 550 P2P 兼容实现。
  两份内核源码必须一致，否则 doctor 拒绝共用驱动。
- 用 CUDA/CCCL 原子操作替换旧 freestanding libc++；显式选择架构，CUDA C++17、CMake >= 3.22。
- 统一有限次读基准 `copilotio/microbenchmark/swarmio_read.cu` 编译两次。
  BaM 使用 GPU SQ/CQ，CoPilotIO 使用 CPU SQ/CQ 和 GDRCopy 完成通知。
  每次逻辑操作只发一条 NVMe READ，不含原 `read_data` 路径的额外读和页缓存队列记账。
  因此这里的结果不能直接与旧版含额外读的 BaM 数字比较。
- 相同线程、请求数、确定性随机序列、读范围、请求大小、QD、队列数，无隐藏预热、
  强制 16 队列或 4 GiB 缓冲区。每个线程独占缓冲页，`pages >= threads`。
- 修正 CPU 通知元数据先于 ready 发布、原子停止标志、线程创建失败回收，
  保留 CPU CQ 的真实主机地址，加入 SQ 发布系统屏障和 CQ volatile 读取。
  轮询线程使用普通调度，CPU 编号必须可用且不与 SwarmIO 服务核重叠。

## 目标机准备

沿用原目标：Linux `6.8.0-multistream`、NVIDIA 专有驱动 `550.163.01`、CUDA/CCCL、
支持 P2P 的 Tesla/数据中心 GPU（计算能力 >= 7.0），Python >= 3.9。
需要当前内核构建树和对应 NVIDIA DKMS `Module.symvers`、`nv-p2p.h`。
不需要额外安装 GDRCopy，构建会使用子仓库内的版本。

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
`copilotio/{lib/libnvm.so,bin/nvm-block-bench}` 和 `gdrcopy/src/` 下的库/驱动。
`manifest.json` 记录源码、Git 状态、命令、环境和全部产物哈希。

加载仍只绑定 vendor/device=`0c51:0110` 的 SwarmIO 虚拟控制器，不接管物理 SSD。
已有 swarmio/libnvm/gdrdrv 时拒绝自动替换。`status`、失败保留现场、SIGINT 和正常
`unload` 的原有规则保留；不强杀、不自动重跑、不强制卸载。

## 两个引擎对比

同一次加载后，顺序运行 BaM、CoPilotIO，参数相同，任一轮失败立即停止：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-copilotio compare \
  --threads 1024 --reqs 8 --num-queues 8 --queue-depth 512
```

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
