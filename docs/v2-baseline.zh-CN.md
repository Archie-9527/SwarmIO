# 作者原实现的 Linux 6.8 最小兼容基线

日期：2026-11-12。状态：源码准备及本地检查；尚未在服务器完整构建、加载或取得性能结果。
本轮按用户要求不连接服务器。下面的机器参数是候选值，必须与实际机器核对。
后续静态审计发现原作超时、reset及退出路径的生命周期缺口，见 [卡住问题分析](v2-baseline-hang-analysis.zh-CN.md)。
这些问题尚未修复；开展性能扫描前，需先完成稳定性补丁和基本运行/退出验收。以下命令是准备步骤，不代表当前版本已能安全反复装卸。

## 1. 来源及边界

- 原分支 `dev/v1-numa` 的工作区已保存为 `7721b3e`，提交信息“回退单DSA强制适配”。
- 本分支从作者 SwarmIO `25594a1a763044d3d459fd15ccebc5d44d1690ca` 创建。
- BaM 从作者指定的 `315fadfc5c5c018a64596157bfac94ecbb7d87a2` 创建；作者 `bam_rev.patch` 已单独应用并提交为 `98c3a3a`，不要重复应用。
- 不引入 `swarmio_bam.py`、`bam_dsa_read.cu`、借用 DMAengine 的替代后端、重写的 BATCH 状态机或拓扑路由。
- 保留作者多 DSA 枚举、SU/group 分配、SQE 合并获取、BATCH 自适应提交/回收、worker 回调和完成路径、GPU QP 选择、`read_data()` 命令链、时延模型。
- 单 DSA和双 CPU 双 DSA均使用一个 GPU、一个虚拟 NVMe 控制器，符合作者入口模式；本分支不增加双 GPU 客户端。

## 2. 兼容修改白名单

| 文件 | 修改理由 |
|---|---|
| `Kbuild`、`Makefile`、`scripts/build.sh` | 可指定目标内核与匹配的 IDXD 私有头路径；重新安装时覆盖旧模块，避免加载旧产物 |
| `dsa/dsa.h`、`batch.c`、`single.c` | 跨编译单元 helper 改为普通外部函数，避免 GNU C inline 链接问题；函数体不变 |
| BaM `module/ctrl.*`、`pci.c` | 设备节点改用 miscdevice，适配依赖 NVIDIA 专有模块时目标内核的 GPL-only class/device 接口；节点仍为 libnvmN |
| BaM `module/map.c` | 在头文件支持时使用 persistent pin API，否则保留原 API；保留作者的控制器 mapping 列表，不增加全局 DSA peer 选择器 |
| BaM `CMakeLists.txt` | 可显式指定当前内核的 NVIDIA DKMS Module.symvers；兼容 CMake 3.22 的显式 GPU 架构；每次构建进入 Kbuild |
| 作者 shell 脚本 | 允许覆盖路径、GPU、请求数、QD、数据范围与缓存大小，原 CUDA kernel 不变；卸载失败返回失败 |

没有移入旧适配版的 GPU fence、volatile CQ 读取、BAR 映射/CQE 发布重写、PRP 修复或算法限制。
因此这些原作路径能否在新 GPU/驱动上正确运行仍需服务器验收；若失败，先保留日志，针对具体问题增加独立最小补丁，不以重写引擎代替兼容修复。
特别是作者原版 DMA 出错后可能回退 CPU；发生此类日志的性能轮不能计为 DSA 结果。

## 3. 目标机准备与构建

从本分支及其匹配的 BaM 提交启动全新构建。提交仍仅在本地，传到服务器前必须连同 BaM 提交一起同步。

```bash
git switch v2-baseline
git submodule update --init bam
uname -r
nvidia-smi
lscpu -e=CPU,NODE,SOCKET,CORE
lspci -tv
accel-config list -i
```

