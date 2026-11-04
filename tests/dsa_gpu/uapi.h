/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DSA_GPU_UAPI_H
#define DSA_GPU_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define DG_ALIGN 65536ULL
#define DG_WINDOW 128
/* 简化的 64B SQE：只保留搬运测试需要的字段，不是 NVMe 协议实现。
 * read=0：模拟 SSD 读，CPU→GPU；write=1：模拟 SSD 写，GPU→CPU。 */
struct dg_cmd { __u64 offset, tag; __u32 bytes, write; __u64 reserved[5]; };
struct dg_run {
    __u64 gpu_data, gpu_sq, data_bytes, sq_bytes, operations;
    __u32 block_bytes, mode, batch_size, depth, direction, seed;
    __u64 sq_ns, payload_ns, total_ns, completed, host_errors;
};
#define DG_RUN _IOWR('G', 1, struct dg_run)
/* 两端共用相同的可重复模式，逐 64 位字校验，避免只校验首尾。 */
#ifdef __CUDACC__
#define DG_HD __host__ __device__
#else
#define DG_HD
#endif
static DG_HD inline __u64 dg_pattern(__u64 word) {
    return (word * 0x9e3779b97f4a7c15ULL) ^ 0xd5a0123456789abcULL;
}
/* 对 2 的幂个 block 使用仿射置换：每一轮访问所有 block，顺序打散。
 * 不是独立同分布采样；窗口内无重复地址，使异步写不存在地址竞争。 */
static DG_HD inline __u64 dg_block(__u64 i, __u64 blocks, __u32 seed) {
    return (i * (((__u64)seed << 1) | 1) + (seed ^ 0x5bd1e995U)) & (blocks - 1);
}
static DG_HD inline __u32 dg_write(__u64 block, __u32 direction) {
    return direction == 2 ? (__u32)(block & 1) : direction;
}
#endif
