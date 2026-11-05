"""可选的CUDA host语法检查。使用SDK声明替身，不代表nvcc/真实CUDA编译。"""
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
clang = shutil.which("clang++")
if not clang:
    raise SystemExit("缺少clang++，可跳过此可选检查")
runtime = r'''
#pragma once
#include <stddef.h>
#define __host__ __attribute__((host))
#define __device__ __attribute__((device))
#define __global__ __attribute__((global))
struct uint3 { unsigned x,y,z; };
struct dim3 {
    unsigned x,y,z;
    __host__ __device__ dim3(unsigned a=1,unsigned b=1,unsigned c=1):x(a),y(b),z(c){}
};
extern __device__ const uint3 blockIdx,blockDim,threadIdx,gridDim;
typedef int cudaError_t;
enum {cudaSuccess=0,cudaDevAttrGPUDirectRDMASupported=1,cudaMemcpyDeviceToHost=2};
cudaError_t cudaMalloc(void **,size_t);
cudaError_t cudaFree(void *);
cudaError_t cudaMemset(void *,int,size_t);
cudaError_t cudaSetDevice(int);
cudaError_t cudaDeviceGetAttribute(int *,int,int);
cudaError_t cudaDeviceSynchronize();
cudaError_t cudaGetLastError();
cudaError_t cudaMemcpy(void *,const void *,size_t,int);
const char *cudaGetErrorString(cudaError_t);
int cudaConfigureCall(dim3,dim3,size_t=0,void *stream=nullptr);
int cudaSetupArgument(const void *,size_t,size_t);
int cudaLaunch(const void *);
'''
driver = r'''
#pragma once
#include <stdint.h>
typedef uintptr_t CUdeviceptr;
typedef int CUresult;
enum {CUDA_SUCCESS=0,CU_POINTER_ATTRIBUTE_SYNC_MEMOPS=1};
CUresult cuPointerSetAttribute(const void *,int,CUdeviceptr);
'''
with tempfile.TemporaryDirectory(prefix="dg-cuda-syntax-") as tmp:
    p=Path(tmp)
    (p/"linux").mkdir()
    (p/"linux/types.h").write_text("typedef unsigned long long __u64; typedef unsigned int __u32;\n")
    (p/"linux/ioctl.h").write_text("#include <sys/ioctl.h>\n")
    (p/"cuda.h").write_text(driver)
    (p/"cuda_runtime.h").write_text(runtime)
    subprocess.run([clang,"-x","cuda","--cuda-host-only","-nocudainc","-nocudalib",
                    "-D__CUDACC__","-std=c++14","-Wall","-Wextra","-Werror",
                    "-I",str(p),"-I",str(HERE),"-fsyntax-only",str(HERE/"bench.cu")],check=True)
print("PASS: bench.cu CUDA host syntax with stub SDK; not an nvcc build")
