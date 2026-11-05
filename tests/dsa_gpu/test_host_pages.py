"""编译真实host_pages.h，模拟DMA分配并检查地址计算及部分失败回收。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
HERE = Path(__file__).resolve().parent
C = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
struct device { int unused; };
#define GFP_KERNEL 0
#define pr_err(...) fprintf(stderr, __VA_ARGS__)
static unsigned calls, live, fail_at;
static void *kvcalloc(size_t n, size_t size, int flags) {
    (void)flags; return calloc(n, size);
}
static void kvfree(void *p) { free(p); }
static void *dma_alloc_coherent(struct device *dev, size_t bytes,
                                dma_addr_t *dma, int flags) {
    (void)dev; (void)flags;
    assert(bytes == 4096);
    calls++;
    if (calls == fail_at) return NULL;
    void *p = calloc(1, bytes);
    if (p) { live++; *dma = 0x100000000ULL + calls * 0x3000ULL; }
    return p;
}
static void dma_free_coherent(struct device *dev, size_t bytes,
                              void *cpu, dma_addr_t dma) {
    (void)dev; (void)dma;
    assert(bytes == 4096 && live); live--; free(cpu);
}
#include "host_pages.h"
int main(void) {
    struct device dev = {0};
    const u64 sizes[] = {65536, 2ULL<<20, 32ULL<<20};
    for (unsigned s = 0; s < sizeof(sizes)/sizeof(sizes[0]); s++) {
        u64 bytes = sizes[s];
        calls = live = fail_at = 0;
        struct dg_host_page *host = dg_host_alloc(&dev, bytes);
        assert(host && calls == bytes / 4096);
        for (u64 i = 0; i < bytes/8; i++) *dg_host_word(host, i) = i ^ 0xabcd1234ULL;
        for (u64 i = 0; i < bytes/8; i++) assert(*dg_host_word(host, i) == (i ^ 0xabcd1234ULL));
        for (u64 block = 512; block <= 4096; block *= 8)
            for (u64 off = 0; off < bytes; off += block) {
                assert(dg_host_dma(host, off) == host[off/4096].dma + off%4096);
                assert(off%4096 + block <= 4096);
                if (off && off%4096 == 0)
                    assert(dg_host_dma(host, off) != dg_host_dma(host, off-4096) + 4096);
            }
        dg_host_free(&dev, host, bytes);
        assert(!live);
    }
    for (unsigned failure = 1; failure <= 16; failure++) {
        calls = live = 0; fail_at = failure;
        assert(!dg_host_alloc(&dev, 65536));
        assert(!live);
    }
    assert(!dg_host_alloc(&dev, 0));
    assert(!dg_host_alloc(&dev, 4097));
    dg_host_free(&dev, NULL, 65536);
}
'''

with tempfile.TemporaryDirectory(prefix="dg-host-pages-") as tmp:
    p = Path(tmp)
    (p / "linux").mkdir()
    for name in ("dma-mapping.h", "slab.h", "vmalloc.h"):
        (p / "linux" / name).write_text("")
    (p / "host_pages.h").write_text((HERE / "host_pages.h").read_text())
    (p / "check.c").write_text(C)
    cc = shutil.which("cc")
    if not cc:
        raise SystemExit("需要C编译器")
    subprocess.run([cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-I", str(p), str(p / "check.c"), "-o", str(p / "check")], check=True)
    done = subprocess.run([str(p / "check")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stderr.count("allocation failed") == 16
print("PASS: real host_pages.h C compilation, discontiguous DMA addresses, 32MiB data, partial allocation cleanup")
