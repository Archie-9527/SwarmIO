# 精简版 Author Baseline（schema 2）

全程执行作者的 `random_access_kernel`，保留原来的主 READ、额外 READ 和 CQ 协议。
保留 `BAM_SWARMIO_FAULT_EXIT=1`、故障轮询间隔 4096；不执行 `dsa-read-v1`。

当前采集代码统一放在 `script/baseline/`，文件职责见该目录的 [README](../script/baseline/README.md)。
旧的扫描器、author/dsa-read 对照入口和独立 DSA2/权重实验脚本已移除；其基线所需构建和检查逻辑已收进此目录。
项目通用的编译、加载和卸载脚本仍在 `scripts/`，采集器会调用其中的 `load.sh`、`load_bam.sh`。

BaM 需要含 SwarmIO 故障退出路径的版本，例如服务器已验证的 `2a08f65`；仅更新本仓库不会自动改变本地 BaM 子模块版本。
采集器保留作者 GPU kernel，只在隔离目录 `results/baseline/build-author-poll4096/` 构建主机端校验和故障轮询适配。
保留现有 `bam/build/CMakeCache.txt` 中的 CUDA/CMake 设置；必要时通过 `CMAKE_ARGS` 传递构建选项。
旧 `results/bam-iops-compare/` 构建目录不再使用。

## 精简后的路径

| 测试 | GPU / DSA | 扫描维度 |
|---|---|---|
| 单 DSA 吞吐 | GPU0 → dsa0 | 线程 × QP × QD |
| 双 DSA 原始均分 | GPU0 → dsa0+dsa2 | 线程 × QP × QD |
| 双 DSA 连续 QID 均分 | GPU0，128:128 | 线程 × QD，固定 256 QP |
| 双 DSA 邻域加权 | GPU0，DSA0 QP 为 152/156/160，DSA2 为余数 | 线程 × QD，固定 256 QP |
| 单 DSA 拓扑对照 | GPU0/1 × dsa0/2 | 每种 READ 大小固定一组相同的线程/QP/QD |
| 双 DSA 阶段时延对照 | GPU0 | 用加权最优点的相同几何配置，比较原始均分、连续均分和加权 |

默认线程为 **8192、16384、32768、65536**。65535 不满足当前 GPU 线程块和 warp/QP 均衡条件，因此使用对齐的 65536。
QP 为 **32、64、128、256**，QD 为 **256、512、1024**，READ 大小为 512 B 和 4096 B。
这里的 QP 指 BaM 实际创建并供 GPU 使用的队列数。作者的控制器初始化先按 `MAX_QUEUES` 协商预留容量，SwarmIO 当前返回 256，再创建 QID 1 到所需 QP 数。
因此 `assigned sq [01-256, stride: 04]` 是预留范围，不代表创建了 256 个队列；dispatcher 会跳过未创建的 SQ。
脚本保留该协商方式，分别校验预留范围、实际创建的 SQ/CQ ID 和深度，并按实际 QID 计算 DSA 工作量比例。
`qid-assignment.csv` 的 `qids/num_qps` 为实际创建的队列，`reserved_qids/num_reserved_qps` 为预留范围。
每点只运行 **3 次**，取中位数；没有额外预热、第二轮长测或组首/组尾复测。
拓扑点已在吞吐矩阵里测过时直接复用这三次结果；只有未覆盖的远端/GPU1 路径再补三次。
时延需要另用插桩模块测三次，这些吞吐数据不计入 `best.csv`。

保留连续 QID 的 128:128 是为了用同一种 QID 布局比较权重的作用，避免把 QID 布局变化带来的收益全部归因于加权。
`weight-comparison.csv` 对每个加权点给出同线程/QP/QD 下，相对原始均分与连续均分的提升。
如果实测没有提升或最优落在邻域边界，照实保留结果，不预设加权一定更快；邻域扫描也不代表所有权重中的全局最优。

## BAR1 与数据缓冲区

BAR1 不再硬编码为 256 MiB。每次运行前都读取实际总量与空闲量。
数据 cache 与 BAR1 是不同参数：扩大 cache 会增加 BAR1 映射需求，但不会自动扩大 GPU 的 PCIe BAR1 窗口。

默认 `--cache-mib auto` 在整轮数据集开始时选定一个大小，之后固定，避免不同配置混用不同 cache：

