// SPDX-License-Identifier: GPL-2.0-only
// Host-side checks and statistics only; the author GPU kernels are unchanged.
static void require_queue_geometry(const Controller& ctrl, unsigned qps, unsigned qd)
{
    if (!qps || ctrl.n_qps != qps || ctrl.n_sqs < qps || ctrl.n_cqs < qps)
        throw error("controller returned a different QP count");
    for (unsigned q = 0; q < qps; ++q) {
        if (!ctrl.h_qps[q] || ctrl.h_qps[q]->qp_id != q + 1 ||
            ctrl.h_qps[q]->sq.no != q + 1 || ctrl.h_qps[q]->cq.no != q + 1)
            throw error("controller created an unexpected SQ or CQ ID");
        if (ctrl.h_qps[q]->sq.qs != qd || ctrl.h_qps[q]->cq.qs != qd)
            throw error("controller returned a different SQ or CQ depth");
    }
    std::cout << std::dec << "Actual QPs: " << qps << " SQD: " << qd << " CQD: " << qd << '\n';
    std::cout << "Actual QID range: 1-" << qps << '\n';
    std::cout << "Reserved SQs: " << ctrl.n_sqs << " CQs: " << ctrl.n_cqs << '\n';
}

static uint64_t read_extra_count(const page_cache_t& cache)
{
    using Counter = cuda::atomic<uint64_t, cuda::thread_scope_device>;
    static_assert(sizeof(Counter) == sizeof(uint64_t));
    uint64_t count;
    const auto status = cudaMemcpy(&count, cache.pdt.extra_reads, sizeof(count),
                                   cudaMemcpyDeviceToHost);
    if (status != cudaSuccess)
        throw error(cudaGetErrorString(status));
    return count;
}
