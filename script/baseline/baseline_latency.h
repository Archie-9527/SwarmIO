/* SPDX-License-Identifier: GPL-2.0-only */
/* Copied into an isolated diagnostic module; never included in throughput builds. */
#ifndef SWARMIO_BASELINE_LATENCY_H
#define SWARMIO_BASELINE_LATENCY_H
#include <linux/types.h>
struct idxd_wq;
enum baseline_stage { BASELINE_SQ, BASELINE_WAIT, BASELINE_RECLAIM };
struct baseline_token {
	struct idxd_wq *wq;
	u64 start;
	bool sampled;
};
struct baseline_token baseline_begin(struct idxd_wq *wq, unsigned int stage);
void baseline_end(struct baseline_token *token, unsigned int stage, u64 units);
int baseline_latency_init(void);
void baseline_latency_exit(void);
#endif
