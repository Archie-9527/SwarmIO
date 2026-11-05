# 单DSA多group GPU搬运测试：共享服务器模式 v3.1

本版针对4group/4engine/8WQ、WQ总容量128、最大BATCH1024的DSA，仍测试GPU SQE→CPU解析→CPU/GPU payload搬运，payload为512B/4096B。CUDA端与内核使用ABI3，必须同步更新源码并重新编译两端。不能把新版测试直接加载到已有Oops残留的内核中验证。

## 0. v3.1修复GPU映射失败后的错误清理

本次日志先报告`no space for BAR1 mappings, length: 0x10000000`，256MiB数据区固定失败并返回`-ENOMEM`，随后`unpin → nvidia_p2p_put_pages_persistent → RmP2PPutPagesPersistent`空指针异常。此时尚未创建DSA工作线程。主线程`Z`和其它线程`D`说明发生异常后的退出/清理受阻，不是正常的10秒性能测试还在执行。

旧代码只检查页面表指针非NULL，没有确认对应get操作成功；失败接口留下的输出指针不能作为有效资源释放。这是测试模块的错误路径缺陷。v3.1用独立所有权状态修复：

* get/map使用局部输出指针，仅在返回成功后保存到资源对象；失败输出不保存、不解引用、不调用对应释放API。
* get成功、map失败时只释放已成功固定的页面；data成功、SQ失败时分别按实际所有权逆序回收。
* unmap失败时停止对同一区域执行put；清理API失败则隔离已拥有的句柄、保留模块/设备引用、拒绝后续测试，不强行释放或自动复位。该保留状态需要维护处理，不能通过普通卸载强行清除。
* 默认data工作集降为8MiB；新增`max_pinned_mb=32`，限制data+SQE的总映射请求。用户态在CUDA初始化之前检查，内核也独立检查并返回`-E2BIG`。这是软件预算，不能保证BAR1可用空间，也不会自动调大BAR1。
* GPU映射前移到大批主机内存分配之前，并输出pin_data/pin_sq/host_alloc/start_workers等阶段日志。

已发生的Oops不能通过替换二进制修复。当前保留故障进程/模块，暂停测试，不反复kill、卸载、解绑或复位。可以把恢复安排到管理员认可的维护窗口；本代码没有可靠的在线恢复方法，也不能保证崩溃后的内核和NVIDIA状态完整。

## 1. 当前故障能否不重启

服务器已报告旧模块的以下状态：

```text
/proc/modules: dsa_gpu_test ... Unloading ... (OE-)
/sys/module/dsa_gpu_test/initstate: going
rmmod → test_exit → dsa_disable_devices → device_release_driver
 → idxd_device_drv_remove → idxd_device_evl_free → mutex_lock(NULL)
```

可以暂时停止DSA实验，保留故障模块/设备到维护窗口，不必为了安装新版测试程序立刻重启。其它业务是否可继续应由管理员结合新的Oops、hung task和业务状态评估；不能保证发生过Oops的内核仍完整可靠。

但本测试没有可验证安全的在线恢复办法来让已中断卸载的旧模块回到live状态。新模块加载被同名going对象拦截；livepatch可以影响后续函数调用，但不能自动续接已经崩溃的rmmod或重建丢失的清理状态。不应改模块名绕过、改引用计数、手动清模块链表、强制rmmod、解绑或复位受影响设备。

`bash diagnose.sh`只读检查，发现going会停止，不继续读取DSA设备sysfs，避免等待异常卸载可能遗留的锁。`prepare_dsa.sh`及新内核模块也会在内核存在Oops的D标志时拒绝启动新的实验；它们不自动重启、不自动恢复异常设备。

## 2. v3防御措施与边界

旧版复制SwarmIO的设备接管逻辑，会在卸载时解绑整颗DSA，进入IDXD的EVL清理。本版删除了该路径：

