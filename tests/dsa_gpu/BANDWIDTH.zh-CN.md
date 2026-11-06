# 用单个 DSA 的搬运包络评估 SwarmIO 优化空间（ABI4）

## 测量目标与新增路径

原 `dsa-gpu-bench` 是 SQ→payload 流水线基准。GPU初始化命令、内存固定、主机分配、最终校验
位于计时外；SQ DMA、CPU校验SQ、构造payload子描述符、提交和完成轮询位于计时内。
因此原13M IOPS代表特定传输流水线的可达吞吐，不是纯payload带宽或单DSA理论上限。

ABI4新增 `--path`，默认仍是pipeline：

| 路径 | GPU SQ获取 | CPU逐请求构造payload子描述符 | 数据搬运/完成检查 |
|---|---|---|---|
|pipeline|有，GPU→CPU|有|DSA|
|payload|无；CPU按相同地址规则生成搬运工作|有|DSA|
|replay|无|计时前预构造，计时中复用|DSA|

replay为整个工作集建立子描述符bank，按环遍历全部bank，每组有独立数据区。
bank数不小于depth；按提交顺序回收，避免一个bank还在执行时被再次使用。
不是只反复复制depth个固定数据窗口，也不让计时外准备悄悄缩小实际工作集。
每块约需96B主机DMA元数据（64B描述符＋32B完成记录），512B、128MiB数据区对应约24MiB元数据。
这些元数据也会带来缓存和DMA访问，故不同路径差值不是可直接相加的单阶段耗时。

replay仍包含顶层BATCH准备/提交、完成记录清零、父子完成检查、计数和必要的CPU轮询。
它测的是本软件路径与当前WQ配置下的可达搬运带宽，不能宣称完全剥离软件的硬件理论峰值。
每个成功BATCH按包含的真实payload操作数计数；元数据传输不计入payload_GBps。

payload/replay支持512B..64KiB的2次幂尺寸；pipeline仍仅512B/4KiB。
大块路径按max(4KiB,块大小)独立分配连续主机DMA块；GPU块不跨64KiB映射页，
使用每页真实DMA地址，不假设相邻页物理连续。跨块访问仍是带seed的置换遍历，不是IID随机访问。

## 编译与切换

先正常卸载当前SwarmIO实例，确认没有负载在使用dsa0。两个测试不能并用同一组WQ。
沿用8个WQ各size16、4group/4engine的原布局；若已经改为2/30，先按原准备流程恢复。

```bash
cd ~/SwarmIO/tests/dsa_gpu
make module \
  IDXD_INCLUDE=/home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd \
  NVIDIA_INCLUDE=/usr/src/nvidia-595.99.02/nvidia \
  NVIDIA_SYMVERS=/var/lib/dkms/nvidia/595.99.02/$(uname -r)/x86_64/module/Module.symvers
make user NVCC=/usr/local/cuda-13.2/bin/nvcc
make check
```

若旧dsa_gpu_test仍正常加载，先普通rmmod后再加载新版；不要对going或仍有DMA的模块强制卸载。
新内核模块为 `v4.0-payload-replay`，与用户程序均为ABI4，必须同步更新两端。
旧ABI会在启动传输前被拒绝。

```bash
sudo insmod ./dsa_gpu_test.ko exclusive=1 device_id=0 groups=4 \
  cpus=8,9,10,11,12,13,14,15 max_pinned_mb=256
sudo ./dsa-gpu-bench --info
```

这里的max_pinned_mb与libnvm的max_gpu_pinned_mb是两个不同模块的参数。
replay/payload只固定data，不再分配或固定GPU SQ。软件预算不代表BAR1可用空间；
实际pin失败仍应停止，不把它当成性能数据。
raw路径仅使用奇数payload WQ和CPU9/11/13/15；偶数WQ无DMA，但仍被模块独占借用。
退出和清理继续等待已提交DMA及DRAIN，不自动停用、解绑或重置DSA/GPU。

## 先做有限请求的内容检查

```bash
sudo ./dsa-gpu-bench --path payload --size 512 --mode batch --direction read \
  --batch 32 --depth 4 --mb 8 --ops 4097
sudo ./dsa-gpu-bench --path replay --size 512 --mode batch --direction read \
  --batch 32 --depth 4 --mb 8 --ops 4096
sudo ./dsa-gpu-bench --path replay --size 65536 --mode batch --direction read \
  --batch 8 --depth 4 --mb 16 --ops 256
sudo ./dsa-gpu-bench --path replay --size 65536 --mode batch --direction write \
  --batch 8 --depth 4 --mb 16 --ops 256
```

要求host_errors=0、gpu_errors=0、PASS。raw路径SQE_IOPS必须为0。
replay只支持batch；ops须为groups×batch的整数倍，否则不够一批的尾部需要重建描述符，
会破坏replay定义。payload保留任意尾批支持。
固定数据模式的最终校验可检查搬运内容，但不能证明每次重复操作写入了不同的新值。

