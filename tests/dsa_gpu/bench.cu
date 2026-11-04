// SPDX-License-Identifier: GPL-2.0-only
/* CUDA 端负责：分配/初始化 GPU 内存、生成 SQE、触发内核测试、校验结果。
 * 测量阶段没有 cudaMemcpy payload；CUDA 拷贝仅用于计时后的校验。
 */
#include <cuda.h>
#include <cuda_runtime.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <vector>
#include <stdexcept>
#include <string>
#include "uapi.h"

static void ck(cudaError_t e) {
    if (e != cudaSuccess) throw std::runtime_error(cudaGetErrorString(e));
}
/* 用 CUDA kernel 生成命令，使 SQE 确实位于 GPU 内存。
 * offset 为 512B/4KiB 对齐，全部字段清零后写入，tag 用于检查过期 SQE。 */
__global__ void init_sq(dg_cmd *sq, unsigned long long n,
                        unsigned long long blocks, unsigned bytes,
                        unsigned direction, unsigned seed) {
    for (unsigned long long i = blockIdx.x * blockDim.x + threadIdx.x;
         i < n; i += gridDim.x * blockDim.x) {
        auto b = dg_block(i, blocks, seed);
        dg_cmd c = {};
        c.offset = b * bytes; c.tag = i; c.bytes = bytes;
        c.write = dg_write(b, direction); sq[i] = c;
    }
}
__global__ void init_data(unsigned long long *p, unsigned long long words,
                          unsigned bytes, unsigned direction) {
    for (unsigned long long i = blockIdx.x * blockDim.x + threadIdx.x;
         i < words; i += gridDim.x * blockDim.x) {
        // read 的目标 GPU 初始取反，确保 DSA 不执行时校验必然失败。
        bool write = dg_write(i * 8 / bytes, direction);
        p[i] = dg_pattern(i) ^ (write ? 0ULL : ~0ULL);
    }
}
struct gpu_mem {
    void *raw = nullptr, *aligned = nullptr;
    explicit gpu_mem(size_t bytes) {
        ck(cudaMalloc(&raw, bytes + DG_ALIGN));
        aligned = (void *)(((uintptr_t)raw + DG_ALIGN - 1) & ~(DG_ALIGN - 1));
        // 按 GPUDirect 要求使用同步内存操作属性，建立与 CUDA API 的同步语义。
        unsigned int one = 1;
        CUresult e = cuPointerSetAttribute(&one, CU_POINTER_ATTRIBUTE_SYNC_MEMOPS,
                                          (CUdeviceptr)aligned);
        if (e != CUDA_SUCCESS) {
            cudaFree(raw); raw = nullptr;
            throw std::runtime_error("cuPointerSetAttribute(SYNC_MEMOPS) failed");
        }
    }
    ~gpu_mem() { if (raw) cudaFree(raw); }
};
static unsigned long long number(const char *s) {
    char *end; errno = 0;
    auto v = strtoull(s, &end, 0);
    if (errno || !*s || *end || *s == '-') throw std::runtime_error("无效数字参数");
    return v;
}
int main(int argc, char **argv) {
    int fd = -1;
    try {
        dg_run r = {};
        r.data_bytes = 2ULL << 20; r.operations = 65536; r.block_bytes = 512;
        r.batch_size = 32; r.depth = 4; r.seed = 12345;
        int gpu = 0;
        for (int i = 1; i < argc; i++) {
            std::string key = argv[i];
            if (key == "--help") {
                puts("用法: dsa-gpu-bench [--size 512|4096] [--mode sync|batch]"
                     " [--direction read|write|mixed] [--ops 65536] [--mb 2]"
                     " [--batch 32] [--depth 4] [--seed 12345] [--gpu 0]");
                return 0;
            }
            if (++i >= argc) throw std::runtime_error("参数缺少值");
            const char *v = argv[i];
            if (key == "--mode") {
                if (!strcmp(v,"sync")) r.mode = 0;
                else if (!strcmp(v,"batch")) r.mode = 1;
                else throw std::runtime_error("mode 必须是 sync/batch");
            } else if (key == "--direction") {
                if (!strcmp(v,"read")) r.direction = 0;
                else if (!strcmp(v,"write")) r.direction = 1;
                else if (!strcmp(v,"mixed")) r.direction = 2;
                else throw std::runtime_error("direction 必须是 read/write/mixed");
            } else {
                auto x = number(v);
                if (key == "--size" && (x == 512 || x == 4096)) r.block_bytes = x;
                else if (key == "--ops" && x && x <= 1048576) r.operations = x;
                else if (key == "--mb" && x && x <= 64 && !(x & (x-1))) r.data_bytes = x << 20;
                else if (key == "--batch" && x >= 2 && x <= 32) r.batch_size = x;
                else if (key == "--depth" && x >= 1 && x <= 16) r.depth = x;
                else if (key == "--seed" && x <= 0xffffffffULL) r.seed = x;
                else if (key == "--gpu" && x <= 1024) gpu = x;
                else throw std::runtime_error("未知参数或数值超出范围: " + key);
            }
        }
        ck(cudaSetDevice(gpu)); ck(cudaFree(nullptr));
        int supported = 0;
        ck(cudaDeviceGetAttribute(&supported, cudaDevAttrGPUDirectRDMASupported, gpu));
        if (!supported) throw std::runtime_error("GPU/驱动未报告 GPUDirect RDMA 支持");
        static_assert(sizeof(dg_cmd) == 64, "SQE 必须为64字节");
        r.sq_bytes = (r.operations * sizeof(dg_cmd) + DG_ALIGN - 1) & ~(DG_ALIGN - 1);
        gpu_mem data(r.data_bytes), sq(r.sq_bytes);
        auto blocks = r.data_bytes / r.block_bytes;
        init_sq<<<128,256>>>((dg_cmd *)sq.aligned, r.operations, blocks,
                             r.block_bytes, r.direction, r.seed);
        ck(cudaGetLastError());
        init_data<<<128,256>>>((unsigned long long *)data.aligned,
                               r.data_bytes / 8, r.block_bytes, r.direction);
        ck(cudaGetLastError()); ck(cudaDeviceSynchronize());
        r.gpu_data = (uintptr_t)data.aligned; r.gpu_sq = (uintptr_t)sq.aligned;
        fd = open("/dev/dsa_gpu_test", O_RDWR | O_CLOEXEC);
        if (fd < 0) throw std::runtime_error(std::string("open: ") + strerror(errno));
        if (ioctl(fd, DG_RUN, &r))
            throw std::runtime_error(std::string("DG_RUN: ") + strerror(errno) + "；请查看 dmesg");
        close(fd); fd = -1;
        /* ioctl 返回时 DSA 已完成且 DRAIN 已成功。随后进入 CUDA API 建立
         * 顺序，再把结果拷回校验；这些操作均在内核计时范围以外。 */
        ck(cudaDeviceSynchronize());
        std::vector<unsigned long long> result(r.data_bytes / 8);
        ck(cudaMemcpy(result.data(), data.aligned, r.data_bytes, cudaMemcpyDeviceToHost));
        std::vector<bool> touched(blocks, false);
        for (unsigned long long i = 0; i < r.operations; i++)
            touched[dg_block(i, blocks, r.seed)] = true;
        unsigned long long errors = 0;
        for (size_t i = 0; i < result.size(); i++) {
            auto b = i * 8 / r.block_bytes;
            bool unchanged = !dg_write(b, r.direction) && !touched[b];
            auto expected = dg_pattern(i) ^ (unchanged ? ~0ULL : 0ULL);
            if (result[i] != expected) {
                if (errors < 4) fprintf(stderr, "GPU word=%zu got=%llx expected=%llx\n",
                                       i, result[i], (unsigned long long)expected);
                errors++;
            }
        }
        printf("mode=%s direction=%s size=%u ops=%llu completed=%llu batch=%u depth=%u\n",
            r.mode ? "batch" : "sync", r.direction == 0 ? "CPU->GPU" :
            r.direction == 1 ? "GPU->CPU" : "mixed", r.block_bytes,
            (unsigned long long)r.operations, (unsigned long long)r.completed, r.batch_size, r.depth);
        printf("SQE_ms=%.3f payload_ms=%.3f total_ms=%.3f\n",
               r.sq_ns/1e6, r.payload_ns/1e6, r.total_ns/1e6);
        printf("end_to_end_IOPS=%.0f payload_GBps=%.3f\n",
               r.completed*1e9/r.total_ns, r.completed*(double)r.block_bytes/r.payload_ns);
        printf("host_errors=%llu gpu_errors=%llu %s\n",
            (unsigned long long)r.host_errors, errors,
            (errors || r.host_errors || r.completed != r.operations) ? "FAIL" : "PASS");
        return (errors || r.host_errors || r.completed != r.operations) ? 1 : 0;
    } catch (const std::exception &e) {
        if (fd >= 0) close(fd);
        fprintf(stderr, "%s\n", e.what()); return 1;
    }
}
