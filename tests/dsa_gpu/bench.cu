// SPDX-License-Identifier: GPL-2.0-only
/* CUDA端只准备GPU数据/SQE和最终校验；计时区间的数据搬运全部由DSA执行。 */
#include <cuda.h>
#include <cuda_runtime.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <cstdint>
#include <vector>
#include <stdexcept>
#include <string>
#include "uapi.h"
static void ck(cudaError_t e) { if (e != cudaSuccess) throw std::runtime_error(cudaGetErrorString(e)); }
struct device_fd {
    int fd;
    device_fd() : fd(open("/dev/dsa_gpu_test", O_RDWR | O_CLOEXEC)) {
        if (fd < 0) throw std::runtime_error(std::string("open: ") + strerror(errno));
    }
    ~device_fd() { close(fd); }
};
struct gpu_mem {
    void *raw = nullptr, *aligned = nullptr;
    explicit gpu_mem(size_t bytes) {
        ck(cudaMalloc(&raw, bytes + DG_ALIGN));
        aligned = (void *)(((uintptr_t)raw + DG_ALIGN - 1) & ~(DG_ALIGN - 1));
        unsigned int one = 1;
        if (cuPointerSetAttribute(&one, CU_POINTER_ATTRIBUTE_SYNC_MEMOPS,
                                 (CUdeviceptr)aligned) != CUDA_SUCCESS) {
            cudaFree(raw); raw = nullptr;
            throw std::runtime_error("设置SYNC_MEMOPS失败");
        }
    }
    ~gpu_mem() { if (raw) cudaFree(raw); }
};
__global__ void init_data(unsigned long long *p, unsigned long long words,
                          unsigned block, unsigned direction) {
    for (unsigned long long i = blockIdx.x * blockDim.x + threadIdx.x;
         i < words; i += gridDim.x * blockDim.x) {
        bool write = dg_write(i * 8 / block, direction);
        p[i] = dg_pattern(i) ^ (write ? 0ULL : ~0ULL);
    }
}
__global__ void init_sq(dg_cmd *sq, unsigned long long stride,
                        unsigned long long bytes, unsigned block,
                        unsigned groups, unsigned direction, unsigned seed) {
    unsigned long long blocks = bytes / groups / block;
    for (unsigned long long i = blockIdx.x * blockDim.x + threadIdx.x;
         i < blocks * groups; i += gridDim.x * blockDim.x) {
        unsigned g = i / blocks;
        ((dg_cmd *)((char *)sq + g * stride))[i % blocks] =
            dg_command(g, i % blocks, bytes, block, groups, direction, seed);
    }
}
static unsigned long long number(const char *s) {
    char *end; errno = 0;
    auto v = strtoull(s, &end, 0);
    if (errno || !*s || *end || *s == '-') throw std::runtime_error("无效数字");
    return v;
}
static void help() {
    puts("DSA GPU benchmark ABI3\n"
         "  --size 512|4096 --mode sync|batch --direction read|write|mixed\n"
         "  --batch 2..1024 (2的幂) --depth 1..16 --mb 1..256 (2的幂)\n"
         "  --seconds 10 --warmup-ms 1000  或 --ops N (总请求数，关闭预热)\n"
         "  --seed 12345 --gpu 0 --info\n"
         "默认：512B、batch=32、depth=8、8MiB、预热1秒、计时5秒。\n"
         "read=CPU→GPU，write=GPU→CPU；SQE始终从GPU获取。");
}
int main(int argc, char **argv) {
    try {
        static_assert(sizeof(dg_cmd) == 64, "SQE ABI");
        dg_run r = {};
        r.abi = DG_ABI; r.data_bytes = 8ULL << 20; r.block_bytes = 512;
        r.mode = 1; r.batch_size = 32; r.depth = 8; r.seconds = 5;
        r.warmup_ms = 1000; r.seed = 12345;
        bool depth_set = false, time_set = false, ops_set = false, warmup_set = false, info_only = false;
        int gpu = 0;
        for (int i = 1; i < argc; i++) {
            std::string key = argv[i];
            if (key == "--help") { help(); return 0; }
            if (key == "--info") { info_only = true; continue; }
            if (++i >= argc) throw std::runtime_error("参数缺少值: " + key);
            const char *v = argv[i];
            if (key == "--mode") {
                if (!strcmp(v, "sync")) r.mode = 0;
                else if (!strcmp(v, "batch")) r.mode = 1;
                else throw std::runtime_error("mode必须是sync/batch");
            } else if (key == "--direction") {
                if (!strcmp(v, "read")) r.direction = 0;
                else if (!strcmp(v, "write")) r.direction = 1;
                else if (!strcmp(v, "mixed")) r.direction = 2;
                else throw std::runtime_error("direction必须是read/write/mixed");
            } else {
                auto x = number(v);
                if (key == "--size" && (x == 512 || x == 4096)) r.block_bytes = x;
                else if (key == "--mb" && x && x <= 256 && !(x & (x-1))) r.data_bytes = x << 20;
                else if (key == "--batch" && x >= 2 && x <= DG_MAX_BATCH && !(x & (x-1))) r.batch_size = x;
                else if (key == "--depth" && x && x <= DG_MAX_DEPTH) { r.depth = x; depth_set = true; }
                else if (key == "--seconds" && x && x <= 3600) { r.seconds = x; time_set = true; }
                else if (key == "--warmup-ms" && x <= 60000) { r.warmup_ms = x; warmup_set = true; }
                else if (key == "--ops" && x && x <= (1ULL<<32)) { r.operations = x; ops_set = true; }
                else if (key == "--seed" && x <= 0xffffffffULL) r.seed = x;
                else if (key == "--gpu" && x <= 1024) gpu = x;
                else throw std::runtime_error("未知参数或超出范围: " + key);
            }
        }
        if (time_set && ops_set) throw std::runtime_error("seconds和ops只能选择一种");
        if (ops_set) {
            if (warmup_set && r.warmup_ms) throw std::runtime_error("ops模式不支持预热");
            r.seconds = r.warmup_ms = 0;
        }
        if (!r.mode) {
            if (depth_set && r.depth != 1) throw std::runtime_error("sync模式要求depth=1");
            r.depth = 1;
        }
        device_fd fd;
        dg_info info = {};
        if (ioctl(fd.fd, DG_INFO, &info))
            throw std::runtime_error(std::string("DG_INFO失败，请加载配套ABI3模块: ") + strerror(errno));
        if (info.abi != DG_ABI || !info.groups || info.groups > DG_GROUPS)
            throw std::runtime_error("模块ABI/配置不匹配");
        if (info_only) {
            printf("ABI=%u dsa%u groups=%u engines=%u WQs=%u size/WQ=%u max_batch=%u max_pin_MiB=%llu\n",
                   info.abi, info.device_id, info.groups, info.engines, info.wqs, info.wq_size,
                   info.max_batch, (unsigned long long)(info.max_pin_bytes >> 20));
            return 0;
        }
        unsigned batch = r.mode ? r.batch_size : 1;
        if (r.batch_size > info.max_batch || r.depth > info.max_depth ||
            (unsigned long long)batch*r.depth > r.data_bytes/info.groups/r.block_bytes ||
            (!r.seconds && r.operations < info.groups))
            throw std::runtime_error("batch/depth/工作集不匹配：需depth<=WQ容量，且每group的block数>=batch*depth；ops>=groups");
        auto stride = dg_sq_stride(r.data_bytes, r.block_bytes, info.groups);
        r.sq_bytes = stride * info.groups;
        if (r.data_bytes + r.sq_bytes > info.max_pin_bytes)
            throw std::runtime_error("GPU data+SQE超出模块映射预算；减小mb/batch/depth，或由管理员确认BAR1余量后调整max_pinned_mb（软件预算不代表硬件可用空间）");
        ck(cudaSetDevice(gpu)); ck(cudaFree(nullptr));
        int supported = 0;
        ck(cudaDeviceGetAttribute(&supported, cudaDevAttrGPUDirectRDMASupported, gpu));
        if (!supported) throw std::runtime_error("GPU未报告GPUDirect RDMA支持");
        gpu_mem data(r.data_bytes), sq(r.sq_bytes);
        ck(cudaMemset(sq.aligned, 0, r.sq_bytes));
        init_data<<<128,256>>>((unsigned long long *)data.aligned, r.data_bytes/8,
                               r.block_bytes, r.direction);
        ck(cudaGetLastError());
        init_sq<<<128,256>>>((dg_cmd *)sq.aligned, stride, r.data_bytes,
                             r.block_bytes, info.groups, r.direction, r.seed);
        ck(cudaGetLastError()); ck(cudaDeviceSynchronize());
        r.gpu_data = (uintptr_t)data.aligned; r.gpu_sq = (uintptr_t)sq.aligned;
        printf("ABI=%u dsa%u groups=%u engines=%u WQs=%u size/WQ=%u max_batch=%u max_transfer=%llu\n",
            info.abi, info.device_id, info.groups, info.engines, info.wqs, info.wq_size,
            info.max_batch, (unsigned long long)info.max_transfer);
        printf("size=%u mode=%s batch=%u depth=%u direction=%u working_set_MiB=%llu\n",
            r.block_bytes, r.mode ? "batch" : "sync", batch, r.depth, r.direction,
            (unsigned long long)(r.data_bytes>>20));
        fflush(stdout);
        puts("进入DG_RUN：先映射GPU并分配主机资源，随后才开始计时；阶段进度见dmesg。");
        fflush(stdout);
        if (ioctl(fd.fd, DG_RUN, &r))
            throw std::runtime_error(std::string("DG_RUN: ") + strerror(errno) + "；请查看dmesg");
        puts("DG_RUN已返回，开始GPU结果校验（不计时）。");
        fflush(stdout);
        /* ioctl返回前全部WQ均已DRAIN；此后CUDA API读取GPU结果，位于计时范围外。 */
        ck(cudaDeviceSynchronize());
        std::vector<unsigned long long> result(r.data_bytes/8);
        ck(cudaMemcpy(result.data(), data.aligned, r.data_bytes, cudaMemcpyDeviceToHost));
        auto blocks = r.data_bytes / info.groups / r.block_bytes;
        std::vector<bool> touched(r.data_bytes/r.block_bytes, false);
        unsigned long long executed = 0, sq_measured = 0;
        for (unsigned g = 0; g < info.groups; g++) {
            // 回收顺序与请求顺序一致，executed表示该group已完成的前缀长度。
            auto n = r.stats[g].executed < blocks ? r.stats[g].executed : blocks;
            for (unsigned long long i = 0; i < n; i++)
                touched[g*blocks + dg_block(i, blocks, r.seed+g)] = true;
            executed += r.stats[g].executed; sq_measured += r.stats[g].sq_measured;
            printf("group%u fetch_wq=%u copy_wq=%u cpu=%u/%u measured=%llu executed=%llu\n",
                g, g*2, g*2+1, r.stats[g].fetch_cpu, r.stats[g].worker_cpu,
                (unsigned long long)r.stats[g].measured, (unsigned long long)r.stats[g].executed);
        }
        unsigned long long errors = 0;
        for (size_t i = 0; i < result.size(); i++) {
            auto b = i*8/r.block_bytes;
            bool unchanged = !dg_write(b, r.direction) && !touched[b];
            auto expected = dg_pattern(i) ^ (unchanged ? ~0ULL : 0ULL);
            if (result[i] != expected) {
                if (errors < 4) fprintf(stderr, "GPU word=%zu got=%llx expected=%llx\n",
                                       i, result[i], (unsigned long long)expected);
                errors++;
            }
        }
        bool bad = errors || r.host_errors || !r.completed ||
                   (!r.seconds && executed != r.operations);
        double sec = r.elapsed_ns/1e9;
        printf("elapsed_s=%.6f completed=%llu IOPS=%.0f payload_GBps=%.3f SQE_IOPS=%.0f\n",
            sec, (unsigned long long)r.completed, r.completed/sec,
            r.completed*(double)r.block_bytes/r.elapsed_ns, sq_measured/sec);
        printf("host_errors=%llu gpu_errors=%llu %s\n",
               (unsigned long long)r.host_errors, errors, bad ? "FAIL" : "PASS");
        return bad ? 1 : 0;
    } catch (const std::exception &e) { fprintf(stderr, "%s\n", e.what()); return 1; }
}
