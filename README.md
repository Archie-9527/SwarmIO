# SwarmIO + CoPilotIO / BaM：Linux 6.8

当前目标为 Linux **6.8.0 系列**（完整后缀以 `uname -r` 为准）、NVIDIA 驱动
**580.196.07**，编译工具链按此前已确认的 **CUDA Toolkit/nvcc 12.4.131**。
`nvidia-smi` 的 `CUDA Version: 13.0` 是驱动支持上限，不代表当前安装的是 Toolkit 13.0。
脚本按实际运行内核选择构建树与 NVIDIA DKMS 符号表。

一次构建两套隔离的库和可执行文件，共用 SwarmIO 和 libnvm 内核模块：

| 项目 | BaM | CoPilotIO |
| --- | --- | --- |
| 源码 | `copilotio/bam` | `copilotio/copilot-io` |
| SQ/CQ | GPU 内存 | CPU 内存 |
| 完成检测 | GPU 轮询 CQ | CPU 轮询 CQ，GPU 等待通知 |
| 默认通知位置 | 无独立通知 | CUDA 映射 CPU 内存，`notify_memory=host` |
| 默认 doorbell | GPU 直接 MMIO | GPU 直接 MMIO |

保留之前的命令名 `scripts/swarmio_bam.py`。
本地开发机没有 Linux/CUDA 执行环境，下面的构建与 GPU 验证需在目标服务器运行。
本地回归检查不等于真实 nvcc 编译、随机读稳定性或数据内容校验通过。

## 1. 确认服务器输入

```bash
uname -r
nvidia-smi
/usr/local/cuda-12.4/bin/nvcc --version
dkms status
cat /proc/cmdline
```

切换内核后不能复用为 6.18 编译的 `.ko`，也不能复用其 NVIDIA Module.symvers。
当前配置 `configs/bam-cpu.json` 仍采用：

- `memmap=24G$4G`：4 GiB 起预留 24 GiB，内部区域占 4 GiB，约 20 GiB namespace；
- CPU full-I/O，关闭 DSA，512 B 时序单位；
- 4 个 dispatcher + 4 个 worker，CPU 8–15；
- CoPilot CPU 轮询默认使用 CPU 0–3，不与服务核重叠；
- 512 B 请求，2 MiB 缓冲区，1 GiB 地址范围；支持显式改为 4 KiB。

确认切换内核后预留参数仍存在。换机器时先调整地址和 CPU，不能把未预留的 RAM 用于仿真器。

CoPilotIO 的连续 CPU 队列在 QD=512 时每个 QP 需要两个空闲 2 MiB hugepages。
8 QP 需要 16 页；可根据当前机器使用情况预留足够页数，例如：

```bash
grep -E 'HugePages|Hugepagesize' /proc/meminfo
sudo sysctl -w vm.nr_hugepages=128
```

脚本不会自动更改 hugepage 配置。QD<=64 的队列不要求 hugepages。

## 2. 检查并重新构建

从 SwarmIO 根目录使用一个全新的构建目录：

```bash
python3 scripts/swarmio_bam.py doctor --cuda-root /usr/local/cuda-12.4
python3 scripts/swarmio_bam.py --build-dir build-68 build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4 --notify-memory host
```

要求 Python >=3.9、CMake >=3.22、当前内核构建树、CUDA/CCCL、NVIDIA DKMS 输入。
NVIDIA 路径非标准时，doctor/build 支持 `--nvidia-include` 和 `--nvidia-symvers`。
构建固定 CUDA C++17、实际 GPU 架构和工具链根目录，禁用模块 BTF 生成；不安装或加载模块。

默认保留已选定的 host 通知模式，不需要加载 gdrdrv；GDRCopy 用户库仍由随附源码构建。
若要另行测试原始 GDR 通知，使用**不同构建目录**并选择 `--notify-memory gdr`。
该选择保存在 manifest 中；脚本根据实际导入符号审计 gdrdrv，不因内核变为 6.8 就跳过审计。
不能通过修改已有 manifest 来切换模式。

