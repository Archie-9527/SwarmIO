# v2-baseline：运行、复位及卸载卡住分析

日期：2026-11-12。检查对象：SwarmIO `e99f01b`、BaM `5b11a4b`。
本轮仅分析、补充文档，没有修改执行代码，没有连接服务器。

2026-11-18 更新：本文记录的是补丁前的源码行为。稳定性补丁和当前卸载步骤见 [基线说明](v2-baseline.zh-CN.md)。

## 1. 证据与结论边界

旧分支 `7721b3e:README.md` 记录过 `__fill_cq_result` 空指针、worker Oops/`exited with irqs disabled`，
以及 `swarmio_dispatcher_exit -> kthread_stop` 的卸载等待。
本轮未找到对应原始服务器栈日志，所以这些属于历史记录，不能当成已经重新复现或已经定位了唯一根因。
当前源码则能确认以下生命周期缺口。它们能解释一组可行的故障链，但不能证明此前每次卡住均由同一个问题引起。

普通编译、链接不会执行 DSA 搬运。需要区分编译进程慢/内存压力，与 insmod/modprobe、GPU运行、rmmod 或脚本 EXIT 清理卡住。
如果 make 看起来卡住，应先看进程栈与等待点，不能直接归因 DSA。已有 Oops 的内核也可能让后续构建/清理间接受影响。

## 2. 优先级最高的源码问题

### 2.1 超时不等于 DMA 停止，但代码提前撤销了软件跟踪

`dsa/dsa.h::__dsa_sync_wait()` 超时后只打印并返回，不取消 DMA，也不报告硬件已经静止。
`dsa/single.c::dsa_dma_wait_one_single()` 随即清除 issued bit。
`dsa/batch.c::dsa_dma_wait_oldest_batch()` 清 bit、移出链表；失败时清空 num_filled，却不对这些请求执行完成回调。

后果：

- 硬件可能仍访问描述符、完成记录、主机或 GPU 缓冲区，但软件已失去相应在途跟踪。
- batch 中请求可能保留 `is_issued=true/is_copied=false`，永远无法发布 CQE 或回收 local_queue head。
- 后续回收或复用这些资源可能形成迟到 DMA 写入已释放/复用内存。

这是源码可确认的协议缺口，不应通过缩短超时或超时后直接释放来解决。

### 2.2 DMA 出错后的 CPU fallback 没有覆盖全部旧在途请求

`src/worker.c` 的提交失败路径设置全局 `worker_using_dma=false`，对当前请求进行 CPU copy。
但原先已提交、尚未回调的其它 batch 请求没有由该操作自动完成或排空。
之后 batch 的刷新/等待逻辑又受 `worker_using_dma` 开关控制。

因此“切换 CPU 后端”并不等于恢复队列；GPU 仍可能等待永远不来的 CQE。
GPU `cq_poll()` 本身是持续轮询，没有整体请求超时退出机制。
该现象首先表现为 benchmark 不返回，不足以单独证明内核已经损坏。

### 2.3 reset 屏障缺少故障退出，卸载又等待线程退出

`src/dispatcher.c::swarmio_proc_bars()` 在 controller disable 时设置 stop_flag，
循环等待 `num_waiting` 等于预期服务线程数，循环没有超时、故障或 kthread stop 条件。
如果一个 worker 已经 Oops、尚卡在 DMA，或无法到达暂停点，dispatcher0 就可能一直等。