* 管理员在健康、空闲的设备上一次性配置标准DMAengine WQ。
* 模块只读取配置，并通过DMAengine的独占通道申请API借用指定WQ；通道忙或配置不匹配时返回错误，不接管、不停用其它客户端。
* 描述符仍直接使用idxd的MEMMOVE/BATCH接口，保留1024项批量发布；DMAengine用于通道所有权和引用管理，不替换数据热路径。
* 模块卸载及加载失败回滚时，只归还成功借到的通道并放下设备引用。WQ、engine、group和父DSA驱动绑定均保留；不调用device_release_driver、device_attach、idxd_drv_enable_wq、idxd_drv_disable_wq、设备reset或disable。
* 只在所有DMA已完成或成功DRAIN后回收描述符、GPU映射和主机内存。收到信号、提交错误或5秒DMA超时后停止新增任务；仍在途的资源继续保留，清理等待改为睡眠轮询，避免占满CPU。
* DMA永久无响应时ioctl仍可能无法结束，设备通道和模块引用保持占用。这样选择保留资源而不是强制回收，不实现在线共享设备复位；正常rmmod会因仍被使用而拒绝。
* 初始化和新ioctl遇到内核Oops标志时拒绝启动新的传输。普通NVIDIA/外部模块产生的O/E/P taint不在这个拒绝条件内。

这些措施移除了本测试已知的整设备解绑触发路径，不等价于修复Linux/NVIDIA底层驱动，也不能承诺任何硬件/内核异常均不会导致Oops。运行过程中管理员仍不得强制解绑或重配正在使用的WQ。

## 3. 默认资源与流水线

预配置4组，每组1个engine和2条dedicated kernel WQ；8条WQ各分配16项，总容量128。WQ名称及driver_name均为dmaengine，最大batch1024，max_transfer使用硬件报告值。

| group | engine | SQE WQ | payload WQ |
|---|---|---|---|
| 0 | 0 | wq0.0 | wq0.1 |
| 1 | 1 | wq0.2 | wq0.3 |
| 2 | 2 | wq0.4 | wq0.5 |
| 3 | 3 | wq0.6 | wq0.7 |

表中设备假定dsa0。模块参数device_id明确选择设备，不自动改用其它设备。exclusive=1表示调用者确认已获准测试；实际通过DMAengine通道申请实现队列级独占，不是锁住整个PCI设备。

每组有独立fetch/worker线程，环形slot执行FREE→FETCHING→READY→COPYING→FREE，不再每128条整体等待。depth是每组slot数，两个阶段共享这些slot；默认depth8、最大16，每slot预留两个顶层描述符，分别来自两个WQ。默认最大参数4×16×1024提供65536条payload的软件在途预算，不能理解为65536条物理操作同时执行。

sync模式强制每组depth1，一条SQE和一条payload依次完成；4组就是4条同步链并行。groups=1/2/4只选择已配置前1/2/4组，不修改全设备配置；预配置4组时，即使只借1组，每WQ仍是16项。

## 4. 更新与编译

先备份旧目录并同步整套新文件。仍在going的旧模块不能通过更新文件修复，不要尝试强制在线卸载它。

```bash
cd ~/SwarmIO/tests/dsa_gpu
KDIR=/lib/modules/$(uname -r)/build
IDXD_INCLUDE=/home/zhaohq/kernel-src-dsa/linux-hwe-6.8-6.8.0/drivers/dma/idxd
NVIDIA_INCLUDE=/usr/src/nvidia-595.99.02/nvidia
NVIDIA_SYMVERS=/var/lib/dkms/nvidia/595.99.02/$(uname -r)/x86_64/module/Module.symvers
make clean KDIR="$KDIR"
make all KDIR="$KDIR" IDXD_INCLUDE="$IDXD_INCLUDE" \
  NVIDIA_INCLUDE="$NVIDIA_INCLUDE" NVIDIA_SYMVERS="$NVIDIA_SYMVERS"
make check
modinfo -F version ./dsa_gpu_test.ko
```

版本应为v3.1-pin-ownership，用户态及模块均为ABI3。backend.c不再复制旧SwarmIO设备配置实现，config.c保留为历史文件，不再链接。编译仍依赖../../dsa/dsa.h及匹配的IDXD内部头；目标内核需要导出idxd_alloc_desc/submit_desc/free_desc等原有符号，以及DMAengine通道API。CUDA12+、NVIDIA persistent P2P API和64KiB GPU页映射要求与原版相同，不支持MIG/vGPU。

## 5. 管理员一次性预配置

以下操作仅适用于未发生Oops、无going旧模块、确认空闲并获准测试的设备。不得在当前故障dsa0上直接运行--apply。