记录所有 DSA 的实际编号/BDF/NUMA 节点、GPU BDF/节点、服务 CPU、预留 RAM 实际分布。
作者代码按编号选取前 N 颗可发现 DSA，并由模块自己配置/启停 WQ；不使用旧分支的 prepare_dsa 或已借用 WQ 工作流。
目标设备应为空闲测试资源。若前两颗 DSA 不分别属于两个 CPU，则 `dsa_num_devs=2` 不等于双 CPU 双 DSA；不能把该结果标为双路，需先解决资源选择并记录差异。

候选启动参数为 `intel_iommu=off memmap=24G$4G`；仅在该物理范围确实可以预留时采用。
已验证机器布局优先；不要为套用样例覆盖正在使用的内存。

```bash
export KERNELDIR=/lib/modules/$(uname -r)/build
export IDXD_INCLUDE=/path/to/matching-linux-6.8/drivers/dma/idxd
bash scripts/build.sh -i configs/baseline-single-dsa.conf --clean

# 替换成该 GPU 的计算架构、驱动头目录和当前内核对应的 DKMS 符号表。
export CMAKE_ARGS='-DCMAKE_CUDA_ARCHITECTURES=120 -DNVIDIA=/path/to/nvidia -DNVIDIA_SYMVERS=/path/to/Module.symvers'
bash scripts/build_bam.sh --clean
```

上述命令沿用作者的 build/load/run 脚本。需要 GNU getopt、coreutils、bc、numactl、make、CMake、CUDA、匹配 Linux 6.8 的完整 IDXD 私有头及导出符号；公用 kernel headers 不一定包含 IDXD 私有头。
完整编译后检查 `modinfo swarmio.ko`、`bam/Release/module/libnvm.ko` 的 vermagic 与实际依赖。
IDXD 私有接口没有稳定 ABI，源码版本和目标发行版必须匹配；本地源码检查不能代替这一步。

## 4. 两个候选配置与手工运行

`configs/baseline-single-dsa.conf`：1 DSA、4 SU、每 SU 1 worker、候选 CPU8–15。
`configs/baseline-dual-cpu-dsa.conf`：2 DSA、8 SU、候选 CPU8–15/28–35。
两配置沿用作者 batch16、depth32、每设备4 group、SQE 合并1024、关闭 prefetch、开启真实 SQ/payload DMA。
保留原 QID 轮转和 CPU 编号分配，不能在基线内偷偷加入 NUMA 本地路由。
构建选项相同，切换资源配置只需正常卸载后重新加载。

```bash
mkdir -p results/baseline/manual
bash scripts/load.sh -i configs/baseline-single-dsa.conf
# BDF 必须核实为 SwarmIO 虚拟控制器。
bash scripts/load_bam.sh 0001:10:00.0
export BAM_GPU=0 BAM_CACHE_SIZE=512M BAM_DATA_SIZE=128M
export BAM_REQS=32 BAM_QUEUE_DEPTH=1024 BAM_RANDOM=true
bash scripts/run_bam-block-bench.sh -j 64 -q 8 -l 512 \
  > results/baseline/manual/single-smoke.log 2>&1
```

作者默认32GiB缓存/112GiB范围不适用于所有机器，故这里只改 shell 输入尺寸。
`BAM_CACHE_SIZE / 请求字节数` 必须至少等于线程数；缓存需加上 GPU SQ/CQ 和其它元数据后仍放得进显存。
`BAM_DATA_SIZE` 必须小于模拟存储容量，并留出至少最大请求长度的余量：作者随机起始 LBA 上限未为多 LBA 请求留足空间，本分支未修改该 CUDA 代码。
例如20GiB后端使用128MiB范围不会碰到 namespace 尾端，但这不是对原 benchmark 所有几何的正确性证明。

每轮保留原始日志和该轮内核日志。确认所有 dispatcher/worker 使用 DSA、队列建立完整、GPU正常退出，且无 timeout、DMA polling failed、disabling DMA、CPU fallback 或 Oops。
作者入口没有完整逐字节内容验证，本分支不冒充已经验证了数据正确性；如新环境出现异常，先解决正确性，再报最大性能。

正常退出后按作者流程卸载，再换双路配置：

