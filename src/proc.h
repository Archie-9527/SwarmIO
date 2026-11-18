// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief SwarmIO 的 procfs 参数与统计入口的初始化、清理接口。
 */
#ifndef _SWARMIO_PROC_H
#define _SWARMIO_PROC_H

#include "nvmev/nvme_vdev.h"

int swarmio_proc_init(struct nvme_vdev *vdev);
void swarmio_proc_exit(struct nvme_vdev *vdev);

#endif /* _SWARMIO_PROC_H */
