# lab230-s6：单个 DSA 的 QP、线程、service unit 与 depth 扩展实验

本配置始终使用 **dsa0 + GPU0、NUMA0**。不启用 dsa2，不改 SSD 时延模型、
GPU 每线程同步请求协议或 CPU CQE 写回路径。

服务器已确认：4 group、4 engine、8 WQ、`max_work_queues_size=128`、
`max_batch_size=1024`；当前每 WQ size=16，偶数 WQ 获取 SQ，奇数 WQ 搬运数据。
`memmap=24G$4G` 沿用现有配置，无需为这些实验另改 GRUB。

## GPU pin 预算的含义与配置

`bam/module/map.c` 原来就有只读的 `module_param(max_gpu_pinned_mb, uint, 0444)`；
以前测试脚本加载 libnvm 时固定传入32，现在改为读取配置：

```json
"libnvm": {
  "max_gpu_pinned_mb": 256
}
```

也可在 doctor/build 时用 `--gpu-pinned-mb 256` 覆盖。旧配置未指定时仍用32。
预算保存在 manifest，load 传给 libnvm，run 核对实际模块值；加载后不能用 echo 修改。
调整预算不需要改 C 常量，但当前流程需在新构建目录生成相应记录并重新加载。

这个值是**允许被 P2P pin 的 GPU 内存总量上限**，不是预先分配的独立缓冲池。
当前包括 GPU SQ/CQ 和请求接收缓冲区；READ 数据方向为预留主机 RAM → GPU。
不包括 memmap 模拟存储区、主机 DSA 描述符、普通 CUDA CID/ticket/lock 数组。

设 `A(x)=向上对齐到64KiB(x)`，估算为：

```text
pin = A(pages × page_size)
    + num_queues × [A(queue_depth × 64) + A(queue_depth × 16)]
```

在 QD≤1024 时每 QP 的 SQ/CQ 至少占128KiB。`pages >= threads`。
run 在启动 GPU 进程之前拒绝超过预算的几何，并打印/记录 `buffer_budget`。
这些是当前单段512B/4KiB profile的映射预算，不是全部 CUDA 显存占用。
BaM 普通队列管理数组还约占 `QP × (65536 + 4×QD) × 32B`：
例如512 QP、QD1024约1.0625GiB，另有 CUDA 上下文、页面元数据等开销。
提高预算并不保证 GPU/驱动一定能成功分配或 pin 相应内存，实际错误仍会使本轮失败。

## 先运行 128 / 256 / 512 QP

`configs/bam-dsa-scale-lab230.json`：编译上限512 QP，pin预算256MiB，
4 service unit，CPU8–15，batch128/depth16，worker本地队列32768。
沿用当前8×16 WQ，无需重配DSA。
512 QP 的 QID512 doorbell 跨入第三个BAR页，BaM已有按请求QP数计算映射长度的路径；
本次增加队列编号、深度和BAR/内部区布局边界检查。
扩展基准仍限制QD≤1024，并检查控制器没有裁剪实际QP数和深度。
BaM CQ仍禁用每队列中断，不要求为512个CQ各增加一个MSI-X向量。

同步 SwarmIO 源码和 bam 目录中的改动，使用一个不存在的新目录：

```bash
python3 scripts/swarmio_bam.py --config configs/bam-dsa-scale-lab230.json \
  --build-dir build-dsa-scale-q512-su4-b128-d16 build --jobs 8 \
  --cuda-root /usr/local/cuda-13.2 \
  --idxd-include /home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd

# unload 必须指定当前已加载实例的目录；按实际目录替换。
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-sq-b128-d16 unload
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-scale-q512-su4-b128-d16 load
cat /sys/module/libnvm/parameters/max_gpu_pinned_mb
```

最后应输出256。只有卸载成功才继续加载；测试失败时沿用原 status/排空恢复流程。
新编译目录中的源文件副本不会被旧目录自动替换。

先验证所有512个QP都能参与的512B随机读：

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-scale-q512-su4-b128-d16 run \
  --threads 4096 --reqs 1 --pages 4096 \
  --num-queues 512 --queue-depth 1024 \
  --page-size 512 --num-blks 262144 --random --verify
```

本配置初始化128MiB校验区，覆盖262144×512B的地址范围。
期望内容PASS、payload/SQ条目均4096、payload字节2097152、SQ字节262144，错误和超时均0。
也可用相同线程/队列几何再测 `--page-size 4096 --num-blks 32768 --verify`。

随后各跑三次下面的性能组合。三轮都为67,108,864次512B READ、32GiB传输，
地址范围统一128MiB，监控参数保持一致：

| num-queues | threads | pages | reqs | QD | pin估算 |
|---:|---:|---:|---:|---:|---:|
|128|65536|65536|1024|1024|48MiB|
|256|131072|131072|512|1024|96MiB|
|512|262144|262144|256|1024|192MiB|

```bash
sudo python3 scripts/swarmio_bam.py --build-dir build-dsa-scale-q512-su4-b128-d16 run \
  --threads 65536 --reqs 1024 --pages 65536 \
  --num-queues 128 --queue-depth 1024 \
  --page-size 512 --num-blks 262144 --random --monitor-interval 0.1
