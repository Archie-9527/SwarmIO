/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DG_GPU_REGION_H
#define DG_GPU_REGION_H
#include <linux/errno.h>
#include <linux/string.h>
#include <nv-p2p.h>

/* 只有API返回成功才能取得所有权。失败API写出的非NULL指针不是有效句柄。
 * 该状态与指针分别记录，防止失败get之后误调用put而进入驱动无效对象。
 */
struct dg_region {
    u64 va;
    nvidia_p2p_page_table_t *pages;
    nvidia_p2p_dma_mapping_t *map;
    bool pages_owned, map_owned;
};
static inline int dg_region_pin(struct pci_dev *pdev, struct dg_region *r,
                               u64 va, u64 bytes)
{
    nvidia_p2p_page_table_t *pages = NULL;
    nvidia_p2p_dma_mapping_t *map = NULL;
    int ret;
    if (r->pages_owned || r->map_owned) return -EBUSY;
    memset(r, 0, sizeof(*r));
    r->va = va;
    ret = nvidia_p2p_get_pages_persistent(va, bytes, &pages, 0);
    if (ret) return ret; /* 不保存/释放失败输出，驱动负责失败调用的内部清理。 */
    if (!pages) return -EIO;
    r->pages = pages; r->pages_owned = true;
    if (!NVIDIA_P2P_PAGE_TABLE_VERSION_COMPATIBLE(pages) ||
        pages->page_size != NVIDIA_P2P_PAGE_SIZE_64KB)
        return -EOPNOTSUPP;
    ret = nvidia_p2p_dma_map_pages(pdev, pages, &map);
    if (ret) return ret; /* pages仍归本模块，但失败map输出不归本模块。 */
    if (!map) return -EIO;
    r->map = map; r->map_owned = true;
    if (!NVIDIA_P2P_DMA_MAPPING_VERSION_COMPATIBLE(map) ||
        map->page_size_type != NVIDIA_P2P_PAGE_SIZE_64KB ||
        map->entries < bytes / 65536 || !map->dma_addresses)
        return -EOPNOTSUPP;
    return 0;
}
static inline int dg_region_unpin(struct pci_dev *pdev, struct dg_region *r)
{
    int ret;
    if (r->map_owned) {
        ret = nvidia_p2p_dma_unmap_pages(pdev, r->pages, r->map);
        if (ret) return ret; /* 不再继续put，保留所有权供上层隔离。 */
        r->map_owned = false; r->map = NULL;
    }
    if (r->pages_owned) {
        ret = nvidia_p2p_put_pages_persistent(r->va, r->pages, 0);
        if (ret) return ret;
        r->pages_owned = false; r->pages = NULL;
    }
    memset(r, 0, sizeof(*r));
    return 0;
}
#endif
