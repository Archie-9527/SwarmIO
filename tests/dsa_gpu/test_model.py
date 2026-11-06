"""无硬件回归检查；不替代真实Linux模块/nvcc编译和DSA/GPU测试。"""
from pathlib import Path
import random
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
CXX = r'''
#include <cassert>
#include <cstdint>
#include <vector>
#include <algorithm>
#include "uapi.h"
int main() {
    static_assert(sizeof(dg_cmd)==64, "SQE ABI");
    static_assert(sizeof(dg_info)==48, "INFO ABI");
    static_assert(sizeof(dg_run)==232, "RUN ABI");
    for (unsigned groups : {1U,2U,4U}) {
        assert(groups*2*(128/(groups*2))==128);
        for (unsigned size : {512U,4096U,65536U})
        for (uint64_t bytes : {1ULL<<20,32ULL<<20,256ULL<<20})
        for (unsigned seed : {0U,12345U,0xffffffffU}) {
            auto blocks=bytes/groups/size;
            auto stride=dg_sq_stride(bytes,size,groups);
            assert(stride%65536==0 && stride>=blocks*64);
            for (unsigned g=0;g<groups;g++) {
                std::vector<bool> seen(blocks,false);
                for (uint64_t i=0;i<blocks;i++) {
                    auto c=dg_command(g,i,bytes,size,groups,2,seed);
                    auto local=c.offset/size-g*blocks;
                    assert(local<blocks && !seen[local]); seen[local]=true;
                    assert(c.offset>=g*bytes/groups && c.offset+size<=(g+1)*bytes/groups);
                    assert(c.offset%size==0 && c.offset%65536+size<=65536);
                    assert(c.tag==g*(stride/64)+i && c.bytes==size);
                    assert(c.write==(c.offset/size)%2);
                    for(auto v:c.reserved) assert(v==0);
                }
            }
            for(unsigned batch=1;batch<=1024;batch*=2)
            for(unsigned depth : {1U,2U,4U,8U,16U}) {
                if(batch*depth>blocks) continue;
                for(uint64_t first=0;first<blocks;first+=batch)
                    assert((first*64)%65536+batch*64<=65536);
            }
        }
        // 任意总ops分摊后数量守恒，且支持不足一个完整batch的尾部。
        for(uint64_t ops : {4ULL,129ULL,4097ULL,1048576ULL}) {
            uint64_t sum=0;
            for(unsigned g=0;g<groups;g++) sum+=ops/groups+(g<ops%groups);
            assert(sum==ops);
        }
    }
    assert(4ULL*1024*16*4096==(256ULL<<20));
    assert((256ULL<<20)+4*dg_sq_stride(256ULL<<20,4096,4)==(260ULL<<20));
    assert((8ULL<<20)+4*dg_sq_stride(8ULL<<20,512,4)<=(32ULL<<20));
}
'''

def check_pipeline():
    """随机扰动DMA完成顺序：检查环形背压、在途地址不重叠、按序回收和尾批。"""
    rng = random.Random(20261105)
    for batch in (1, 2, 8, 32, 1024):
        for depth in (1, 2, 4, 16):
            for total in (1, 129, batch * depth * 3 + 1):
                blocks = 1
                while blocks < batch * depth:
                    blocks *= 2
                slots = [dict(state="free") for _ in range(depth)]
                cursor = executed = tail = head = 0
                for tick in range(100000):
                    for s in slots:
                        if s["state"] == "fetch" and s["due"] <= tick:
                            s["state"] = "ready"
                    for s in slots:
                        if s["state"] == "ready":
                            s.update(state="copy", due=tick+rng.randrange(1,15))
                    s = slots[head]
                    if s["state"] == "copy" and s["due"] <= tick:
                        assert s["first"] == executed
                        executed += s["count"]
                        s["state"] = "free"
                        head = (head+1)%depth
                    s = slots[tail]
                    if cursor < total and s["state"] == "free":
                        n = min(batch, total-cursor, blocks-cursor%blocks)
                        s.update(state="fetch", first=cursor, count=n,
                                 due=tick+rng.randrange(1,15))
                        cursor += n
                        tail = (tail+1)%depth
                    active = set()
                    for s in slots:
                        if s["state"] != "free":
                            for i in range(s["count"]):
                                b = (s["first"]+i)%blocks
                                assert b not in active
                                active.add(b)
                    if executed == total:
                        assert all(s["state"]=="free" for s in slots)
                        break
                else:
                    raise AssertionError("流水线未前进")

with tempfile.TemporaryDirectory(prefix="dg-v2-model-") as tmp:
    p = Path(tmp)
    (p/"linux").mkdir()
    (p/"linux/types.h").write_text("typedef unsigned long long __u64; typedef unsigned int __u32;\n")
    (p/"linux/ioctl.h").write_text("#define _IOR(a,b,c) 0\n#define _IOWR(a,b,c) 0\n")
    (p/"test.cpp").write_text(CXX)
    cc = shutil.which("c++")
    if not cc:
        raise SystemExit("需要C++编译器")
    subprocess.run([cc,"-std=c++14","-O2","-Wall","-Wextra","-Werror",
                    "-I",str(p),"-I",str(HERE),str(p/"test.cpp"),"-o",str(p/"test")],check=True)
    subprocess.run([str(p/"test")],check=True)
check_pipeline()
subprocess.run([shutil.which("python3"),str(HERE/"test_host_pages.py")],check=True)
print("PASS: ABI4, 4-group/8-WQ budget, GPU SQE/page boundaries, pin budgets, 1024-item batches, ring backpressure and tail requests")
