/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DSA_GPU_UAPI_H
#define DSA_GPU_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define DG_ABI 3
#define DG_GROUPS 4
#define DG_ALIGN 65536ULL
#define DG_MAX_BATCH 1024
#define DG_MAX_DEPTH 16
#ifdef __CUDACC__
#define DG_HD __host__ __device__
#else
#define DG_HD
#endif
/* 64B简化SQE，不实现NVMe协议；write=0为CPU→GPU，write=1为GPU→CPU。 */
struct dg_cmd { __u64 offset, tag; __u32 bytes, write; __u64 reserved[5]; };
struct dg_info {
    __u32 abi, device_id, groups, engines, wqs, wq_size, max_batch, max_depth;
    __u64 max_transfer, max_pin_bytes;
};
struct dg_group_stats {
    __u64 measured, executed, sq_measured;
    __u32 fetch_cpu, worker_cpu;
};
struct dg_run {
    __u64 gpu_data, gpu_sq, data_bytes, sq_bytes, operations;
    __u32 abi, block_bytes, mode, batch_size, depth, direction, seed;
    __u32 seconds, warmup_ms, reserved;
    __u64 elapsed_ns, completed, host_errors;
    struct dg_group_stats stats[DG_GROUPS];
};
#define DG_INFO _IOR('G', 2, struct dg_info)
#define DG_RUN _IOWR('G', 3, struct dg_run)
static DG_HD inline __u64 dg_pattern(__u64 word) {
    return (word * 0x9e3779b97f4a7c15ULL) ^ 0xd5a0123456789abcULL;
}
/* 带seed的仿射置换：在2的幂个block中打散遍历，不是IID随机采样。 */
static DG_HD inline __u64 dg_block(__u64 i, __u64 blocks, __u32 seed) {
    return (i * (((__u64)seed << 1) | 1) + (seed ^ 0x5bd1e995U)) & (blocks - 1);
}
static DG_HD inline __u32 dg_write(__u64 block, __u32 direction) {
    return direction == 2 ? (__u32)(block & 1) : direction;
}
static DG_HD inline __u64 dg_sq_stride(__u64 bytes, __u32 block, __u32 groups) {
    __u64 n = bytes / groups / block * sizeof(struct dg_cmd);
    return (n + DG_ALIGN - 1) & ~(DG_ALIGN - 1);
}
/* 各group访问互不重叠的数据区；tag是整个GPU SQ区域内的命令序号。 */
static DG_HD inline struct dg_cmd dg_command(__u32 group, __u64 index,
        __u64 bytes, __u32 block, __u32 groups, __u32 direction, __u32 seed) {
    struct dg_cmd c = {0, 0, 0, 0, {0, 0, 0, 0, 0}};
    __u64 blocks = bytes / groups / block;
    __u64 b = group * blocks + dg_block(index, blocks, seed + group);
    c.offset = b * block;
    c.tag = group * (dg_sq_stride(bytes, block, groups) / sizeof(c)) + index;
    c.bytes = block; c.write = dg_write(b, direction);
    return c;
}
#endif
