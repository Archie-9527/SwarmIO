"""无Linux构建环境的可选C语法检查；接口替身不验证真实内核ABI/符号。"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def write_header_stub(path, name, platform=None):
    """Do not replace the error definitions needed by glibc's errno.h.

    Linux libc may include <linux/errno.h> while mock.h is still being parsed.
    Forward that include to the real UAPI header, rather than re-entering the
    mock's include guard. macOS has no Linux UAPI, so use its native errno.h.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if name == 'errno.h':
        text = '#include_next <linux/errno.h>\n' if (platform or sys.platform).startswith('linux') else '#include <errno.h>\n'
    else:
        text = '#include "mock.h"\n'
    path.write_text(text)


MOCK = r'''
#ifndef DG_MOCK_H
#define DG_MOCK_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
typedef unsigned long long u64, __u64, dma_addr_t;
typedef unsigned int u32, __u32;
typedef unsigned char u8;
#define __user
#define __init
#define __exit
#define ____cacheline_aligned_in_smp
#define THIS_MODULE ((void *)0)
#define DEFINE_MUTEX(x) int x
#define module_param(...)
#define module_param_named(...)
#define module_param_array(...)
#define MODULE_PARM_DESC(...)
#define MODULE_LICENSE(...)
#define MODULE_VERSION(...)
#define MODULE_IMPORT_NS(...)
#define MODULE_DESCRIPTION(...)
#define module_init(...)
#define module_exit(...)
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define LINUX_VERSION_CODE KERNEL_VERSION(6,8,0)
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define smp_load_acquire(p) (*(p))
#define smp_store_release(p,v) (*(p)=(v))
#define dma_rmb() ((void)0)
#define dma_wmb() ((void)0)
#define smp_mb() ((void)0)
#define min(a,b) ((a)<(b)?(a):(b))
#define max(a,b) ((a)>(b)?(a):(b))
#define min_t(t,a,b) min((t)(a),(t)(b))
#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))
#define U64_MAX (~0ULL)
#define HZ 1000
#define GFP_KERNEL 0
#define CAP_SYS_RAWIO 1
#define TAINT_DIE 7
#define MISC_DYNAMIC_MINOR 1
#define IS_ERR(p) ((uintptr_t)(p)>(uintptr_t)-4096)
#define PTR_ERR(p) ((long)(p))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define confdev_to_idxd_dev(dev) container_of(dev,struct idxd_dev,conf_dev)
#define idxd_dev_to_idxd(idxd_dev) container_of(idxd_dev,struct idxd_device,idxd_dev)
#define pr_err(...) ((void)0)
#define pr_info(...) ((void)0)
#define pr_emerg(...) ((void)0)
#define time_after(a,b) ((long)((b)-(a))<0)
#define is_power_of_2(x) ((x)&&!((x)&((x)-1)))
#define _IOR(a,b,c) ((unsigned)sizeof(c)+2)
#define _IOWR(a,b,c) ((unsigned)sizeof(c)+3)
#define NVIDIA_P2P_CAP_GET_PAGES_PERSISTENT_API
#define NVIDIA_P2P_PAGE_SIZE_64KB 1
#define NVIDIA_P2P_PAGE_TABLE_VERSION_COMPATIBLE(p) ((p)->version!=0)
#define NVIDIA_P2P_DMA_MAPPING_VERSION_COMPATIBLE(p) ((p)->version!=0)
#define DSA_SCAN_MAX_DEVS 16
#define DSA_MAX_DEVS 4
#define DSA_MAX_WQS 16
#define DSA_COMP_NONE 0
#define DSA_COMP_SUCCESS 1
#define DSA_COMP_STATUS(x) ((x)&0x7f)
#define DSA_OPCODE_MEMMOVE 3
#define DSA_OPCODE_BATCH 1
#define DSA_OPCODE_DRAIN 2
#define DSA_OPCODE_NOOP 0
#define IDXD_OP_FLAG_CRAV 4
#define IDXD_OP_FLAG_RCR 8
#define IDXD_OP_NONBLOCK 0
#define IDXD_WQ_DISABLED 0
#define IDXD_WQ_ENABLED 1
#define IDXD_DEV_ENABLED 1
#define IDXD_WQT_KERNEL 1
#define DMA_MEMCPY 1
typedef unsigned long dma_cap_mask_t;
#define dma_cap_zero(mask) ((mask)=0)
#define dma_cap_set(cap,mask) ((mask)|=(cap))
struct device_driver { const char *name; };
struct device { struct device_driver *driver; };
struct pci_dev { struct device dev; };
struct idxd_dev { struct device conf_dev; };
struct idxd_group { int rdbufs_allowed,rdbufs_reserved; };
struct idxd_engine { struct idxd_group *group; };
struct idxd_device {
    struct idxd_dev idxd_dev; struct pci_dev *pdev;
    struct idxd_wq **wqs; struct idxd_group **groups; struct idxd_engine **engines;
    int id,state,max_wqs,max_groups,max_engines,max_wq_size;
    u32 max_batch_size; u64 max_xfer_bytes;
};
struct dma_chan { int unused; };
struct idxd_dma_chan { struct dma_chan chan; };
struct idxd_wq {
    int id,state,type,client_count; u32 size,max_batch_size; u64 max_xfer_bytes;
    struct idxd_group *group; struct idxd_dev idxd_dev;
    struct idxd_dma_chan *idxd_chan; void *portal,*descs; char name[128];
};
#define wq_confdev(wq) ((wq)->idxd_dev.conf_dev)
#define wq_dedicated(wq) ((wq)->type==IDXD_WQT_KERNEL)
struct dsa_hw_desc { u32 pasid,opcode,flags; u64 src_addr,dst_addr,completion_addr; u32 xfer_size; };
struct dsa_completion_record { u8 status; };
struct idxd_desc { struct dsa_hw_desc *hw; struct dsa_completion_record *completion; struct idxd_wq *wq; dma_addr_t compl_dma; };
struct dsa_cfg_params { unsigned int num_devs,num_groups_per_dev,num_wqs_per_group,num_engines_per_group; const unsigned int *wq_sizes,*wq_priorities; };
typedef struct { u32 version,page_size,entries; } nvidia_p2p_page_table_t;
typedef struct { u32 version,page_size_type,entries; u64 *dma_addresses; } nvidia_p2p_dma_mapping_t;
typedef struct { int counter; } atomic_t;
#define ATOMIC_INIT(n) {n}
struct completion { int unused; };
struct task_struct { int unused; };
struct file { int unused; };
struct file_operations { void *owner; long (*unlocked_ioctl)(struct file *,unsigned int,unsigned long); };
struct miscdevice { int minor; const char *name; const struct file_operations *fops; unsigned int mode; };
extern unsigned long jiffies;
extern unsigned int nr_cpu_ids;
extern int dsa_bus_type;
void *kvcalloc(size_t,size_t,int); void kvfree(void *);
void *kcalloc(size_t,size_t,int); void kfree(void *);
void *dma_alloc_coherent(struct device *,size_t,dma_addr_t *,int);
void dma_free_coherent(struct device *,size_t,void *,dma_addr_t);
unsigned long *bitmap_zalloc(unsigned int,int); void bitmap_free(void *);
void __set_bit(unsigned long,unsigned long *); bool test_bit(unsigned long,const unsigned long *);
int atomic_read(const atomic_t *); void atomic_set(atomic_t *,int); int atomic_cmpxchg(atomic_t *,int,int);
int atomic_inc_return(atomic_t *); void atomic_dec(atomic_t *);
int nvidia_p2p_dma_unmap_pages(struct pci_dev *,nvidia_p2p_page_table_t *,nvidia_p2p_dma_mapping_t *);
int nvidia_p2p_put_pages_persistent(u64,nvidia_p2p_page_table_t *,u32);
int nvidia_p2p_get_pages_persistent(u64,u64,nvidia_p2p_page_table_t **,u32);
int nvidia_p2p_dma_map_pages(struct pci_dev *,nvidia_p2p_page_table_t *,nvidia_p2p_dma_mapping_t **);
struct idxd_desc *idxd_alloc_desc(struct idxd_wq *,int);
void idxd_free_desc(struct idxd_wq *,struct idxd_desc *);
int idxd_submit_desc(struct idxd_wq *,struct idxd_desc *);
void cpu_relax(void); int cond_resched(void); long schedule_timeout_uninterruptible(long);
void usleep_range(unsigned long,unsigned long); int test_taint(unsigned int);
u64 ktime_get_ns(void);
void complete(struct completion *); void complete_all(struct completion *);
void init_completion(struct completion *); void wait_for_completion(struct completion *);
int wait_for_completion_interruptible(struct completion *);
int dev_to_node(struct device *); unsigned int cpumask_local_spread(unsigned int,int);
struct task_struct *kthread_create_on_node(int (*)(void *),void *,int,const char *,...);
void get_task_struct(struct task_struct *); void put_task_struct(struct task_struct *);
void kthread_bind(struct task_struct *,unsigned int); int wake_up_process(struct task_struct *);
bool capable(int); unsigned long copy_to_user(void *,const void *,unsigned long);
unsigned long copy_from_user(void *,const void *,unsigned long);
bool mutex_trylock(int *); void mutex_unlock(int *);
unsigned int num_online_cpus(void); bool cpu_online(unsigned int);
int snprintf(char *,size_t,const char *,...);
struct device *bus_find_device_by_name(const int *,struct device *,const char *);
struct dma_chan *dma_request_channel(dma_cap_mask_t,bool (*)(struct dma_chan *,void *),void *);
void dma_release_channel(struct dma_chan *);
void __module_get(void *);
void put_device(struct device *);
int dsa_set_params(const struct dsa_cfg_params *); int dsa_enable_devices(void); void dsa_disable_devices(void);
int misc_register(struct miscdevice *); void misc_deregister(struct miscdevice *);
#endif
'''
def check():
    with tempfile.TemporaryDirectory(prefix="dg-kernel-syntax-") as tmp:
        p=Path(tmp)
        headers=("module.h miscdevice.h fs.h uaccess.h mutex.h delay.h ktime.h capability.h log2.h "
             "kthread.h completion.h cpu.h sched.h sched/task.h types.h ioctl.h dma-mapping.h slab.h vmalloc.h device.h dmaengine.h panic.h errno.h string.h")
        for name in headers.split():
            dest=p/"linux"/name
            write_header_stub(dest, name)
        (p/"mock.h").write_text(MOCK)
        (p/"dsa.h").write_text('#include "mock.h"\n')
        (p/"nv-p2p.h").write_text('#include "mock.h"\n')
        cc=shutil.which("cc")
        for source in ('kernel.c','backend.c'):
            subprocess.run([cc,"-std=gnu11","-fsyntax-only","-Wall","-Wextra","-Werror",
                            "-Wno-unused-parameter","-Wno-unused-function","-Wno-unused-variable",
                            "-I",str(p),"-I",str(HERE),str(HERE/source)],check=True)
    print("PASS: kernel.c/backend.c C syntax with interface stubs; not a real Linux module build")

if __name__ == "__main__":
    check()
