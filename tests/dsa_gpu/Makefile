ifneq ($(KERNELRELEASE),)
obj-m := dsa_gpu_test.o
dsa_gpu_test-y := kernel.o backend.o config.o
ccflags-y += -DCONFIG_SWARMIO_HAS_DSA=1 -I$(src)/../../dsa -I$(src)/../../src
ccflags-y += -I$(IDXD_INCLUDE) -I$(NVIDIA_INCLUDE)
else
KDIR ?= /lib/modules/$(shell uname -r)/build
NVCC ?= nvcc
.PHONY: all module user clean check
all: module user
module:
	@test -f "$(IDXD_INCLUDE)/idxd.h" || (echo '请设置 IDXD_INCLUDE=运行内核源码/drivers/dma/idxd'; exit 1)
	@test -f "$(NVIDIA_INCLUDE)/nv-p2p.h" || (echo '请设置 NVIDIA_INCLUDE=匹配已加载驱动的头文件目录'; exit 1)
	@test -f "$(NVIDIA_SYMVERS)" || (echo '请设置 NVIDIA_SYMVERS=匹配运行内核与已加载驱动的Module.symvers'; exit 1)
	$(MAKE) -C $(KDIR) M=$(CURDIR) IDXD_INCLUDE=$(IDXD_INCLUDE) NVIDIA_INCLUDE=$(NVIDIA_INCLUDE) KBUILD_EXTRA_SYMBOLS=$(NVIDIA_SYMVERS) modules
user: dsa-gpu-bench
dsa-gpu-bench: bench.cu uapi.h
	$(NVCC) -O2 -std=c++14 -Xcompiler=-Wall bench.cu -o $@ -lcuda
check:
	python3 test_model.py
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(RM) dsa-gpu-bench
endif