产物与日志：

```text
build-68/manifest.json
build-68/build.log
build-68/swarmio/swarmio.ko
build-68/libnvm/libnvm.ko
build-68/bam/{lib/libnvm.so,bin/nvm-block-bench}
build-68/copilotio/{lib/libnvm.so,bin/nvm-block-bench}
build-68/gdrcopy/src/libgdrapi.so
```

只有 GDR 通知构建还包含 `gdrdrv.ko`。构建会检查模块内核版本、CPU full-I/O、
NVIDIA P2P CRC、库路径和全部产物哈希。已有目录拒绝覆盖，失败日志保留。

## 3. 加载并先检查 GPU 映射

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-68 load
sudo python3 scripts/swarmio_bam.py --build-dir build-68 probe --engine bam
sudo python3 scripts/swarmio_bam.py --build-dir build-68 probe --engine copilotio
```

加载只接管 vendor/device 为 `0c51:0110` 的 SwarmIO 虚拟控制器，必须确认 `/dev/libnvm0` 属于它。
`probe` 不 reset 控制器、不创建 NVMe 队列、不发 NVMe 读写命令；依次检查
BAR/doorbell 注册、host DMA 映射、GPU P2P DMA 映射及所选 CoPilot 通知分配，并释放测试映射。
成功输出 `Probe OK:`。它通过仅说明注册和映射阶段通过，仍需下面的实际 I/O 检查。

如果 `probe` 失败，先保存日志并处理该阶段；不要直接启动随机读。
任何失败不自动重试、不强杀、不自动卸载。内核异常或不完整负载记录要求保存现场并重启。

## 4. 最小读与随机读对比

先分别运行默认小顺序读（64 线程，每线程 1 次请求，单队列）：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-68 run --engine bam
sudo python3 scripts/swarmio_bam.py --build-dir build-68 run --engine copilotio
```

两轮均正常完成后，对比相同参数的随机读：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-68 compare \
  --threads 1024 --reqs 8 --num-queues 8 --queue-depth 512
```

也可沿用单轮 CoPilotIO 快捷入口：

```bash
sudo bash scripts/run_random_read.sh build-68 1024 8 8 512
```

上面的旧入口省略 `--batch-size` 时保持每线程一次提交、一次等待，供同步回归。
性能对比使用新的批量微基准入口：**BaM 每线程同步，CoPilotIO 先提交一批，再等待这一批完成**。
这项对比同时改变了提交方式，结果不能解释为仅更换 CQ 轮询者带来的收益。

更新源码后需重新构建；旧二进制不支持批量参数。使用新目录，例如：

```bash
python3 scripts/swarmio_bam.py --build-dir build-68-batch build \
  --jobs 8 --cuda-root /usr/local/cuda-12.4 --notify-memory host
# 如果旧 build-68 的模块仍已加载，先在无负载时正常卸载；失败则保留现场。
sudo python3 scripts/swarmio_bam.py --build-dir build-68 unload
sudo python3 scripts/swarmio_bam.py --build-dir build-68-batch load
sudo python3 scripts/swarmio_bam.py --build-dir build-68-batch probe --engine bam
sudo python3 scripts/swarmio_bam.py --build-dir build-68-batch probe --engine copilotio
```

512 B、1 GiB 地址范围和资源监控示例（两轮各 1,048,576 次 READ）：

```bash
sudo python3 scripts/swarmio_microbench.py --build-dir build-68-batch \
  --threads 512 --reqs 2048 --batch-size 8 --pages 4096 \
  --num-queues 16 --queue-depth 512 --page-size 512 --num-blks 2097152 \
  --monitor-interval 0.1
```

4 KiB 对比只需改请求大小及地址范围计数：

```bash
sudo python3 scripts/swarmio_microbench.py --build-dir build-68-batch \
  --threads 512 --reqs 2048 --batch-size 8 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 4096 --num-blks 262144 --monitor-interval 0.1
