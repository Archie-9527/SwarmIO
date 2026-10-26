/* Diagnostic read/write-only SQ validation; not a production NVMe validator. */
#ifndef SWARMIO_SQ_DIAGNOSTICS_H
#define SWARMIO_SQ_DIAGNOSTICS_H
#include <linux/io.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/printk.h>

/* 只读故障锁存用于定位非法 SQE，不伪造成功完成；该诊断仅接受读写命令。 */
static bool sq_diag_faulted;
static bool sq_cpu_fromio = true;
static bool sq_diag_seen[NR_MAX_IO_QUEUE + 1];
#ifdef CONFIG_SWARMIO_SQ_DIAGNOSTICS
module_param(sq_diag_faulted, bool, 0444);
MODULE_PARM_DESC(sq_diag_faulted, "Diagnostic fault latched; unload/reload before another test");
#endif
/* GPU BAR 映射必须按 I/O 内存读取；此开关仅用于对照实验。 */
module_param(sq_cpu_fromio, bool, 0644);
MODULE_PARM_DESC(sq_cpu_fromio, "Use memcpy_fromio for non-RAM SQ mappings (default Y)");
#ifdef CONFIG_SWARMIO_SQ_DIAGNOSTICS
MODULE_INFO(swarmio_sq_diag, "1");
#endif

#ifdef CONFIG_SWARMIO_SQ_DIAGNOSTICS
static void sq_diag_dump(struct nvme_submission_queue *sq,
                         const struct nvme_command *cmd, int entry,
                         const char *reason)
{
    SWARMIO_ERROR("SQDIAG %s qid=%d entry=%d depth=%d sq_dma=%#llx mapped=%d fromio=%d nsid=%u opcode=%#x cid=%u slba=%llu nlb=%u prp1=%#llx\n",
        reason, sq->qid, entry, sq->queue_size,
        (unsigned long long)sq->dma_addr, sq->mapped != NULL,
        READ_ONCE(sq_cpu_fromio), le32_to_cpu(cmd->common.nsid),
        cmd->common.opcode, le16_to_cpu(cmd->common.command_id),
        (unsigned long long)le64_to_cpu(cmd->rw.slba),
        (unsigned int)le16_to_cpu(cmd->rw.length) + 1,
        (unsigned long long)le64_to_cpu(cmd->rw.dptr.prp1));
    print_hex_dump(KERN_ERR, "SwarmIO SQDIAG raw: ", DUMP_PREFIX_NONE,
                   16, 4, cmd, sizeof(*cmd), false);
}

static bool sq_diag_valid(struct nvme_submission_queue *sq,
                          const struct nvme_command *cmd, int entry)
{
    u32 nsid = le32_to_cpu(cmd->common.nsid);
    u64 start, blocks, capacity;
    const char *reason = NULL;

    if (READ_ONCE(sq_diag_faulted))
        return false;
    if (!nsid || nsid > vdev->num_ns)
        reason = "INVALID_NSID";
    else if (cmd->common.opcode != nvme_cmd_read && cmd->common.opcode != nvme_cmd_write)
        reason = "UNSUPPORTED_OPCODE";
    else {
        start = le64_to_cpu(cmd->rw.slba);
        blocks = (u64)le16_to_cpu(cmd->rw.length) + 1;
        capacity = vdev->ns[nsid - 1].size >> LBA_BITS;
        if (start >= capacity || blocks > capacity - start)
            reason = "LBA_OUT_OF_RANGE";
        else if (!le64_to_cpu(cmd->rw.dptr.prp1))
            reason = "ZERO_PRP1";
    }
    if (reason) {
        WRITE_ONCE(sq_diag_faulted, true);
        sq_diag_dump(sq, cmd, entry, reason);
        SWARMIO_ERROR("SQDIAG submission processing stopped; no success completion was fabricated; preserve logs then end the workload\n");
        return false;
    }
    if (sq->qid > 0 && sq->qid <= NR_MAX_IO_QUEUE && !sq_diag_seen[sq->qid]) {
        sq_diag_seen[sq->qid] = true;
        sq_diag_dump(sq, cmd, entry, "FIRST_VALID");
    }
    return true;
}

static bool sq_diag_batch(struct nvme_submission_queue *sq,
                          const struct nvme_command *cmds, int first, int count)
{
    int i;
    for (i = 0; i < count; ++i)
        if (!sq_diag_valid(sq, &cmds[i], first + i))
            return false;
    return true;
}

#else
static inline bool sq_diag_valid(struct nvme_submission_queue *sq,
                                 const struct nvme_command *cmd, int entry)
{ return true; }
static inline bool sq_diag_batch(struct nvme_submission_queue *sq,
                                 const struct nvme_command *cmd, int first, int count)
{ return true; }
#endif

/* 逐页解析 SQ：普通 RAM 页不保证物理连续，GPU BAR 则使用 memcpy_fromio。 */
static void sq_diag_copy(struct nvme_submission_queue *sq, void *destination,
                         int first, int count)
{
    char *dst = destination;
    while (count > 0) {
        int chunk = min(count, (int)NR_SQE_PER_PAGE - first % (int)NR_SQE_PER_PAGE);
        size_t bytes = chunk * sizeof(struct nvme_command);
        const void __iomem *src = __sq_entry_at(sq, first);
        if (sq->mapped && READ_ONCE(sq_cpu_fromio))
            memcpy_fromio(dst, src, bytes);
        else
            memcpy(dst, (const void __force *)src, bytes);
        first += chunk;
        count -= chunk;
        dst += bytes;
    }
}
#endif
