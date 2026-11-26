# Author Baseline 采集说明

> 此文保留旧的 schema 1 大矩阵及历史命令，仅供追溯；下文的 `scripts/` 基线入口已移除。
> 当前入口为 `script/baseline/run_paper_baseline.sh`，执行方法与矩阵以 [精简版 schema 2](paper-baseline-compact.zh-CN.md) 为准。

本轮只执行作者 `random_access_kernel`，通过 `SWARMIO_COMPARE_KERNEL=author` 固定选择。
沿用当前 BaM 的单控制器、随机 READ、每线程固定地址并重复请求的测试方法，保留主 READ、额外 READ 和 CQ 回收协议。
不执行 `dsa-read-v1`，也不采用它逐轮生成新随机地址的访问方式。
这是一组 RAM 后备的仿真框架吞吐实验，不是物理 SSD 的吞吐，也没有增加逐请求数据校验。

保留已经验证的故障退出路径：`BAM_SWARMIO_FAULT_EXIT=1`，轮询间隔为 4096。
所以这里的 “author” 指作者的 GPU 测试 kernel 和协议；环境仍包含已有的兼容性与安全修复，不能描述成完全未经修改的上游系统。

## 执行步骤

在服务器 SwarmIO 项目目录执行。脚本不改变当前目录，不依赖 `HOME`，也不依赖 perf。
开始前需要原模块和双 DSA 正常加载、`libnvm` 已绑定虚拟设备，且没有其他 benchmark。
不需要手工卸载；所有构建在首次模块切换前完成。

先运行完整流程的缩小版本，检查新增的时延模块能否在当前内核上编译、采样和恢复：

```bash
sudo bash scripts/run_paper_baseline.sh --quick --run --out results/Baseline-author-smoke
```

成功结束后检查恢复状态：

```bash
cat results/Baseline-author-smoke/status.json
cat results/Baseline-author-smoke/restored.json
```

`status` 应为 `complete`，`restored` 为 `true`。冒烟结果仅检查流程，不能用于论文性能结论。
若失败，保留同名 `.tar.gz`，先处理失败原因再进行正式采集。

生成正式矩阵计划，查看总配置数，不访问 GPU、不加载模块：

```bash
bash scripts/run_paper_baseline.sh --out results/Baseline-author
```

正式运行，使用完全相同的输出目录：

```bash
sudo bash scripts/run_paper_baseline.sh --run --out results/Baseline-author
```

默认矩阵有 **3030 个有效配置**，每配置预热一次、测三次；还有组首/组尾对照、独立长测、拓扑与时延采集。
低吞吐配置也会完成固定数量的请求，所以可能运行数小时至数天。正式采集期间保持这两张 GPU 和测试 CPU 空闲。
控制台输出可在终端观察，配置、数据、各次原始日志和状态均写入结果目录。

如果缺少私有 `idxd.h`，使用这台服务器此前成功构建所用的源码目录：

```bash
sudo env IDXD_INCLUDE=/home/zhaohq/swarmio-deps/6.8.0-149-generic/ubuntu/linux-hwe-6.8-6.8.0/drivers/dma/idxd \
  bash scripts/run_paper_baseline.sh --run --out results/Baseline-author
```

通常不需要设置此变量：脚本先检查既有的 `dsa/.*.o.cmd` 和 `src/.*.o.cmd`，恢复成功编译时的包含路径。
已显式设置但无效时会停止，不会悄悄使用另一套头文件。

Ctrl+C 会请求 BaM 排空，等待程序退出，然后按开始前保存的参数恢复原模块与绑定。
请等待恢复结束；不执行强制卸载或强制终止 DMA 程序。如果排空/卸载失败，立即停止恢复步骤并保留错误日志。
同一套源码、内核、驱动和 GPU 环境下，重新运行**完全相同的命令及 `--out`**，跳过已经成功保存的试次。
中断试次的日志留在独立 `attempt-*` 目录，续测会重新执行该试次。
修改配置、源码、内核或 GPU/驱动后应使用新目录。

仅重新生成表格，不操作设备：

```bash
python3 scripts/paper_baseline.py --aggregate-only --out results/Baseline-author
```

完成或中断后会生成 `results/Baseline-author.tar.gz`；归档包含较多原始日志和构建文件。
可以用 `--no-archive` 暂缓压缩，之后自行打包整个结果目录。

## 矩阵与固定条件

