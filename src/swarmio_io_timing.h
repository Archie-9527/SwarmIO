/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef SWARMIO_IO_TIMING_H
#define SWARMIO_IO_TIMING_H
#include <linux/types.h>

/* 只在启用 io_latency 时打时间戳；随 work 复用，不导出逐请求记录。 */
struct swarmio_bar_timing {
	bool enabled;
	u64 start_ns;
	u64 map_ns, copy_ns, unmap_ns, bar_ns, xfer_ns, segments;
};
extern bool swarmio_io_latency_enabled;
#endif