```bash
# 默认只检查，不修改设备
bash prepare_dsa.sh dsa0 --check

# 健康内核中、设备本来就disabled且所有WQ均disabled/unbound时，才允许配置
sudo bash prepare_dsa.sh dsa0 --apply
```

脚本不执行disable-device、disable-wq、卸载、解绑或reset。设备已enabled且配置匹配时不作修改；已enabled但配置不匹配时拒绝，留给管理员评估。配置过程中若某一步失败，停止并保留当前状态，不以整设备停用作自动回滚。

准备完成后保持DSA及WQ为enabled。**新版模块加载前不要再执行此前教程中的disable-wq/disable-device。**

```bash
sudo insmod dsa_gpu_test.ko exclusive=1 device_id=0 groups=4
sudo dmesg | tail -40
```

加载会验证前8条WQ已绑定标准dmaengine驱动、dedicated/kernel、每组2WQ、每组1engine、batch和transfer能力，并借到全部需要的通道。任一通道被占用会失败并归还此前已借到的通道，设备仍enabled。

可选cpus参数明确绑定8个不同在线CPU，顺序fetch0,worker0,fetch1,worker1,...。例如cpus=0,2,4,6,8,10,12,14只是示例，先用lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE选择靠近DSA的不同物理核。默认按DSA的NUMA节点分布到不同在线逻辑CPU。

## 6. 正确性与性能命令

以下仅用于恢复健康且已加载配套v3.1模块的测试环境。先进行小工作集正确性验证，再逐步增加batch和depth。

```bash
# 只读取模块信息，不初始化CUDA或固定GPU页面
sudo ./dsa-gpu-bench --info
sudo ./dsa-gpu-bench --size 512 --mode sync --direction read --ops 129 --mb 1
sudo ./dsa-gpu-bench --size 4096 --mode sync --direction write --ops 129 --mb 1
sudo ./dsa-gpu-bench --size 4096 --mode batch --direction read \
  --batch 32 --depth 1 --ops 129 --mb 1

# 上述全部PASS且无新内核异常后，再逐步增加并发
sudo ./dsa-gpu-bench --size 512 --mode batch --direction read \
  --batch 128 --depth 4 --mb 8 --seconds 10 --warmup-ms 1000

sudo ./dsa-gpu-bench --size 4096 --mode batch --direction read \
  --batch 128 --depth 4 --mb 8 --seconds 10 --warmup-ms 1000

# 扫描：任一次失败就停止
sudo bash run_matrix.sh | tee read-results.txt
sudo env DG_DIRECTION=write bash run_matrix.sh | tee write-results.txt
```

read=CPU→GPU payload，write=GPU→CPU payload，mixed=按block划分固定50%方向；SQE始终GPU→CPU。组间数据区不重叠，组内采用带seed的仿射置换，打散遍历后循环，不是IID/Zipf。固定模式数据的最终校验不能判断每次重复请求是否都产生新的内容变化。

工作集需满足data_bytes/groups/block_bytes >= batch×depth，避免在途目标重叠。支持1～256MiB的2次幂数据区，batch为2～1024的2次幂，同时受总映射预算限制。SQE获取最多64KiB且不跨GPU映射页；实际payload不会扩大到2GiB。

4group×batch1024×depth16×4KiB需要至少256MiB数据区，另有4MiB SQE，共260MiB映射请求，超过默认32MiB软件预算。本次实际连256MiB数据区都固定失败，不能继续沿用该最大参数命令。512B的相同并发配置也需要32MiB数据+4MiB SQE，共36MiB，仍超过默认预算。硬件支持1024项batch不意味着BAR1足以固定相应工作集。

健康环境中可用`nvidia-smi -i 0 -q -d MEMORY`查看对应GPU的BAR1总量/使用量；显存空闲量不等于BAR1空闲量，检查与映射之间也存在其它客户端竞争。只有管理员确认足够BAR1余量和业务许可后，才能在正常加载时调大`max_pinned_mb`；不要在当前异常内核上通过调参重试。接口正常返回`-ENOMEM`时，新版应按所有权安全清理并报告失败，而不是把内存不足变成Oops。