| 项目 | 默认配置 |
|---|---|
| READ 大小 | 512 B、4096 B；namespace LBA 固定 512 B |
| GPU | CUDA ordinal 0、1；实际 PCI BDF、UUID、NUMA 记录入结果 |
| 线程数 | 1024、2048、4096、8192、16384、32768、65536 |
| QP 数 | 8、16、32、64、128、256 |
| QD | 每个 SQ/CQ：64、128、256、512、1024 |
| 数据/cache 范围 | 均为 128 MiB；4 KiB 最大 32768 线程 |
| DSA | 单物理 dsa0、单物理 dsa2、双 DSA |
| DSA BATCH | 最多 32 个子描述符；外层槽位/worker WQ 深度均为 30 |
| DSA 分组 | 每 DSA 4 group，每 group 1 engine、2 WQ，深度 2/30、优先级 1/15 |
| CPU 绑定 | dsa0：8–15；dsa2：28–35；每 DSA 4 dispatcher + 4 worker |
| 模型 | 8 个调度实例，512 B 单元，调度间隔 100 ns、最小附加时延 49900 ns |
| 每个扫描试次请求数 | 512 B：67108864；4 KiB：16777216；各线程均分，固定总工作量 |
| 重复与验证 | 扫描每点 3 次；候选最优点独立测 5 次，按扫描速率估算至少 20 秒的工作量 |

线程数必须是 `32 × QP` 的整数倍，保证作者的 warp/QP 分配对应相同的主 READ 工作量；线程数也受数据缓冲容量限制。
`skipped.csv` 保存跳过的配置。这里的线程指 GPU 线程；QP 和 QD 指 NVMe 队列，不是 DSA WQ。
QD 扫描低于 64 或增加线程上限可通过 CLI 调整，所有固定请求总数必须是最大线程数的整数倍。

按以下次序采集：

1. `single`：两颗物理 DSA × 两张 GPU × 两种 READ 大小，各自扫描线程/QP/QD 的完整笛卡尔积。
2. `dual-equal`：原始交错 QID 分配，均分主 READ 工作量，完整线程/QP/QD 矩阵。
3. `dual-contiguous`：256 QP、连续 QID、128:128，扫描线程/QD。
4. `dual-weighted`：256 QP、连续 QID，扫描线程/QD/DSA0 QP 数。
   DSA0 QP 数为 64、80、96、100、104、112、128、144、152、156、160、168、176、192；DSA2 为 `256 - DSA0`。
   两张 GPU 使用同一个权重范围，能覆盖 GPU 更换后的拓扑反转；没有预设 156:100 必然最优。
5. 从扫描中选每场景整体及各轴条件下的最好配置，独立长测。
6. 选定一组单 DSA 本地最优几何配置后固定线程/QP/QD，测试四条 GPU–DSA 路径，便于直接比较。
   双 DSA 同时保留各自最优点，以及在加权最优点几何配置下的原始均分/连续均分对照。
7. 同一诊断几何配置分别运行无插桩模块与采样模块，记录吞吐变化和分 DSA/WQ 的阶段耗时。

每个模块分组的首尾都有固定点复测，记录长扫描过程中的漂移。
“最大”指**配置网格内独立复测得到的最好中位数**，不是硬件的数学上限。
长测的请求数量根据扫描结果估算；最终实际 kernel 时长以 `min_elapsed_s` 为准，不承诺每次必定超过 20 秒。
扫描与长测请求数不同，绘图时明确区分 `scan` 与 `confirm`，不能混在同一组重复试次里。

## 时延含义

时延只在 `*-latency` 隔离模块中采集，每个 WQ 每 64 次调用采一个样本。
GPU kernel 仍是 `author`。`ktime_get_ns()` 记录 CPU 观察到的墙钟耗时，包括可能的抢占/中断影响，不是设备时间戳。

| metric | 起止点 | units |
|---|---|---|
| `sq_sync` | 合并 SQE 的同步 DSA 调用之前，到调用成功返回；含描述符准备、提交、轮询及该调用的回收 | 该次搬运的 SQE 数 |
| `worker_wait` | 进入 `__dsa_sync_wait()` 前，到观察到外层 BATCH 完成后 | 该批真实子描述符数，排除填充 NOOP |
| `worker_reclaim` | 上述轮询返回后的 issued 位/链表清理，到状态检查、回调和 `num_filled` 重置结束 | 同上 |

正常的 oldest-first 和 drain/all 回收路径均有采样。
回收回调会标记数据就绪，并在时序模型允许时发布 CQE；此指标不包含 GPU 对 CQE 的等待，也不包含之后的自适应超时计算。
`worker_wait` 可能观察到一个已经完成的 batch，所以它是 **CPU 实际轮询等待耗时**，不能当作数据 DMA 的完整在途时延。
`sq_sync` 是每次合并搬运的延迟；除以 SQE 数得到的摊销成本不是单条 SQE 的端到端延迟。
worker 的 units 是搬运段，不能不加区分地改称主 READ I/O 数；作者的额外 READ 也会通过这些路径。

均值使用 `sum_ns / samples`；`amortized_ns_per_unit` 使用 `sum_ns / units`。
直方图为 2 的幂次纳秒分桶，`p50_upper_ns/p95_upper_ns/p99_upper_ns` 是**对应桶的上界**，不是精确分位数。
例如桶 9 代表 256–511 ns。最后一个桶饱和时输出 `overflow`。
计时没有扣除时钟读取开销；采样统计也会影响调度，不能把插桩运行的 IOPS 放进吞吐上限表。
`profile-overhead.csv` 给出同几何配置下相对无插桩运行的 IOPS 变化，以便检查扰动。

