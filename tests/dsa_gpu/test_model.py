"""无硬件检查：编译真实共用头，并检查地址置换、窗口/尾批边界与数据模型。
不替代 nvcc、Linux 模块编译和 DSA/GPU 硬件验证。
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
source = r'''
#include <cassert>
#include <cstdint>
#include <vector>
#include <algorithm>
#include "uapi.h"
int main() {
    static_assert(sizeof(dg_cmd) == 64, "SQE ABI");
    static_assert(sizeof(dg_run) == 104, "ioctl ABI");
    for (unsigned bytes : {512U,4096U})
    for (unsigned seed : {0U,1U,12345U,0xffffffffU})
    for (unsigned direction : {0U,1U,2U})
    for (unsigned ops : {1U,17U,33U,127U,128U,129U,1025U}) {
        uint64_t blocks = 65536 / bytes;
        std::vector<bool> seen(blocks,false), touched(blocks,false);
        for (uint64_t i=0;i<blocks;i++) {
            auto b=dg_block(i,blocks,seed); assert(b<blocks && !seen[b]); seen[b]=true;
        }
        std::vector<uint64_t> host(65536/8), gpu(host.size());
        for (uint64_t i=0;i<host.size();i++) {
            bool w=dg_write(i*8/bytes,direction);
            host[i]=dg_pattern(i) ^ (w ? UINT64_MAX:0);
            gpu[i]=dg_pattern(i) ^ (w ? 0:UINT64_MAX);
        }
        for (uint64_t pos=0,n;pos<ops;pos+=n) {
            n=std::min<uint64_t>({DG_WINDOW,blocks,ops-pos});
            // SQE 窗口不跨 64KiB GPU 页；窗口内地址不重复。
            assert((pos*64)%65536+n*64<=65536);
            std::fill(seen.begin(),seen.end(),false);
            for (uint64_t j=0;j<n;j++) {
                auto b=dg_block(pos+j,blocks,seed); assert(!seen[b]); seen[b]=true;
                touched[b]=true;
                assert((b*bytes)%65536+bytes<=65536);
                for (uint64_t w=b*bytes/8;w<(b+1)*bytes/8;w++)
                    if(dg_write(b,direction)) host[w]=gpu[w]; else gpu[w]=host[w];
            }
            for (unsigned batch=2;batch<=32;batch++) {
                unsigned completed=0;
                for(uint64_t j=0;j<n;) {
                    auto actual=std::min<uint64_t>(batch,n-j);
                    auto hardware=actual==1?2:actual;
                    assert(hardware>=2 && hardware<=32);
                    completed+=actual; j+=actual;
                }
                assert(completed==n);
            }
        }
        for(uint64_t i=0;i<host.size();i++) {
            auto b=i*8/bytes;
            auto p=dg_pattern(i);
            assert(host[i]==(p ^ ((dg_write(b,direction)&&!touched[b])?UINT64_MAX:0)));
            assert(gpu[i]==(p ^ ((!dg_write(b,direction)&&!touched[b])?UINT64_MAX:0)));
        }
    }
}
'''

with tempfile.TemporaryDirectory(prefix="dsa-gpu-model-") as tmp:
    p = Path(tmp)
    # macOS 无 Linux UAPI 头；这里只提供固定宽度类型和 ioctl 宏替身。
    # 被测试的结构体、地址生成、方向及数据模式全部来自真实 uapi.h。
    (p / "linux").mkdir()
    (p / "linux/types.h").write_text(
        '#include <stdint.h>\ntypedef uint64_t __u64; typedef uint32_t __u32;\n')
    (p / "linux/ioctl.h").write_text('#define _IOWR(a,b,c) 0\n')
    (p / "test.cpp").write_text(source)
    compiler = os.environ.get("CXX") or shutil.which("c++")
    if not compiler:
        raise SystemExit("需要 C++ 编译器")
    subprocess.run([compiler, "-std=c++14", "-Wall", "-Wextra", "-Werror",
                    "-I", str(p), "-I", str(HERE), str(p / "test.cpp"),
                    "-o", str(p / "test")], check=True)
    subprocess.run([str(p / "test")], check=True)
print("PASS: shared ABI, permutation, SQE/page boundaries, tail batches, bidirectional data model")