## 三层测量

所有性能轮次先预热1秒，测量10～20秒，每点重复3次取中位数。
保持GPU0、dsa0、CPU和工作集一致；如安装numactl，建议在命令前使用
`sudo numactl --cpunodebind=0 --membind=0`，约束准备线程/主机分配的NUMA位置。

第一层：512B，分别运行三种path，其他参数相同。

```bash
sudo ./dsa-gpu-bench --path pipeline --size 512 --mode batch --direction read \
  --batch 128 --depth 16 --mb 64 --seconds 15 --warmup-ms 1000
sudo ./dsa-gpu-bench --path payload --size 512 --mode batch --direction read \
  --batch 128 --depth 16 --mb 64 --seconds 15 --warmup-ms 1000
sudo ./dsa-gpu-bench --path replay --size 512 --mode batch --direction read \
  --batch 128 --depth 16 --mb 64 --seconds 15 --warmup-ms 1000
```

pipeline→payload反映移除SQ阶段的综合影响；payload→replay反映预构造的综合影响。
CPU任务数和元数据工作集也有所变化，不能把差值直接归因于某一条CPU指令。
另外比较batch128/256/512/1024，确认小块吞吐是否已被更大的批量充分摊销。

第二层：replay的大块带宽。每次MEMMOVE尺寸增大，减少每字节承担的描述符开销。

```bash
sudo ./dsa-gpu-bench --path replay --size 65536 --mode batch --direction read \
  --batch 8 --depth 16 --mb 64 --seconds 15 --warmup-ms 1000
sudo ./dsa-gpu-bench --path replay --size 65536 --mode batch --direction write \
  --batch 8 --depth 16 --mb 64 --seconds 15 --warmup-ms 1000
```

read是CPU→GPU，与SwarmIO READ payload同向；write是GPU→CPU，与SQ获取同向。
mixed按数据块固定50%双向，输出是两个方向payload之和；它不等于SwarmIO的512B数据＋64B SQ比例。
应同时观察4KiB、16KiB、64KiB是否进入相近的带宽平台，不能只把一个参数点称为带宽上限。

第三层：工作集16/64/128MiB，检查CPU缓存、DRAM和映射规模对上限的影响。
比较工作集时保持batch/depth/方向不变，并保证 `groups×batch×depth×size <= data_bytes`。
初始化、GPU固定和校验不计时，开始前的CPU初始化可能使主机数据进入缓存；
较小工作集的高带宽不能直接替代较大工作集下的DRAM搬运上限。

固定工作集扫描脚本（约需数十分钟，任一失败立即停止）：

```bash
sudo env DG_MB=64 DG_SECONDS=10 DG_REPEATS=3 bash run_bandwidth.sh | tee bandwidth-read.log
sudo env DG_MB=64 DG_DIRECTION=write DG_SECONDS=10 DG_REPEATS=3 bash run_bandwidth.sh | tee bandwidth-write.log
```

可用DG_SIZES、DG_BATCHES、DG_DEPTHS缩小范围，例如：

```bash
sudo env DG_SIZES='4096 16384 65536' DG_BATCHES='8 32' DG_DEPTHS='8 16' \
  DG_MB=64 DG_REPEATS=3 bash run_bandwidth.sh
```

脚本固定工作集，跳过容量不够覆盖在途请求的组合，不自动缩小工作集或提升pin预算。
`--seconds`是有效测量窗口，不是含分配/清理的整轮墙钟时间。整个batch按CPU观察到完成的时间归入窗口，
存在窗口边界量化误差，长窗口和重复测量可降低其影响。

## 如何评估 SwarmIO 的剩余空间

目前SwarmIO约12.209M×512B = 6.251GB/s payload；另有约0.781GB/s反向SQ，
以及CPU发布CQE、doorbell和协议开销。不同方向不能简单相加后与单向带宽比较。

应分开记录：大块replay带宽、小块replay IOPS、pipeline IOPS、SwarmIO IOPS。
例如“大块20GB/s、小块replay 6.5GB/s、SwarmIO 6.25GB/s”只是假设示例：
它说明瓶颈主要在小块处理效率，而非尚有20/6.25倍可直接获取的优化空间。
只有在允许数据合并、相邻请求确实连续且语义允许时，大块带宽余量才能转化为小块IOPS收益。

反之，如果512B replay显著高于SwarmIO，则控制路径、运行时描述符构造、CPU CQE发布和调度仍可能有空间；
再结合pipeline/payload差异及perf定位，不能仅凭一个带宽数归因。
这些是可达性能包络，硬件、块大小、地址分布、工作集、batch、depth和完成策略都要随结果记录。

本地仅完成真实C控制流/复制的接口替身测试、UBSan和语法检查；真实内核构建、GPU/DSA数据校验及带宽测量需在服务器执行。
