# 兼容实现与历史问题

当前操作步骤见父目录 README。本文件说明保留在源码中的兼容实现，不表示已经在每个环境完成 GPU 实测。

## Linux 6.8 与 6.18

- 内核与 NVIDIA DKMS 输入根据 `uname -r`、当前驱动版本选择，不硬编码旧内核路径。
- `get_user_pages` 使用 Linux 6.5 起的四参数接口；回归检查覆盖 6.8.0 和 6.18.5 条件。
- Linux 6.14 起 `MODULE_IMPORT_NS` 使用字符串；较旧版本保留旧语法。
- miscdevice 替代旧 cdev/class 注册，避免 libnvm 依赖 GPL-only 的设备类符号。
- libnvm 的 DMA 地址尾数组使用柔性数组与 `struct_size`；分配溢出、部分 pin、DMA 映射失败均回收正确范围。
- SwarmIO、libnvm、可选 gdrdrv 均传入 `CONFIG_DEBUG_INFO_BTF_MODULES=`。Kbuild 的 `ifdef` 会把非空的 `n` 视为启用，因此不能写成 `=n`。

## 定制内核的 CXL BAR fixup

服务器之前的 6.18.5 内核含 `pci_fixup_nvmev_cxl_bars`，它按相同 PCI ID 为 SwarmIO 错套了另一套 CXL 布局：BAR0 缩到 64 KiB，增加实际未实现的 BAR2/BAR4。
SwarmIO 在扫描之后、驱动绑定之前恢复自身的单个 1 MiB BAR0，额外 BAR 清空；MSI-X 表仍在 BAR0+0x81000。
该处理保留在当前代码中，不因回到 6.8 就假定自定义 fixup 不存在。已被认领的资源会拒绝改写。

## GDRCopy 与专有 NVIDIA 驱动

Linux 6.18 的 `vm_flags_set` 可引入 GPL-only 的 `__vma_start_write`。
gdrdrv 即使自身声明 GPL 兼容许可，引用专有 nvidia 模块后仍可能无法同时导入 GPL-only 内核符号。
不能仅修改 MODULE_LICENSE 或关闭内核检测来解决。
参见 [GDRCopy 上游问题 #335](https://github.com/NVIDIA/gdrcopy/issues/335)。
Linux 6.8 使用较旧实现，但实际兼容性仍由本次构建的导入符号和当前 Module.symvers 审计确定，不直接按版本号放行。
默认 host 通知不构建、不加载 gdrdrv。选择 `build --notify-memory gdr` 才使用原 GDR 通知路径；两个模式的性能结果分别标记。

## CUDA 与 C++ 头文件

- CPU 用户库使用普通 C++ 编译器，`lane_id` 等 PTX 函数必须限于 `__CUDACC__`，不能通过清空 CUDA 关键字来掩盖错误。
- CoPilotIO 的 CPU/GPU 协议结构在 `copilot_types.h` 中完整定义，消除 `page_cache.h` 对包含顺序的依赖。
- 用户库与 CUDA 基准统一 C++17，要求显式 GPU 架构，CUDAToolkit_ROOT 与选定 nvcc 同源。
- `mmap` 失败按 MAP_FAILED 处理，保留 errno。
- 直接 MMIO 注册使用 `cudaHostRegisterIoMemory | cudaHostRegisterMapped`；注册失败时打印地址、长度、标志。GPU 注册 BAR 的失败不等同于 BAR 被内核屏蔽。

## 测试口径

统一基准每次逻辑操作只发一条 NVMe READ，不使用原 `read_data` 路径的额外读取和页缓存队列记账；不可直接对照旧历史 IOPS。
CoPilotIO 使用 CPU SQ/CQ；BaM 使用 GPU SQ/CQ；两者保留各自完成检测方法。
CPU 映射通知增加 GPU 对主机内存的读取，doorbell relay 额外增加 CPU 转发线程，均不同于原始 GDR+GPU MMIO 路径。
`run.json`、对比 CSV/JSON 记录这些模式，脚本拒绝混用不同 doorbell 模式的日志进行对比。
