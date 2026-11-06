# lab230-s6：DSA 合并获取 SQE 对照测试

现有CPU-SQ配置已经启用 `CONFIG_SWARMIO_DISP_COALESCE_SQE=y`：
按doorbell中已就绪的连续SQE合并取回，跨环尾拆分，最多受 `NR_MAX_FETCH_SQE=1024` 限制。
CPU helper又按4KiB页边界调用memcpy_fromio；合并并未消除CPU读取GPU BAR的开销。
实际合并量取决于就绪数量和队列深度，不是每次固定32条。payload batch=32是另一个参数。

本配置复用dispatcher原有的DSA合并路径：每次用一条MEMMOVE获取 `num_fetch * 64` 字节，
等待这批命令到达主机命令缓冲区后，继续原来的检查、调度和分派。
这是**合并DMA读取多个SQE**，不对每条SQE构造一个硬件BATCH子描述符；本轮也不开启SQ预取。

* `configs/bam-dsa-sq-lab230.json`：DMA_DISPATCHER=y、COALESCE_SQE=y、PREFETCH_SQE=n、disp_using_dma=1。
* dsa0的偶数WQ0/2/4/6用于SQ获取，每dispatcher分配2个描述符；每批取回后再解析。
* 奇数WQ1/3/5/7继续payload异步BATCH，batch=32、depth=16、flush=5us。
* CPU8–15、GPU0、memmap、GPU基准、CQ写回、中断扫描、SSD时延模型均沿用CPU-SQ对照配置。
* SQ DMA失败停止分派并锁存故障，不回退CPU，不推进失败批次的fetched_pos。
* 新增SQ DMA计数与payload计数分离；正常结束时两类在途资源均已排空。

环境和IDXD/NVIDIA路径沿用 [原DSA说明](dsa-bam-lab230.zh-CN.md)。
本轮没有重写payload批量引擎；主要是启用已有路径、识别SQ上下文、计数和WQ检查。

## 构建与加载

同步主仓库修改及新配置，使用新构建目录；本轮无BaM子模块修改。
路径按服务器此前成功的安装位置替换。

```bash
cd ~/SwarmIO
python3 scripts/swarmio_bam.py --config configs/bam-dsa-sq-lab230.json \
  --build-dir build-dsa-sq-b32-d16 build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-async-b32-d16 unload
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-sq-b32-d16 load
cat /sys/module/swarmio/parameters/dsa_state
```

unload必须使用当前加载模块的原构建目录。加载应看到：
`version=3 borrowed=8 mode=1 sq_fetch=1 fault=0`，以及active_batches/pending_batches/sq_pending均为0。
所有8个WQ须已启用、dedicated/kernel且绑定dmaengine；只借用，不重配设备。

## 内容校验与性能对照

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-sq-b32-d16 run \
  --threads 64 --reqs 1 --pages 4096 --num-queues 1 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --verify

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-sq-b32-d16 run \
  --threads 64 --reqs 1 --pages 4096 --num-queues 1 --queue-depth 512 \
  --page-size 4096 --num-blks 16384 --random --verify

sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-sq-b32-d16 run \
  --threads 4096 --reqs 256 --pages 4096 --num-queues 16 --queue-depth 512 \
  --page-size 512 --num-blks 131072 --random --monitor-interval 0.1
```

两次校验都须PASS。每轮64次请求对应64条SQE/4096字节SQ DMA；
payload分别64次/32768字节和64次/262144字节。
性能轮次仍为1048576条READ：payload完成1048576次/536870912字节，SQ获取1048576条/67108864字节。
SQ DMA提交次数取决于实际合并情况，只要求提交与完成相等，不要求等于READ数。
脚本分别核对，SQ获取不会被误计为额外payload READ。

`run.json`增加 `sq_backend=dsa-coalesced` 和 `dsa_sq_delta`：

* sq_submitted/sq_completed：本轮合并SQ DMA次数。
* sq_entries/sq_bytes：本轮实际取回的SQE数量及字节数。
* average_sqes_per_dma：本轮SQE数量 / SQ DMA完成次数。
* max_sqes_since_load：模块加载以来单次SQ DMA包含的最大SQE数，不是本轮峰值。

若平均SQE/DMA接近1，说明就绪命令较分散，不能按1024条推断合并收益。
对照固定threads/reqs/queue参数及监控设置，建议各重复3轮。
如需复查perf，用reqs=4096延长负载，并沿用之前的CPU8–15采样方法；
原 `memcpy_fromio → sq_diag_copy → swarmio_dispatch_sq` 热点应从运行期SQ获取路径消失。
后续若瓶颈转移，依据新采样再决定是否优化SQ预取、CQ或worker空轮询，不在这一轮混合改动。

本地为macOS，已执行真实C控制流/接口替身及回归检查；真实内核构建、GPU内容校验和吞吐仍需服务器验证。
