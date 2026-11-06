ifneq ($(KERNELRELEASE),)
obj-m := dsa_gpu_test.o
dsa_gpu_test-y := kernel.o backend.o
ccflags-y += -DCONFIG_SWARMIO_HAS_DSA=1 -I$(src)/../../dsa -I$(src)/../../src
ccflags-y += -I$(IDXD_INCLUDE) -I$(NVIDIA_INCLUDE)
else
.DEFAULT_GOAL := all
KDIR ?= /lib/modules/$(shell uname -r)/build
NVCC ?= nvcc
.PHONY: all module user check clean diagnose
all: module user
module:
	@test -f "$(IDXD_INCLUDE)/idxd.h" || (echo '请设置 IDXD_INCLUDE=匹配内核源码/drivers/dma/idxd'; exit 1)
	@test -f "$(NVIDIA_INCLUDE)/nv-p2p.h" || (echo '请设置 NVIDIA_INCLUDE=匹配驱动的头文件目录'; exit 1)
	@test -f "$(NVIDIA_SYMVERS)" || (echo '请设置 NVIDIA_SYMVERS=匹配驱动与运行内核的Module.symvers文件'; exit 1)
	$(MAKE) -C $(KDIR) M=$(CURDIR) IDXD_INCLUDE=$(IDXD_INCLUDE) NVIDIA_INCLUDE=$(NVIDIA_INCLUDE) KBUILD_EXTRA_SYMBOLS=$(NVIDIA_SYMVERS) modules
user: dsa-gpu-bench
dsa-gpu-bench: bench.cu uapi.h
	$(NVCC) -O2 -std=c++14 -Xcompiler=-Wall bench.cu -o $@ -lcuda
check:
	python3 test_model.py
	python3 test_evl_guard.py
	python3 test_backend.py
	python3 test_prepare.py
	python3 test_gpu_region.py
	python3 test_payload.py
diagnose:
	bash diagnose.sh
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(RM) dsa-gpu-bench
endif