- 所有参与 GPU 的空闲 BAR1 都足够时，使用 **256 MiB cache**，两种 READ 都能覆盖到 65536 线程。
- 否则使用 **128 MiB cache**，4KB 的 65536 线程跳过，写入 `skipped.csv`；512B 仍测到 65536。
- 两者都放不下时，在首次模块切换前停止。

每个 SQ/CQ 和数据 cache 各自按 64 KiB 取整，再保留 64 MiB 余量。
在 256 QP、QD≤1024 时：

| cache | 队列映射 | 余量 | 所需空闲 BAR1 |
|---|---|---|---|
| 128 MiB | 32 MiB | 64 MiB | 224 MiB |
| 256 MiB | 32 MiB | 64 MiB | 352 MiB |

因此实际 BAR1 扩到 512 MiB 且空闲充足就可容纳完整线程范围；若平台支持，1 GiB 或更大也可以，脚本不会因此增加测试点或缓存到更大值。
`--cache-mib 256` 可强制完整矩阵，空闲不足时停止；`--cache-mib 128` 可固定旧预算。
实际选择和 BAR1 总量保存在 `memory-plan.json`。同一数据集中不改变 cache；改变 BAR1 大小后使用新输出目录。

128 MiB cache 时吞吐扫描 **252 个配置**；256 MiB 时为 **288 个配置**，从原来的 3030 大幅缩减。
默认还包括 6 个未覆盖的拓扑吞吐点、14 个时延配置，均测三次：总计分别 **816 / 924 次 benchmark 进程**。
`--quick` 是更小的流程验证，每点一次；不能用于论文性能结果。
冒烟矩阵包含 64/256 QP 和 QD 512/1024，以覆盖预留数与实际创建数不同的情况。

## 检查与扩大实际 BAR1

先在 SwarmIO 项目目录执行只读检查，自动保存到文件：

```bash
sudo bash script/baseline/check_bar1.sh results/bar1-check.log
```

关注 `BAR1 Memory Usage` 的 Total/Free，以及 `lspci` 中 `Physical Resizable BAR` 的 `BAR 1: current size`、`supported`。
检查脚本不改 GPU 资源，不卸载驱动，不写 `resource1_resize`。