`src/common.h::__thread_wait_on_flag()` 的 wait 条件只有 flag 清零，未包含 `kthread_should_stop()`；
dispatcher 启动等待循环也不检查停止请求，且使用线程间共享的 static 循环变量。
`kthread_stop()` 是请求停止并等待退出，不是强杀：若线程内层循环/等待不退出，卸载也就无法结束。
参见 [Linux kthread 文档](https://docs.kernel.org/driver-api/basics.html#c.kthread_stop)。

这一机制与历史 `rmmod -> kthread_stop` 栈相符，但还需栈确认目标线程具体卡在 reset、wait_event、DMA等待还是其它锁。

### 2.4 退出时释放 DMA 资源早于设备静止保障

`src/main.c::swarmio_exit()` 的顺序为：移除虚拟 PCI → dispatcher_exit → worker_exit → dsa_disable_devices。
single/batch remove 函数直接释放描述符和相关缓冲区，没有先完成明确的跨提交者停止/排空协议。
停止 CPU 线程本身不证明 DSA 已停止。

正常结束且确实没有在途 DMA 时未必触发；异常中断、GPU进程退出、reset或超时后再卸载时，风险明显增加。
队列删除函数也直接清除 SQ/CQ 指针并释放；完成路径会直接解引用这些指针。
如果队列生命周期与异常中的在途请求失配，就可能出现历史记录中的 CQ/SQ 空指针或释放后访问。
正常 NVMe host 应先排空，不能把这条风险说成每次正常 delete 都会出错。

### 2.5 部分初始化失败与部分 WQ 成功未完整处理

- `dsa/single.c` 中途描述符分配失败会直接返回，未释放此前已分配的描述符。
- dispatcher/worker 的 initialized 计数在整个对象成功后才增加，失败对象可能未纳入回滚。
- `dsa_wqs_enable()` 只要至少一个 WQ 成功就返回成功；之后需要的某条 WQ 可能没有就绪。
- `dsa_set_params()` 调用参数校验但没有处理返回值。
- 使用阻塞描述符分配，配置错误或资源泄漏可以表现为加载阶段等待。

这些问题可能解释反复装卸后越来越难恢复、加载卡住或后续空指针，但具体哪一项发生需要初始化日志。

## 3. GPU 队列可见性与底层驱动是另两条独立问题链

作者原版用 WT memremap 映射 GPU 队列，普通 memcpy 写 CQE；BaM CQ 轮询关键字段没有后续适配版的 volatile 读取。
新 GPU/驱动上可能出现 CPU已写CQE、GPU仍未正确观察的停顿。源码差异可确认，但不能单凭这点证明某次停顿就是缓存可见性问题；
旧记录也明确说明，部分 acquire 实验并没有解决当时卡顿。
增加QD、延长等待时间或只检查最终数据，都不足以给该协议下结论。

另一条是 IDXD 驱动本身：本基线恢复了原作 `__idxd_wq_quiesce()`、`idxd_drv_disable_wq()`、`device_release_driver()` 路径。
旧记录曾讨论 EVL 对象不存在时的清理/恢复问题。
上游确有修复 [Fix crash when the event log is disabled](https://lists.openwall.net/linux-kernel/2026/01/15/2221)，
相关修复依赖也有 [stable 维护者说明](https://lkml.rescloud.iu.edu/hypermail/linux/kernel/2605.1/01926.html)。

不能据此断言所有 Linux 6.8 都有该缺陷。
[上游通告](https://kernel.googlesource.com/pub/scm/linux/security/vulns/+/104f5df494e2b3a98cefcda25ff01cdead552511/cve/published/2026/CVE-2026-31443.mbox)
对 FLR 缺陷的版本说明与原始 v6.8 不同；发行版回移补丁后还可能存在其它组合。
必须核对目标发行版完整版本、实际加载 idxd 驱动、匹配源码和回移记录。
若真实栈落在 idxd_device_evl_free/restore、驱动 remove、mutex 等，应独立检查内核；只改 SwarmIO 不能证明修复了驱动问题。

本分支 BaM 为兼容新驱动切换 persistent pin API，但仍沿用作者的释放流程，未建立与 SwarmIO 全部 DMA 的联动排空。
unmap/put 的错误和失败 API 输出所有权也没有后续版本的完整保护；这同样需要纳入异常退出验收。

## 4. 为什么后续可能必须重启

可能的链路为：

```text
I/O异常或CQ不可见 → GPU等待完成
    → 中断进程/自动清理 → reset/删除队列/释放映射
    → 旧DMA仍在途，或worker异常
    → reset等待人数永远不足 / kthread_stop无法返回
    → 模块Unloading、驱动状态不完整，后续无法复用
```

这是一条由源码支持的可能故障链，不是已经得到原始栈证明的唯一历史原因。
若只是 GPU 请求仍在等待且内核没有异常，不能一概说必需重启；若已经内核Oops、迟到DMA写入、驱动清理失败，则不能默认在线状态可复用。
强制卸载、反复reset或kill用户进程不能补回损坏的内核生命周期。

另外，作者 `scripts/run_bam.sh` 的 EXIT trap 会自动卸载模块并忽略失败。
异常运行时这会把最初的I/O卡顿进一步带进清理路径。问题澄清前应使用分步命令，不执行大规模自动扫描。

## 5. 下一轮需要保存的手工证据

先保留首次异常，不立即运行卸载/再次加载：

```bash
sudo dmesg -T > hang-dmesg.log
ps -eo pid,ppid,stat,wchan:40,comm,args > hang-processes.log
cat /proc/modules > hang-modules.log
# 从ps找出卡住进程或服务线程PID，逐个保存：
hang_pid=12345
sudo cat /proc/$hang_pid/stack > hang-stack-$hang_pid.log
```

至少覆盖 benchmark、modprobe/rmmod、dispatcher0和未进入暂停的worker。
同时记录首次卡住发生在编译、加载、GPU运行、Ctrl+C、controller reset还是卸载，以及是否此前已出现Oops。
GPU驱动已失去响应时，不要求故障采集中额外调用可能阻塞的nvidia-smi。

最有价值的是首次BUG/Oops前后的完整日志和线程栈；最后一个等待在kthread_stop的进程通常只是后果。

## 6. 对原作最小基线的处理建议

`v2-baseline` 是忠实原作的源码起点，还不是通过异常恢复验收的运行版本。
需要独立的一组稳定性补丁，共同应用于未来原路由/NUMA优化两组：

1. 所有等待点能识别停止/故障；reset/线程退出形成可解释的协议。
2. 停止新提交、确认在途DMA完成或设备静止后，才释放描述符、SQ/CQ、主机和GPU缓冲区。
3. DMA失败明确终止本轮；不把未排空队列的CPU fallback当作恢复成功。
4. 修复部分初始化回滚、设备/WQ就绪验证及驱动API资源所有权。
5. GPU队列可见性修复按目标环境证据单独验证，保留对正常路径开销的记录。

这些属于复现基础的正确性修复，不是NUMA性能贡献；也不应借此替换作者BATCH策略、SQE合并、负载或时延模型。
验收先覆盖小请求、队列绕环、正常结束与装卸；故障注入应在独立测试环境中进行，不能承诺硬件永久故障后一定可以无重启恢复。