## 结果集与绘图建议

| 文件 | 内容与用途 |
|---|---|
| `baseline.json`、`plan.csv`、`skipped.csv` | 固定参数、测试范围与跳过原因 |
| `runs.csv` | 每次运行；含预热、扫描、长测、对照和时延试次，必须按 `stage` 筛选 |
| `summary.csv` | 各点各阶段的中位数、标准差、CV、范围与实际 kernel 时长；排除预热 |
| `best.csv` | 仅 `confirm` 的整体/各轴条件最优，保留对应的完整几何配置 |
| `controls.csv` | 同一点组首与组尾 IOPS 的漂移百分比 |
| `latency.csv` | 分试次、物理 DSA、WQ 的时延样本统计和原始桶计数 |
| `latency-summary.csv` | 同一点分 DSA 的跨 WQ/重复合并结果；按样本加权，不直接平均 WQ 均值 |
| `profile-overhead.csv` | 插桩与无插桩的吞吐差异 |
| `trials/` | 原始 BaM 日志、逐次配置、实际 QID 分配、BAR1、CPU 利用率、GPU 状态、内核日志增量 |
| `environment/`、`host.json`、`gpu-map.json` | 每次启动的软硬件、NUMA、CPU governor、GPU UUID/BDF、驱动和运行模块 Build ID |
| `modules/`、`bam-build/`、`code/`、`provenance.json` | 隔离源码、构建命令、程序和源码指纹 |
| `restore.log`、`restored.json`、`status.json` | 恢复结果和任务状态；`status != complete` 表示数据集尚未采完 |

推荐先绘制：

- 单 DSA 的线程–QD 热图，按 QP、READ 大小和本地/远端路径分面。
- 固定其他参数的线程/QP/QD 曲线，以及各轴条件最优的包络线；包络线的其他参数会变化，图注需写明。
- 原始双 DSA 均分、连续均分、加权的最高吞吐与**相同几何配置**对照。
- GPU0/1 × NUMA0/1 的固定几何吞吐矩阵，以及分 DSA 的 SQ/worker wait/reclaim 时延。
- DSA0 QP 权重–吞吐曲线，观察 GPU 更换后最佳权重是否反转。
- 额外 READ 比例、插桩扰动、重复试次 CV 和组首/尾漂移，作为结果可信度的辅助材料。

带宽以主 READ 的有效字节数计算：`IOPS × block_bytes`。同时输出十进制 GB/s 和二进制 GiB/s。
`extra_reads` 单独计数，`inferred_nvme_reads` 与 `inferred_nvme_read_bytes` 用来分析作者协议的额外 READ 开销。
不要把主请求 IOPS 与包含额外协议请求的命令数混用。
作者 kernel 每个线程只选一次随机地址，之后重复读取。因此增加线程数也会增加地址工作集，线程曲线同时受到并发和缓存局部性影响，不能全部归因于锁竞争。
`source_working_set_upper_bytes` 仅是 `min(线程数, 地址块数) × READ 大小` 的上界，并不是测得的唯一地址数；128 MiB 是地址范围，不代表每个配置都实际覆盖全部范围。
CPU 利用率来自整个 benchmark 进程运行期间的 `/proc/stat` 差值，包含初始化/销毁；它不是纯 CUDA kernel 区间，也不能换算成每 I/O 时延。
GPU 温度、功耗、时钟仅为前后快照，不是时间平均功耗或峰值跟踪。
当前固定预留 RAM 的 NUMA 归属不应凭 CPU 绑定推断；保留 `iomem`、NUMA 和内存区间记录，论文中区分 GPU–DSA 路径与后备内存归属。

脚本仅操作这轮实验所需的 dsa0/dsa2，不配置 IAX WQ；不安装或覆盖原来的模块，不调用 `depmod`。
模块切换时先排空，再解绑/卸载；任何一步失败均停止后续步骤。
内核日志使用原始单调时间日志的游标，不依赖系统墙钟。新 Oops、DMA 错误或游标丢失都会停止采集。
每次运行都检查实际 QP/QD、主请求数和实际 QID 覆盖/DSA 比例。
BAR1 每次运行前重新查询，数据与每个 SQ/CQ 分别按 64 KiB 取整，再保留 64 MiB。

## 本地验证范围

本地测试覆盖网格约束、作者 kernel 保持、QID 分配核对、日志游标、采样计数/分桶、条件最优汇总、续测与卸载失败的停止行为。
当前开发机无法运行服务器 CUDA benchmark 或编译/加载这套 Linux 内核模块；新增诊断模块仍需先通过上述服务器冒烟流程。