扩大窗口需要主板 BIOS 和 GPU 的 Resizable BAR 支持。
先检查服务器 BIOS 的 Resizable BAR、Above 4G Decoding 以及相应的 64 位 PCIe MMIO 分配设置；具体菜单和支持模式以服务器厂商说明为准。
调整并重启后，重新运行上述检查，确认实际 Total/Free 已增加。
NVIDIA 的 [BAR1 配置说明](https://nvidia.github.io/daqiri/tutorials/system_configuration/#step-8-maximize-gpu-bar1-size) 也提供了固件工具途径。
[Display Mode Selector](https://developer.nvidia.com/display-mode-selector-tool) 的支持列表包含 RTX PRO 5000 Blackwell，但具体可用模式必须以当前工具和 OEM 的支持为准，不能把旧款 GPU 的 8GB 模式命令直接套用。
如果将工具可执行文件放在项目目录，检查脚本会一并保存它的 `--help`。

## 执行命令

更新代码后，在 SwarmIO 项目目录执行。命令不包含 `cd`，不依赖 `HOME`。

```bash
sudo bash script/baseline/check_bar1.sh results/bar1-check.log

sudo bash script/baseline/run_paper_baseline.sh \
  --quick --run --out results/Baseline-author-compact-smoke

cat results/Baseline-author-compact-smoke/status.json
```

确认 `status=complete`、`restored=true`，然后正式运行：

```bash
sudo bash script/baseline/run_paper_baseline.sh \
  --run --out results/Baseline-author-compact
```

默认自动选 cache。如果已确认所有参与 GPU 至少有 352 MiB 空闲 BAR1，并要求完整 65536 线程范围，可以改用：

```bash
sudo bash script/baseline/run_paper_baseline.sh \
  --run --cache-mib 256 --out results/Baseline-author-compact-largebar
```

先只预览计划、不访问 GPU 或切换模块：

```bash
bash script/baseline/run_paper_baseline.sh --out results/Baseline-author-compact
```

自动 cache 的静态计划先按 256 MiB 生成；正式执行时根据实际容量重写有效 `plan.csv` 和 `skipped.csv`。
续测使用完全相同的命令和输出目录。不要用新版继续旧 schema 1 的结果目录。
本次目录迁移和代码整理改变了源码指纹；已有结果可以保留或重新汇总，迁移后首次采集请使用新的 `--out` 目录。
Ctrl+C 会请求 BaM 排空，等待退出，然后恢复开始前的原模块、参数和绑定；不强制杀进程或卸载模块。
排空/卸载任一步失败即停止后续操作，保留日志。

若自动找不到私有 `idxd.h`：

```bash
sudo env IDXD_INCLUDE=/home/zhaohq/swarmio-deps/6.8.0-149-generic/ubuntu/linux-hwe-6.8-6.8.0/drivers/dma/idxd \
  bash script/baseline/run_paper_baseline.sh --run --out results/Baseline-author-compact
```

## 结果与口径

固定条件：DSA batch 上限 32、dispatcher/worker WQ 深度 2/30、worker 外层描述符槽 30，优先级 1/15。
每 DSA 4 group、每 group 1 engine、2 WQ；CPU 分别为 8–15 和 28–35。
时序模型为 8 个调度实例、512 B 单元、100 ns 调度间隔和 49900 ns 最小附加时延。
源地址范围为 128 MiB；扫描每次固定 512B 67108864 个主 READ、4KB 16777216 个，保持不同线程数下的总工作量相同。

| 文件 | 用途 |
|---|---|
| `baseline.json`、`memory-plan.json`、`plan.csv`、`skipped.csv` | 请求参数、实际 cache/BAR1、有效矩阵与跳过原因 |
| `runs.csv`、`summary.csv` | 三次原始结果与中位数/标准差/CV/范围 |
| `best.csv` | `scan` 阶段整体及各轴条件最优；表示网格内实测最好中位数，没有独立长测确认 |
| `weight-comparison.csv` | 相同几何配置下加权相对两种均分的收益 |
| `latency.csv`、`latency-summary.csv` | 分 DSA/WQ/试次的阶段耗时与直方图，以及合并统计 |
| `profile-overhead.csv` | 插桩相对同配置无插桩吞吐的变化；已测过的点复用 `scan` |
| `trials/`、`environment/`、`modules/`、`bam-build/`、`code/` | 原始日志、QID 实际分配、BAR1/CPU/GPU 状态、环境、构建和源码 |
| `status.json`、`restored.json`、`restore.log` | 采集完成与恢复情况 |

最终归档为 `results/Baseline-author-compact.tar.gz`，失败或中断也会保留归档。
仅重新汇总、不操作设备：

```bash
python3 script/baseline/paper_baseline.py --aggregate-only --out results/Baseline-author-compact
```

时延在独立模块中每个 WQ 每 64 次调用采样一次，GPU kernel 始终是 author：

- `sq_sync`：合并 SQE 的同步 DSA 调用前到成功返回，units 为该次 SQE 数。
- `worker_wait`：进入完成记录轮询前到观察到外层 BATCH 完成；是 CPU 实际等待时间，不能当作完整的数据 DMA 在途时间。
- `worker_reclaim`：轮询返回后的位图/链表清理、状态检查、回调和批次重置；不含后续自适应超时计算或 GPU CQ 等待。

worker 的 units 为真实子描述符数，排除尾批填充 NOOP；它不是主 READ I/O 数。
均值为 `sum_ns/samples`，摊销成本为 `sum_ns/units`；直方图分位数是 2 的幂次桶的上界，不是精确分位数。
使用 `ktime_get_ns()` 的 CPU 观察耗时，未扣除时钟读取开销；包含可能的抢占和中断影响。
插桩会扰动调度，因此其 IOPS 不进入最高吞吐结果。

带宽为主 READ 的有效字节数，分别输出 GB/s 与 GiB/s；额外 READ 单独计数。
作者 kernel 每个线程只选择一次随机地址再重复读取，所以线程数变化同时改变并发与工作集，不应全部归因于锁竞争。
预留 RAM 的 NUMA 归属不能仅凭 CPU 绑定推断；保留物理内存和 NUMA 记录供论文分析。
CPU 利用率包含 benchmark 初始化/销毁，GPU 状态是前后快照，均不等同于每 I/O 时延或时间平均功耗。

脚本不安装或覆盖生产模块，不执行 depmod；只用隔离构建与 insmod，实验后恢复原模块。
本地验证不能替代服务器的 CUDA 执行和 Linux 模块编译，正式采集前先运行冒烟流程。
