"""Execute real NVMe create/delete handlers at the 512-QP boundary."""
from pathlib import Path
import unittest
from test_swarmio_dsa_c import compile_run

ROOT=Path(__file__).resolve().parents[1]


class QueueBoundaryTests(unittest.TestCase):
    def test_all_512_queues_and_out_of_range_commands(self):
        source=(ROOT/'nvmev/admin.c').read_text()
        functions=source[source.index('void vdev_delete_sq('):source.index('static void admin_get_log_page(')]
        pre=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define NR_MAX_IO_QUEUE 512
#define NR_MAX_QUEUE_SIZE 1024
#define VDEV_PCI_MSIX_TS 128
#define CONFIG_SWARMIO_DISP_COALESCE_SQE 1
#define PAGE_SIZE 4096
#define PAGE_SHIFT 12
#define GFP_KERNEL 0
#define MEMREMAP_WB 0
#define NVME_CQ_IRQ_ENABLED 2
#define NVME_QUEUE_PHYS_CONTIG 1
#define NVME_SC_SUCCESS 0
#define NVME_SC_INVALID_FIELD 2
#define NVME_SC_INTERNAL 6
#define NVME_STATUS_DNR 0x4000
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define unlikely(x) (x)
#define WARN_ON(x) assert(!(x))
#define SWARMIO_ERROR(...) ((void)0)
#define spin_lock_init(p) (*(p)=0)
#define mutex_init(p) (*(p)=0)
#define kzalloc(n,f) calloc(1,n)
#define kfree free
#define memunmap free
#define iounmap free
struct nvme_completion { unsigned char bytes[16]; };
struct nvme_create_cq { uint16_t cqid,qsize,cq_flags,irq_vector; uint64_t prp1; };
struct nvme_create_sq { uint16_t sqid,qsize,sq_flags,cqid; uint64_t prp1; };
struct nvme_delete_queue { uint16_t qid; };
struct nvme_command { union { struct nvme_create_cq create_cq; struct nvme_create_sq create_sq; struct nvme_delete_queue delete_queue; unsigned char bytes[64]; }; };
struct nvme_admin_queue { struct nvme_command commands[1]; };
struct nvme_submission_queue {
    int qid,cqid,queue_size,sq_head,fetched_pos;
    void *mapped; struct nvme_command **sq,*cmd_buffer;
    uint64_t *nsecs_targets,cmd_buffer_dma,dma_addr;
};
struct nvme_completion_queue {
    int qid,irq_vector,queue_size,phase,cq_head,cq_tail,entry_lock,irq_lock;
    bool irq_enabled,interrupt_ready,phys_contig;
    void *mapped; struct nvme_completion **cq;
};
static struct {
    struct nvme_admin_queue *admin_q;
    unsigned num_sq,num_cq;
    struct { uint64_t cmd_buffer_start; } config;
    struct nvme_submission_queue *sqes[513];
    struct nvme_completion_queue *cqes[513];
    uint32_t dbs[1026],old_dbs[1026];
} device,*vdev=&device;
#define __admin_sq_entry_at(q,e) (&(q)->commands[e])
static unsigned status,allocations;
static void __make_cq_entry(int eid,unsigned s) { assert(eid==0); status=s; }
static bool pfn_valid(uint64_t p) { return false; }
static void *prp_address_offset(uint64_t a,unsigned i) { assert(0); return NULL; }
static void *ioremap(uint64_t a,size_t n) { allocations++; return calloc(1,n); }
static void *memremap(uint64_t a,size_t n,int f) {
    assert(n==65536 && a>=0x100000 && a<0x100000+512*65536ULL);
    allocations++; return calloc(1,n);
}
'''
        checks=r'''
int main(void) {
    _Static_assert(sizeof(struct nvme_command)==64,"SQE size");
    struct nvme_admin_queue admin={0}; device.admin_q=&admin;
    device.num_sq=device.num_cq=512; device.config.cmd_buffer_start=0x100000;
    for(unsigned i=0;i<1026;i++) device.dbs[i]=device.old_dbs[i]=0xdeadbeef;
    for(unsigned q=1;q<=512;q++) {
        admin.commands[0].create_cq=(struct nvme_create_cq){q,1023,1,65535,0x100000000ULL+q*65536};
        admin_create_cq(0); assert(status==0 && device.cqes[q] && !device.cqes[q]->irq_enabled);
        admin.commands[0].create_sq=(struct nvme_create_sq){q,1023,1,q,0x200000000ULL+q*65536};
        admin_create_sq(0); assert(status==0 && device.sqes[q]);
        assert(device.sqes[q]->cmd_buffer_dma==0x100000+(q-1)*65536ULL);
        assert(!device.dbs[2*q] && !device.dbs[2*q+1] && !device.old_dbs[2*q+1]);
    }
    assert(allocations==1536 && device.dbs[0]==0xdeadbeef);
    unsigned before=allocations;
    unsigned bad[]={0,513,65535};
    for(unsigned i=0;i<3;i++) {
        unsigned q=bad[i];
        admin.commands[0].create_cq=(struct nvme_create_cq){q,1023,1,0,0};
        admin_create_cq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
        admin.commands[0].create_sq=(struct nvme_create_sq){q,1023,1,1,0};
        admin_create_sq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
        admin.commands[0].delete_queue.qid=q;
        admin_delete_sq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
        admin_delete_cq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
    }
    assert(before==allocations);
    for(unsigned q=1;q<=512;q++) {
        admin.commands[0].delete_queue.qid=q;
        admin_delete_sq(0); admin_delete_cq(0);
        assert(!device.sqes[q] && !device.cqes[q]);
    }
    admin.commands[0].create_cq=(struct nvme_create_cq){1,1024,1,0,0};
    admin_create_cq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
    admin.commands[0].create_cq=(struct nvme_create_cq){1,1023,3,128,0};
    admin_create_cq(0); assert(status==(NVME_SC_INVALID_FIELD|NVME_STATUS_DNR));
    assert(before==allocations);
    return 0;
}
'''
        compile_run(pre+functions+checks)


if __name__=='__main__': unittest.main()
