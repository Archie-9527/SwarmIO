// SPDX-License-Identifier: GPL-2.0-only
#ifndef DG_HOST_PAGES_H
#define DG_HOST_PAGES_H
#include <linux/dma-mapping.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>

/* 每块独立分配并保留其真实 DMA 地址，不假设相邻块物理连续。
 * 请求按自身长度对齐；payload/replay可使用最多64KiB的独立连续块。
 * host[i] 是元数据；它可以由 kvcalloc 使用非连续内存分配。
 * 真正参与 DMA 的数据仍由 dma_alloc_coherent 单独分配。
 */
#define DG_HOST_CHUNK 4096U
struct dg_host_page {
    void *cpu;
    dma_addr_t dma;
};
static inline void dg_host_free_chunks(struct device *dev, struct dg_host_page *host,
                                       u64 bytes, unsigned int chunk)
{
    u64 i;
    if (!host) return;
    for (i = 0; i < bytes / chunk; i++)
        if (host[i].cpu)
            dma_free_coherent(dev, chunk, host[i].cpu, host[i].dma);
    kvfree(host);
}
static inline struct dg_host_page *dg_host_alloc_chunks(struct device *dev, u64 bytes, unsigned int chunk)
{
    u64 i, count;
    struct dg_host_page *host;
    if (chunk < DG_HOST_CHUNK || chunk > 65536 || (chunk & (chunk-1)) || !bytes || bytes % chunk) return NULL;
    count = bytes / chunk;
    host = kvcalloc(count, sizeof(*host), GFP_KERNEL);
    if (!host) {
        pr_err("dsa_gpu_test: host page table allocation failed, bytes=%llu\n",
               (unsigned long long)bytes);
        return NULL;
    }
    for (i = 0; i < count; i++) {
        host[i].cpu = dma_alloc_coherent(dev, chunk, &host[i].dma,
                                         GFP_KERNEL);
        if (!host[i].cpu) {
            pr_err("dsa_gpu_test: host chunk %llu/%llu allocation failed\n",
                   (unsigned long long)i, (unsigned long long)count);
            dg_host_free_chunks(dev, host, bytes, chunk);
            return NULL;
        }
    }
    return host;
}
/* 只在通过请求边界检查之后调用；所有 offset 均落在已分配数据区内。 */
static inline dma_addr_t dg_host_dma_chunk(struct dg_host_page *host, u64 offset, unsigned int chunk)
{
    return host[offset / chunk].dma + offset % chunk;
}
static inline u64 *dg_host_word_chunk(struct dg_host_page *host, u64 word, unsigned int chunk)
{
    u64 offset = word * sizeof(u64);
    return (u64 *)((char *)host[offset / chunk].cpu + offset % chunk);
}
/* Keep the original 4 KiB API for the SQ/payload profile and existing tests. */
static inline struct dg_host_page *dg_host_alloc(struct device *d, u64 bytes)
{ return dg_host_alloc_chunks(d, bytes, DG_HOST_CHUNK); }
static inline void dg_host_free(struct device *d, struct dg_host_page *h, u64 bytes)
{ dg_host_free_chunks(d, h, bytes, DG_HOST_CHUNK); }
static inline dma_addr_t dg_host_dma(struct dg_host_page *h, u64 off)
{ return dg_host_dma_chunk(h, off, DG_HOST_CHUNK); }
static inline u64 *dg_host_word(struct dg_host_page *h, u64 word)
{ return dg_host_word_chunk(h, word, DG_HOST_CHUNK); }
#endif
