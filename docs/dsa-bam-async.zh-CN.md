# lab230-s6：SwarmIO + BaM 异步 DSA BATCH

环境继续使用 Linux 6.8.0-138、NVIDIA 595.99.02、CUDA 13.2，
`intel_iommu=off memmap=24G$4G`，GPU0/dsa0/CPU8–15。
设备准备、驱动输入路径和正常装卸要求见 [同步基线说明](dsa-bam-lab230.zh-CN.md)。
这次仅修改 SwarmIO 主仓库，BaM 子模块继续使用已适配的版本。

## 执行路径和参数

使用 `configs/bam-dsa-async-lab230.json`。仍由 CPU 获取 SQE 和写回 NVMe CQE；
4 个 worker 各独占一个 payload WQ。数据搬运使用硬件 `DSA_OPCODE_BATCH`：

* `dma_batch_size=32`：每批最多32条 MEMMOVE，支持2..1024，受实际 WQ 的 max_batch_size 约束。
* `num_dma_descs_per_worker=16`：每 worker 最多16个批次槽位，不能超过 WQ 容量。
* `dma_batch_flush_us=5`：从尾批第一条请求开始计时；worker 轮询时到期即提交，不无限等待凑满。
  它是轮询检查阈值，并非硬实时的5微秒上界。单请求尾批补一条 NOOP，NOOP不计操作数和字节数。
* 满批自动提交。槽位全部占用时返回背压，保留当前 NVMe 请求，下轮重试；热路径不逐批阻塞等待。
* worker 每轮扫描完成状态，验证顶层及子描述符状态，只有确认 DMA 成功的请求才能发布成功 CQE。
* 保留 SSD 时延模型：read_sched_delay=400ns、read_min_delay=49600ns；与同步配置一致。
* 此版本仅测试单物理页内的512B/4KiB请求，每线程独占缓冲页；pages必须不小于threads。
  更大的跨页请求会在入批前报错，不会部分提交后重试。

失败不回退 CPU。硬件BATCH错误先执行DRAIN，未排空前不释放槽位/缓冲区；
错误CQE要等所有worker的DMA提交临界区和在途DMA排空，避免GPU退出时仍有DMA写入。
超时仅锁存故障，不提前释放资源；DRAIN失败时保留资源，需要维护恢复，不承诺在线卸载。

## 构建和加载

更新主仓库源码，使用一个**全新的构建目录**。已有build-dsa-68的源码副本不会随源码更新。
下列CUDA/IDXD路径使用此前成功的路径；如果安装位置不同，请替换。

```bash
cd ~/SwarmIO
python3 scripts/swarmio_bam.py --config configs/bam-dsa-async-lab230.json \
  --build-dir build-dsa-async-b32-d16 build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd \
  --dsa-batch-size 32 --dsa-depth 16 --dsa-flush-us 5
```

build只编译，不改变当前模块。构建成功且旧负载正常退出后，用旧模块原来的目录正常卸载：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-68 unload
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-async-b32-d16 load
cat /sys/module/swarmio/parameters/dsa_state
```

旧模块若使用其它构建目录，将unload的目录换成对应目录。
不能与dsa_gpu_test同时占用这些WQ。正常卸载只归还通道，不重置DSA/WQ。
加载后应看到 `version=2 mode=1 borrowed=4 fault=0 active_batches=0 pending_batches=0`。
如果加载或卸载失败，先保存status，不重复加载或强制卸载。

## 先校验，再测性能

首先检验512B和4KiB，reqs必须为1，以验证每条请求的全部字节：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-async-b32-d16 run \
  --threads 64 --reqs 1 --pages 4096 --num-queues 1 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --verify

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-async-b32-d16 run \
  --threads 64 --reqs 1 --pages 4096 --num-queues 1 --queue-depth 512 \
  --page-size 4096 --num-blks 16384 --random --verify
```

必须看到Verification: PASS、64次真实DMA完成、errors/timeouts为0，以及提交/回收批次数相同。
尾批可小于配置batch；1024批次上限不意味着一定能凑满1024。

随后用与同步基线相同的负载参数做对照，示例百万次512B随机读：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-async-b32-d16 run \
  --threads 4096 --reqs 256 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --monitor-interval 0.1
```

总请求1048576，总字节536870912；64MiB随机访问范围。
4KiB对照将page-size改成4096、num-blks改成16384，其它相同。
较长测试可以reqs=1024；与同步模式比较时同样设置请求总数、线程和队列数，并重复多轮。
不要把pages=8192、page-size=4096直接用于当前32MiB pin预算：仅数据已达32MiB，还需GPU SQ/CQ空间。

## 如何确认真的用了批量流水线

`run.json`包含：

* `payload_backend=dsa-borrowed-batch`、`sq_backend=cpu`；GPU发起引擎仍显示Engine: bam。
* `dsa_submission`：构建时选择的batch/depth/flush参数。
* `dsa_delta`：本轮真实MEMMOVE submitted/completed/bytes/errors/timeouts，不包含NOOP或DRAIN。
* `dsa_batches.batches_submitted/batches_retired`：本轮顶层BATCH提交和安全回收次数。
* `average_batch_size`：本轮真实MEMMOVE数除以顶层BATCH提交数。
* `peak_batches_since_load`：模块加载以来全局已提交但未回收BATCH数量的峰值，**不是本轮峰值**。
* `max_batch_since_load`：加载以来实际提交的最大批次大小，不包含NOOP。

例如请求1048576次、平均batch=32，对应32768次顶层BATCH提交；
如果平均batch接近1，说明大多为尾批，不能用配置batch推断实际摊薄效果。
peak大于1说明存在重叠的未回收批次；它是软件槽位统计，不是DSA硬件内部执行深度的直接测量。
每轮结束要求active_batches/pending_batches为0，提交和回收相等，否则本轮失败。

## 调整批次大小

batch/depth/flush在加载后只读。使用build参数写入新构建目录及manifest，例如：

```bash
python3 scripts/swarmio_bam.py --config configs/bam-dsa-async-lab230.json \
  --build-dir build-dsa-async-b128-d16 build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd \
  --dsa-batch-size 128 --dsa-depth 16 --dsa-flush-us 5
```

正常卸载当前目录、加载新目录后再跑同一负载。建议先比较32/64/128，再尝试256/512/1024，
必要时在独立配置中比较flush=5/20us，观察实际平均batch和尾批延迟。
4worker × batch1024 × depth16能覆盖65536条逻辑搬运；
4096个GPU线程每线程一条请求时，并不能填满这些槽位。不要把独立dsa-gpu-bench的depth/batch容量
等同于BaM运行时可提供的在途请求量。

本版没有把CPU SQ读取、逐请求CQ写回改为DSA，也没有取消SSD时延模型；
实现异步BATCH不等于端到端NVMe IOPS一定达到独立搬运测试的13M。
本地macOS仅执行接口替身、真实C控制流/UBSan及回归测试；真实6.8内核编译和DSA/GPU性能需服务器验证。
