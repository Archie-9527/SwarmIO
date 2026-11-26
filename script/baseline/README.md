# Author Baseline

此目录集中维护当前论文基线采集流程。矩阵、BAR1 预算、测量口径与结果字段见
[精简版 Baseline 文档](../../docs/paper-baseline-compact.zh-CN.md)。

在 SwarmIO 项目目录执行，脚本不切换目录：

```bash
# 只生成计划，不操作 GPU 或内核模块
bash script/baseline/run_paper_baseline.sh --out results/Baseline-author-plan

# 检查容量，再验证完整采集与恢复流程
sudo bash script/baseline/check_bar1.sh results/bar1-check.log
sudo bash script/baseline/run_paper_baseline.sh \
  --quick --run --out results/Baseline-author-organized-smoke

# 冒烟成功后采集正式数据，每点三次
sudo bash script/baseline/run_paper_baseline.sh \
  --run --out results/Baseline-author-organized

# 重新汇总已有结果，不操作设备
python3 script/baseline/paper_baseline.py \
  --aggregate-only --out results/Baseline-author-organized
```

开始前应加载正常的 SwarmIO 双 DSA 原模块，并将 libnvm 绑定到虚拟 NVMe。
BaM 需要包含故障退出接口，例如服务器使用的 `2a08f65`；本目录不会切换子模块版本。
构建产物位于 `results/baseline/`，实验模块只在结果目录内构建，不安装或覆盖原模块。
目录迁移改变了源码指纹，请使用新的结果目录开始采集；同一版本、同一配置可用相同命令续测。

| 文件 | 职责 |
|---|---|
| `run_paper_baseline.sh` / `paper_baseline.py` | 入口、测试矩阵、运行检查、汇总、归档和原配置恢复 |
| `benchmark.py` / `benchmark_checks.cuh` | 作者 BaM 测试程序的隔离构建、队列校验、故障轮询及结果解析 |
| `baseline_modules.py` / `module_edits.py` | 原始、DSA2-only、连续均分及加权模块的隔离源码准备和构建 |
| `baseline_latency.c` / `baseline_latency.h` | SQ 同步搬运、worker 等待与回收的采样 |
| `common.py` | 硬件检查、BAR1 预算和共用常量 |
| `check_bar1.sh` | BAR1 与 Resizable BAR 只读检查 |

只执行作者原始 GPU workload；不再提供 `dsa-read-v1` 等实验 kernel。
通用项目脚本仍在 `scripts/`，本流程调用其 `load.sh`、`load_bam.sh` 完成排空、卸载和绑定。

离线检查不访问服务器硬件：

```bash
python3 -m unittest discover -s tests -q
```