IOPS=测量区间完成的payload条数/秒，每512B/4KiB算一次，不把整个BATCH算一次。payload_GBps使用相同时间区间；SQE_IOPS统计取回的逻辑命令数。mixed带宽是两个方向payload之和。各组完成数和CPU编号也会输出。

按秒模式先预热，再按CPU观察到整个batch完成的时间计数，结束后排空；预热及最终排空不计入性能分子，仍参与校验。ops模式是所有组的总请求数，包含流水线启动和排空，不支持预热。初始化、P2P映射、主机/GPU最终校验在计时外。

扫描脚本自动增大工作集以覆盖在途地址，读取模块映射预算并跳过超预算点；比较候选点时应统一工作集、CPU/NUMA放置和方向，并多次测量。最大参数不保证最大吞吐，结果是该软件与GPU拓扑下单DSA的可达性能，不自动等于硬件理论上限。`--seconds`只限制测量窗口，不包含初始化、映射和最终清理耗时。

正常完成后可卸载测试模块：

```bash
sudo rmmod dsa_gpu_test
sudo accel-config list -i
```

DSA和WQ应仍enabled并保留dmaengine绑定。再次insmod只是重新借用通道，不要求停用设备，也不会执行EVL清理。

## 7. 内核EVL缺陷与在线修复边界

上游52d2edea0d63c修复EVL对象未分配时的空指针访问，参见[上游修复](https://lists.openwall.net/linux-kernel/2026/01/15/2221)。fix_idxd_evl_guard.py仅准备free路径的源码空指针检查，不包含完整FLR恢复修复，不安装驱动，也不能恢复已going的模块。

长期应由管理员在维护窗口部署确认包含完整修复的内核/IDXD驱动。新版测试避免主动进入整设备解绑路径，但管理员以后手动disable-device/卸载idxd仍可能触发旧内核缺陷。不能以新版测试替代底层驱动修复。

Ubuntu 6.8.0-138不是原始上游v6.8源码。2024年的d5638de827cf（Convert spinlock to mutex to lock evl workqueue）修复可睡眠操作的锁上下文问题；2026年的52d2edea0d63c修复EVL对象不存在时的访问问题。这是两个独立修改，发行版可能已经回移前者、尚未包含后者，不能仅凭uname中的6.8判断补丁集合。spinlock/mutex两者都要求锁对象存在，换回spinlock不会解决NULL解引用。

旧的gencfg.evl_en判断检查硬件寄存器位，不等于软件evl对象非NULL。如果目标源码仍有该判断，应进一步核对它是否在锁之前、实际运行驱动与源码是否匹配；不能在未检查完整目标函数时断言Ubuntu已经回移了哪组补丁。另一个EVL内存泄漏修复ee66bc295783会删除寄存器检查，其前提是空指针保护已经存在，移植时需一并核对依赖。

## 8. 验证范围

开发环境仍为macOS，无真实Linux DSA/NVIDIA硬件或nvcc。本机完成：

* make check：共用头C++编译、ABI、WQ容量预算、1024项batch与GPU页边界、尾批、环形背压/在途地址不重叠、真实host_pages.h的C编译和部分分配失败回收、EVL源码补丁测试。
* test_backend.py编译真实backend.c，在接口替身上检查通道独占申请、全部8个失败位置的部分回滚、重复清理、不接管已有客户端、设备/WQ配置和绑定不变；并检查源码没有整设备生命周期调用。
* test_prepare.py在临时proc/sysfs替身上执行真实准备脚本，验证默认只读、已就绪时幂等、配置不匹配/going/Oops时拒绝操作且不改变任何模拟设备状态。
* test_gpu_region.py编译实际gpu_region.h并注入失败，包含失败接口写出非NULL毒指针、get成功/map失败、data成功/SQ失败、版本/布局不兼容、重复清理、unmap/put失败保留所有权等情形；断言不对失败输出调用释放接口。
* test_kernel_syntax.py及test_cuda_syntax.py使用接口/SDK声明替身做语法检查，不等于真实Linux/nvcc编译。
* bash -n检查准备、诊断和扫描脚本。

v3.1的真实Linux编译、加载、通道申请/归还、GPU传输及反复装卸尚需健康测试环境验证。公用服务器有going/Oops残留时，先暂停实验，不能在该状态上直接用新代码验证安全性。
