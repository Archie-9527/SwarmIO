// Finite BaM read baseline: exactly one NVMe READ per logical operation.
#include <cuda_runtime.h>
#include <ctrl.h>
#include <page_cache.h>
#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>
#include "../src/swarmio_pattern.h"

// Legacy helpers log CUDA errors without necessarily stopping. The test entry
// must never print a successful throughput result after a failed CUDA call.
#undef cuda_err_chk
#define cuda_err_chk(call) do { auto e=(call); if(e!=cudaSuccess) throw error(cudaGetErrorString(e)); } while(0)

struct Config {
    uint64_t threads=64, reqs=1, pages=4096, page_size=512, num_blks=131072;
    uint64_t queue_depth=512, num_queues=1;
    int gpu=0;
    bool random=false, verify=false;
};
static Config parse(int argc, char **argv) {
    Config c;
    for (int i=1; i<argc; ++i) {
        std::string a=argv[i]; auto eq=a.find('=');
        if (eq==std::string::npos) throw error("expected --name=value");
        auto k=a.substr(0,eq), s=a.substr(eq+1);
        if (k=="--random" || k=="--verify") {
            if (s!="true" && s!="false") throw error("invalid boolean");
            (k=="--random" ? c.random : c.verify)=s=="true"; continue;
        }
        size_t used=0;
        if (s.empty() || s[0]=='-') throw error("invalid unsigned option");
        auto v=std::stoull(s,&used);
        if (used!=s.size()) throw error("invalid numeric suffix");
        if (k=="--threads") c.threads=v;
        else if (k=="--reqs") c.reqs=v;
        else if (k=="--pages") c.pages=v;
        else if (k=="--page_size") c.page_size=v;
        else if (k=="--num_blks") c.num_blks=v;
        else if (k=="--queue_depth") c.queue_depth=v;
        else if (k=="--num_queues") c.num_queues=v;
        else if (k=="--gpu" && v<=1024) c.gpu=v;
        else if ((k=="--access_type" && v==0) || (k=="--n_ctrls" && v==1) || (k=="--blk_size" && v==64)) {}
        else throw error("unsupported option: "+a);
    }
    if (!c.threads || c.threads%64 || c.threads>0x7fffffff || !c.reqs || c.pages<c.threads ||
        (c.page_size!=512 && c.page_size!=4096) || !c.num_blks ||
        c.reqs>UINT64_MAX/c.threads || c.threads*c.reqs>UINT64_MAX/c.page_size ||
        c.pages>UINT64_MAX/c.page_size || !c.num_queues || c.num_queues>512 ||
        c.queue_depth<2 || c.queue_depth>1024 || (c.queue_depth & (c.queue_depth-1))) throw error("invalid geometry");
    if (c.verify && c.reqs!=1) throw error("--verify requires reqs=1 to check every requested byte");
    return c;
}
__global__ void read_kernel(Controller **ctrls, page_cache_d_t *pc, uint64_t threads,
        uint64_t reqs, uint64_t range, uint64_t blocks, bool random, uint64_t *last) {
    uint64_t tid=uint64_t(blockIdx.x)*blockDim.x+threadIdx.x;
    if (tid>=threads) return;
    auto qp=ctrls[0]->d_qps+(tid%ctrls[0]->n_qps);
    uint64_t seed=tid*6364136223846793005ULL+1442695040888963407ULL;
    for (uint64_t i=0; i<reqs; ++i) {
        seed^=seed<<13; seed^=seed>>7; seed^=seed<<17;
        uint64_t page=(random ? seed%range : (tid+i)%range);
        nvm_cmd_t cmd;
        uint16_t cid=get_cid(&qp->sq);
        nvm_cmd_header(&cmd,cid,NVM_IO_READ,qp->nvmNamespace);
        nvm_cmd_data_ptr(&cmd,pc->prp1[tid],pc->prps ? pc->prp2[tid] : 0);
        nvm_cmd_rw_blks(&cmd,page*blocks,blocks);
        sq_enqueue(&qp->sq,&cmd);
        uint32_t loc,head;
        uint32_t pos=cq_poll(&qp->cq,cid,&loc,&head);
        if (static_cast<volatile nvm_cpl_t*>(qp->cq.vaddr)[pos].dword[3] >> 17) {
            printf("NVMe completion error cid=%u\n",unsigned(cid)); asm("trap;");
        }
        cq_dequeue(&qp->cq,pos,&qp->sq,loc,head);
        put_cid(&qp->sq,cid);
        last[tid]=page;
    }
}
int main(int argc,char **argv) try {
    auto c=parse(argc,argv);
    cuda_err_chk(cudaSetDevice(c.gpu));
    Controller ctrl("/dev/libnvm0",1,c.gpu,c.queue_depth,c.num_queues);
    if (ctrl.n_qps!=c.num_queues || c.page_size%ctrl.blk_size ||
        c.num_blks>ctrl.ns.size/(c.page_size/ctrl.blk_size)) throw error("namespace/queue mismatch");
    for (unsigned q=0; q<ctrl.n_qps; ++q)
        if (ctrl.h_qps[q]->sq.qs!=c.queue_depth || ctrl.h_qps[q]->cq.qs!=c.queue_depth)
            throw error("controller clamped queue depth");
    std::vector<Controller*> ctrls{&ctrl};
    page_cache_t pc(c.page_size,c.pages,c.gpu,ctrl,64,ctrls);
    uint64_t *last=nullptr;
    cuda_err_chk(cudaMalloc(&last,c.threads*sizeof(*last)));
    cudaEvent_t begin,end;
    cuda_err_chk(cudaEventCreate(&begin)); cuda_err_chk(cudaEventCreate(&end));
    cuda_err_chk(cudaEventRecord(begin));
    read_kernel<<<c.threads/64,64>>>(pc.pdt.d_ctrls,pc.d_pc_ptr,c.threads,c.reqs,c.num_blks,
                                   c.page_size/ctrl.blk_size,c.random,last);
    cuda_err_chk(cudaGetLastError());
    cuda_err_chk(cudaEventRecord(end)); cuda_err_chk(cudaEventSynchronize(end));
    float ms=0; cuda_err_chk(cudaEventElapsedTime(&ms,begin,end));
    if (c.verify) {
        std::vector<uint64_t> host(c.threads*c.page_size/8), addresses(c.threads);
        cuda_err_chk(cudaMemcpy(host.data(),pc.pdt.base_addr,host.size()*8,cudaMemcpyDeviceToHost));
        cuda_err_chk(cudaMemcpy(addresses.data(),last,addresses.size()*8,cudaMemcpyDeviceToHost));
        for (uint64_t t=0; t<c.threads; ++t) for (uint64_t w=0; w<c.page_size/8; ++w)
            if (host[t*c.page_size/8+w]!=swarmio_pattern(addresses[t]*c.page_size/8+w))
                throw error("payload verification mismatch at thread "+std::to_string(t));
        printf("Verification: PASS (every requested byte, reqs=1)\n");
    }
    uint64_t ops=c.threads*c.reqs;
    printf("Engine: bam\nBenchmark protocol: dsa-read-v1\n");
    printf("Elapsed Time: %.6f Number of Ops: %llu Data Size (bytes): %llu\n",double(ms)*1000,
           (unsigned long long)ops,(unsigned long long)(ops*c.page_size));
    printf("Ops/sec: %.6f\nBandwidth bytes/sec: %.6f\n",ops/(ms/1000.0),ops*c.page_size/(ms/1000.0));
    cuda_err_chk(cudaFree(last)); cuda_err_chk(cudaEventDestroy(begin)); cuda_err_chk(cudaEventDestroy(end));
    return 0;
} catch (const std::exception& e) { fprintf(stderr,"ERROR: %s\n",e.what()); return 1; }