```

`swarmio_microbench.py` 默认就是上述 512 B 负载（默认不开监控）；`--dry-run` 只检查参数并显示命令。
它复用统一入口的环境检查、运行记录和监控，不加载模块、不更改 hugepages。
也可以直接使用 `swarmio_bam.py ... compare --batch-size 8`，其他负载参数需按下面的容量约束设置。

批量大小支持 1..32；`--reqs` 始终是每线程总请求数，最后一批不足时按实际剩余数提交。
两套基准使用相同的每线程随机序列、总请求数、地址范围及缓冲池大小。
令 `window = min(batch_size, reqs)`，CoPilotIO 每条未完成请求独占缓冲页，要求：

```text
pages >= threads * window
ceil(threads / num_queues) * window < queue_depth
```

第二项为保守的队列容量约束，避免所有线程尚在提交时占满队列、无法进入完成回收。
例如原来的 4096 线程、16 队列、QD=512 不能直接设 batch=8；示例改用 512 线程，
每队列最多 32×8=256 条未完成请求。增加每线程 reqs 到 2048 后，总操作数仍为 1048576。
BaM 每线程只使用一个缓冲页；CoPilotIO 每线程复用自己的 window 个缓冲页，完成一批后才复用。
CoPilotIO 由 CPU 轮询 CQ、发布状态与队列回收表，GPU 等待 CID 通知并独立推进 CQ/SQ，
无需按请求提交顺序等待 CQ 槽位回收；host 模式下回收表也使用 CPU 映射内存，不依赖 gdrdrv。

每次逻辑操作一条 NVMe READ，不含旧版 `read_data` 的额外读取，所以不直接对比旧历史 IOPS。
当前基准仅支持读；检查 NVMe 完成状态，不做逐字节内容校验。8192 次请求是短路径检查，
性能测量应使用更长负载和重复测量，且不能把采样开销忽略。

每轮有 `run.json`、`bench.log`、运行前后现场。compare 另生成 `comparison.csv/json`，
标注 `notify_memory`、`doorbell_mode`、`submission_mode`、`batch_size` 和 `effective_batch_size`；
日志应显示 BaM 的 `Submission mode: sync / Batch size: 1`，CoPilotIO 的 `batch / 8`。
probe 的日志标记为 `operation=probe`，不产生 IOPS 对比。
监控可生成资源 CSV、HTML/SVG；`--bar-latency` 需与 `--monitor-interval` 配合。

## 5. 显式兼容模式与清理

回到 6.8 不代表 NVIDIA 580 的 GPU BAR 注册一定成功。保留显式的
`--doorbell-mode relay`：GPU 写普通映射 CPU 内存，一个 CPU 线程转发到真实虚拟寄存器。
不注册 BAR 到 GPU，不自动回退。两引擎比较时必须都用这一模式，结果单独标记；
额外 CPU 开销不能混入原始 GPU MMIO 性能数字。
在干净的加载会话中可先检查，再进行兼容模式对比：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-68 probe --engine bam --doorbell-mode relay
sudo python3 scripts/swarmio_bam.py --build-dir build-68 probe --engine copilotio --doorbell-mode relay
sudo python3 scripts/swarmio_bam.py --build-dir build-68 compare --doorbell-mode relay
```

状态采集及正常卸载：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-68 status --output results/state.log
sudo python3 scripts/swarmio_bam.py --build-dir build-68 unload
python3 -m unittest discover -s tests -v
```

已有模块不自动替换；失败后保留现场，不使用强制卸载。
本分支同时修改父仓库与 `copilotio/` 独立 Git 工作区，上传时必须包含两边的修改和新增头文件。
不要复制旧构建目录。历史兼容问题与保留的实现说明见 [兼容说明](docs/compatibility-notes.md)。
