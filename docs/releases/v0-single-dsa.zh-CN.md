# V0：单 DSA 发布基线

## 源码与证据

| 项目 | 固定值 |
|---|---|
| 分支 | `release/v0-single-dsa` |
| 固定标签 | `research-v0.0.0`（SwarmIO与BaM各自保存） |
| SwarmIO提交 | `2d9baa8ccee73868a14bc4c20fe044a5a6ff8e56` |
| BaM提交 | `bd9141524f23a179e8b1719cabcefb42e500a7bf` |
| 配置 | `configs/bam-dsa-scale-lab230.json` |
| SQ后端 | `dsa-coalesced` |
| Payload后端 | `dsa-borrowed-batch` |
| 目标环境 | lab230-s6，GPU0、dsa0、NUMA0、CPU8–15 |

配置原文件默认 128 QP、65536 线程；下面的性能命令显式覆盖到 256 QP、131072 线程，以对应归档参考运行，不能把配置默认值写成历史测试值。

两次参考运行是历史已有实验，不是本次新测量：

| 运行ID | kernel平均IOPS | 总512B READ数 |
|---|---:|---:|
| `20261106-155117-147482` | 12,209,344.365652 | 134,217,728 |
| `20261106-160417-117097` | 12,200,776.515740 | 134,217,728 |

两轮都为 GPU0、256 QP、QD1024、131072线程、131072缓冲页、每线程1024次读取，地址范围128MiB，batch128/depth16/flush5us。内容正确性需另外执行校验轮；性能轮完成数正确不等于逐字节校验。

对应原始 `run.json` 和 `monitor-summary.json` 副本保存在 `experiments/baseline-v0/reference/`，摘要及校验值见该目录上一级 `manifest.json`。两轮历史构建 manifest 的 SHA256 相同，但原构建 manifest 未包含在这个小型归档中；需要完整历史构建环境时应另外归档服务器 build manifest。当前源码锚点不伪装成已经核实的历史构建源码提交。

## 独立检出和构建

在独立源码目录检出发布分支或上述确切提交，再初始化固定的子模块；不要使用 `submodule update --remote`。该分支本身包含原有构建入口、配置和测试代码，无需最新开发分支参与。

以下在目标 Linux 环境执行，CUDA及IDXD路径沿用已有实验配置；若路径变化，以对应工具链实际安装位置为准。使用不存在的新 build 目录。

```bash
git switch release/v0-single-dsa
git submodule update --init --recursive

python3 scripts/swarmio_bam.py \
  --config configs/bam-dsa-scale-lab230.json \
  --build-dir build-v0-single-dsa-r1 \
  build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd
```

切换前先按当前已加载版本的原 build 目录正常卸载，再加载新构建。两个版本不得同时占用同一组 WQ。

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-v0-single-dsa-r1 load

# 使用全部256个QP的有限内容校验，总计4096次请求。
sudo python3 scripts/swarmio_bam.py --build-dir build-v0-single-dsa-r1 run \
  --threads 4096 --pages 4096 --reqs 1 --num-queues 256 --queue-depth 1024 \
  --page-size 512 --num-blks 262144 --random --verify

# 参考性能几何：134217728次READ，64GiB有效载荷。
sudo python3 scripts/swarmio_bam.py --build-dir build-v0-single-dsa-r1 run \
  --threads 131072 --pages 131072 --reqs 1024 --num-queues 256 --queue-depth 1024 \
  --page-size 512 --num-blks 262144 --random --monitor-interval 0.1

sudo python3 scripts/swarmio_bam.py --build-dir build-v0-single-dsa-r1 unload
```

只有内容PASS、提交完成和字节匹配、无错误/超时且DMA排空的轮次才进入性能分析。本次没有执行以上服务器命令；历史证据不替代重建后的校验。

## 后续比较

- V1先用同一单路几何复现基线，再测双路；不把线程数、QP和模型容量同时增加后的提升都归因于新机制。
- V2在相同资源下比较同步/异步，再额外测计算重叠。
- V3保持FTL off作为旧模型对照，FTL on单独报告几何、写预处理与模型参数。
- 原始结果及其哈希永久保留，不用新测量覆盖历史记录。