```bash
bash scripts/load_bam.sh -u 0001:10:00.0
bash scripts/load.sh --unload
bash scripts/load.sh -i configs/baseline-dual-cpu-dsa.conf
bash scripts/load_bam.sh 0001:10:00.0
# 用完全相同的 GPU、地址范围和工作负载重复运行。
```

也可使用作者 `scripts/run_bam.sh` 的列表扫描方式。`VDEV_BDF`、`BAM_CPU_NODES`、`BAM_MEMORY_NODES` 可覆盖其中原先写死的机器布局。
该脚本退出时会清理模块，初期建议先用上面的分步命令观察现场。

## 5. 最大 IOPS / 吞吐搜索口径

先完成单路和双路小规模检查。之后固定每轮参数并手工扫描，不增加自动测试框架：

1. IOPS：512B 随机 READ，QP 32/64/128/256，线程4096/16384/65536/131072，QD先固定1024。
2. 吞吐：4KiB，然后16KiB/64KiB；仍使用原 benchmark/PRP 路径，先确认请求能正确完成，再扩大线程和 QP。同步增大 GPU 缓存以满足线程数据槽数量。
3. 每种几何先 reqs32 检查，正式 reqs256/1024/2048；比较加长轮次后的平台区间，防止启动开销或过短运行制造峰值。
4. 先固定作者模型40M目标。搜索执行能力上限时，再对单/双路使用相同的80/160/320M目标配对；通过重复提高模型容量判断是否仍受模型限速，不把提高模型容量当成实现优化。
5. batch/depth、SU和CPU数量分别作为独立参数轴。默认先保留作者 batch16/depth32；若要搜索 batch32/64/128，单/双路均搜索并完整记录，不能只调优其中一组。
6. 每点至少3轮；报告中位数和范围。先用候选扫描定位平台区，再增加线程/QP/QD或运行长度确认平台稳定，称为“已测参数范围内最大”，不宣称绝对硬件极限。

例如仅加长原作者 kernel 的请求循环：

```bash
export BAM_REQS=2048 BAM_CACHE_SIZE=512M BAM_DATA_SIZE=128M
bash scripts/run_bam-block-bench.sh -j 65536 -q 128 -l 512 \
  > results/baseline/manual/single-512b-q128-t65536.log 2>&1
bash scripts/run_bam-block-bench.sh -j 65536 -q 128 -l 4096 \
  > results/baseline/manual/single-4k-q128-t65536.log 2>&1
```

提高模型容量沿用 `scripts/load.sh --target-miops 80`，切换前先正常卸载 BaM/SwarmIO，不能在活动队列中重置控制器。
单路和双路各自寻找最大值，同时另外保存同参数配对结果；两种指标不要混用。

作者 BaM 输出 `Ops/sec` 和 `Effective Bandwidth(GB/S)`；后者实际除以1024³，应按 GiB/s 报告。
`read_data()` 含 `enqueue_second()` 命令链，因此逻辑 IOPS/有效带宽不能直接当作 NVMe 命令率或 DSA 实际搬运字节率。
本基线保留该协议，两组都按相同口径报告。若没有独立命令/硬件计数，不推算物理 NVMe IOPS。

结果表至少保存：SwarmIO/BaM提交、构建参数、CPU/GPU/DSA拓扑、RAM放置、SU、QP、QD、线程、reqs、I/O大小、地址范围、batch/depth、模型容量、时长、逻辑IOPS、GiB/s、错误及全部日志。
单GPU的原CUDA event计时用于此阶段；不套用成双GPU总吞吐。

## 6. 当前完成和未完成的验证

本地：`python3 -m unittest discover -s tests -v`，6项检查全部通过；两个仓库的 `git diff --check` 通过。检查作者核心代码/负载代码保持一致、C链接调整白名单、shell语法、构建参数和手工入口参数转发。
未执行 Linux 6.8 完整模块编译、NVIDIA驱动加载或真实性能测试，也没有预填IOPS/带宽结果。
后续优化从本基线增加拓扑归属策略，保持作者 BATCH、SQE 合并、模型和 benchmark 协议一致。