```

另外两轮只替换表中四项。SQs/CQs打印的是协商上限，`n_qps` 才是本轮实际QP数量。
这张表同时扩大QP和线程，用于压力扩展；若单独研究QP影响，固定threads/pages/reqs，只改num-queues。
GPU线程是提交者数量，不保证同时驻留。每个线程只复用自己的数据槽，不按reqs预分配全部请求数据。

## 在同一个 DSA 上增加 service unit

新实现按 `group = service_unit_id % groups` 分配服务单元。
同组服务单元共享偶数fetch WQ、奇数payload WQ，**每个服务单元仍有独立的描述符、完成记录与回调数组**。
每个唯一DMAengine通道只借用一次，普通卸载仍只归还通道，不停用/重置设备。
QP依旧由单一dispatcher/worker拥有；这里共享的是DSA WQ，不是让多个CPU同时写同一个CQ。

每组实际所有者数记为U，必须同时满足：

```text
U × num_dma_descs_per_disp   <= fetch WQ实际size
U × num_dma_descs_per_worker <= payload WQ实际size
```

Python配置检查、doctor/load实际WQ检查、内核借用检查均校验总额度。
当前单DSA入口支持1..32个service unit；实际还受同NUMA CPU数量和描述符容量限制。

当前16/16 WQ配置可直接比较：

| 配置 | service unit | 每WQ最多所有者 | worker depth | fetch depth | CPU |
|---|---:|---:|---:|---:|---|
|bam-dsa-scale-lab230.json|4|1|16|2|8–15|
|bam-dsa-scale-su8-lab230.json|8|2|8|2|4–19|
|bam-dsa-scale-su16-lab230.json|16|4|4|2|0–15、40–55|

这三组payload描述符总额度均64个顶层BATCH，用于比较更多CPU服务线程是否有效。
SU16使用SMT兄弟线程，不等于增加16个物理CPU核；会使用CPU0等常用核，先按服务器占用调整cpus。
每service unit需要2个在线、同NUMA CPU；脚本核对数量、在线状态及NUMA。
内核不再允许加载后修改num_service_units。

分别使用对应配置、新build目录执行同一build流程并正常切换模块；不要把SU8配置与 `--dsa-depth 16` 混用，
那会要求每个payload WQ有32个描述符，当前size16不满足。

worker本地队列改为 `kvcalloc/kvfree`，不再因为大于单次kmalloc上限而裁剪。
`NR_WORKER_QUEUE_SIZE`仍限制2..65535（16位环索引），分配失败返回错误；
本配置32768槽/worker，约5MiB/worker（实际以构建结构大小为准）。
队列满时仍背压，不要求每个worker缓存全局所有GPU线程的请求。

## 增大 DSA depth：先满足硬件总预算

当前8×16已用完128个WQ槽，不能在保留四组fetch WQ时把四个payload WQ都改成32。
仅修改 `--dsa-depth 32` 或把 pin预算改大都无效。

先固定现有WQ和负载比较depth4/8/16。若确实受depth限制，可用：

* 每组fetch WQ size=2，payload WQ size=30；总量 `4×(2+30)=128`。
* 4 service unit、fetch depth2、payload depth30。
* 配置：`configs/bam-dsa-scale-d30-lab230.json`。

已扩展准备脚本，支持明确指定两种容量：

```bash
bash tests/dsa_gpu/prepare_dsa.sh dsa0 --check --fetch-size 2 --payload-size 30
```

当前设备enabled且容量不匹配时，它会拒绝自动重配，这是预期行为。
这条命令不会停用活动WQ，也不会修改硬件。
只有在SwarmIO等DSA客户端已正常卸载、设备及所有WQ均已由管理员正常停用且无驱动绑定后，才可执行：

```bash
sudo bash tests/dsa_gpu/prepare_dsa.sh dsa0 --apply --fetch-size 2 --payload-size 30
```

之后再以d30配置doctor/build/load；回到原16/16布局时，在同样停用条件下使用默认准备参数。
脚本沿用健康状态检查，不自动解绑、停用或复位设备。depth实验与QP实验分开，避免同时改变过多变量。
软件depth上限64；在本机保留4个活跃group、每组两种WQ的布局下，不能把4个worker都设到64。

## 结果与验证范围

每轮核对payload、SQ fetch、BATCH提交/回收分别平衡，错误/超时为0；
性能轮次没有逐字节校验，内容校验需单独reqs=1运行。
峰值批次数是模块加载以来的软件未回收槽位峰值，不是硬件同时执行深度。
比较稳定阶段和多轮中位数，避免把线程陆续退出时的尾部峰值当成持续吞吐。

本地为macOS；回归检查使用真实C控制流及接口替身，不等价于Linux6.8内核构建或真实GPU/DSA验证。
