#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Stage reproducible modules without modifying or installing production code."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback

from common import ROOT
from module_edits import DSA2_EDITS, WEIGHTED_EDITS

DIRECTORY = Path(__file__).resolve().parent
FLAGS = dict(max_queues=256, max_fetch_sqes=1024, worker_queue_size=32768,
             coalesce_sqe='y', prefetch_sqe='n', skip_io='n', batch_worker_dma='y',
             use_cont_qid='n', assign_worker_rr='n', use_committer='n',
             use_agg_timing_update='y', use_cas_timing_update='y', profile_req='n',
             use_disp_dma='y', use_worker_dma='y')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise RuntimeError(f'隔离构建源码不匹配：{path}: {before[:100]}')
    path.write_text(source.replace(before, after, 1))


def headers(root=ROOT):
    explicit = os.environ.get('IDXD_INCLUDE')
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        for folder in ('dsa', 'src'):
            for command in sorted((root / folder).glob('.*.o.cmd')):
                for include in re.findall(r'(?:^|\s)-I([^\s\\]+)', command.read_text()):
                    p = Path(include.strip('"\''))
                    candidates.append(p if p.is_absolute() else root / p)
        base = re.match(r'\d+\.\d+\.\d+', os.uname().release)
        if base:
            candidates.append(Path(f'/usr/src/linux-source-{base[0]}/drivers/dma/idxd'))
    found = next((p.resolve() for p in candidates if (p / 'idxd.h').is_file()), None)
    if found is None:
        raise RuntimeError('找不到私有 idxd.h，请以 sudo env IDXD_INCLUDE=/匹配源码/drivers/dma/idxd bash script/baseline/run_paper_baseline.sh --run 执行')
    return found


def copy_source(root, build):
    build.mkdir(parents=True)
    for name in ('Makefile', 'Kbuild'):
        shutil.copy2(root / name, build / name)
    for folder in ('src', 'nvmev', 'dsa'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_file() and path.suffix in ('.c', '.h'):
                target = build / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    return {str(p.relative_to(build)): digest(p) for p in build.rglob('*') if p.is_file()}


def stage_original(out, root=ROOT):
    build = out / 'build-original'
    source_hashes = copy_source(root, build)
    (out / 'variant.json').write_text(json.dumps({
        'variant': 'original-interleaved',
        'source_sha256': source_hashes,
    }, indent=2) + '\n')
    return build


def stage_variant(kind, out, root=ROOT, weights=(128,)):
    out.mkdir(parents=True, exist_ok=True)
    if kind == 'original':
        return stage_original(out, root)
    if kind not in ('dsa2', 'weighted'):
        raise ValueError(f'未知基线模块：{kind}')
    if kind == 'weighted' and (not weights or any(w < 4 or w > 252 or w % 4 for w in weights)):
        raise ValueError('DSA0 QP 权重必须为 [4,252] 内 4 的倍数')
    log = out / 'stage.log'
    print(f'准备隔离源码 {kind}，日志：{log}', flush=True)
    try:
        include = headers(root)
        build = out / ('build-dsa2' if kind == 'dsa2' else 'build-qid-weights')
        source_hashes = copy_source(root, build)
        metadata = {'kernel': os.uname().release, 'idxd_include': str(include), 'source_sha256': source_hashes}
        if kind == 'dsa2':
            for before, after in DSA2_EDITS:
                replace_once(build / 'dsa/dsa.c', before, after)
            metadata.update(selected_physical_dsa=2, required_dsa_num_devs=1, qid_layout='interleaved',
                            variant_dsa_c_sha256=digest(build / 'dsa/dsa.c'),
                            edits=[{'before': a, 'after': b} for a, b in DSA2_EDITS])
        else:
            for name, before, after in WEIGHTED_EDITS:
                replace_once(build / name, before, after)
            metadata.update(physical_dsas=[0, 2], required_dsa_num_devs=2, required_service_units=8,
                            required_qps=256, qid_layout='weighted-contiguous', tested_dsa0_qps=list(weights),
                            variant_sha256={name: digest(build / name) for name in {e[0] for e in WEIGHTED_EDITS}},
                            edits=[{'path': n, 'before': a, 'after': b} for n, a, b in WEIGHTED_EDITS])
        (out / 'variant.json').write_text(json.dumps(metadata, indent=2) + '\n')
        (out / 'idxd-include.txt').write_text(str(include) + '\n')
        log.write_text(f'variant={kind}\nidxd_include={include}\nbuild={build}\n')
        return build
    except (OSError, RuntimeError, ValueError) as error:
        log.write_text(traceback.format_exc())
        raise RuntimeError(f'隔离源码准备失败：{kind}，详见 {log}：{error}') from error


def instrument(build):
    """Measure CPU-observed sync-SQ, batch polling and batch reclamation calls."""
    for name in ('baseline_latency.c', 'baseline_latency.h'):
        shutil.copy2(DIRECTORY / name, build / 'dsa' / name)
    with (build / 'Kbuild').open('a') as file:
        file.write('\nswarmio-objs += dsa/baseline_latency.o\n')
    proc = build / 'src/proc.c'
    replace_once(proc, '#include "worker.h"',
                 '#include "worker.h"\n#include "dsa/baseline_latency.h"')
    replace_once(proc, 'int swarmio_proc_init(struct nvme_vdev *vdev)\n{',
                 'int swarmio_proc_init(struct nvme_vdev *vdev)\n{\n'
                 '\tint baseline_ret = baseline_latency_init();\n'
                 '\tif (baseline_ret)\n\t\treturn baseline_ret;')
    replace_once(proc, '\tif (!vdev->proc_root)\n\t\treturn -ENOMEM;',
                 '\tif (!vdev->proc_root) {\n\t\tbaseline_latency_exit();\n'
                 '\t\treturn -ENOMEM;\n\t}')
    replace_once(proc, 'void swarmio_proc_exit(struct nvme_vdev *vdev)\n{',
                 'void swarmio_proc_exit(struct nvme_vdev *vdev)\n{\n'
                 '\tbaseline_latency_exit();')
    disp = build / 'src/dispatcher.c'
    # Only the coalesced SQ call: no worker PRP reads or non-coalesced branches.
    replace_once(disp, '#include "dispatcher.h"',
                 '#include "dispatcher.h"\n#include "dsa/baseline_latency.h"')
    before = ('\t\t\t\tret = dsa_dma_issue_sync_single(\n'
              '\t\t\t\t\tdispatcher->dma_ctx, cmd_buffer_dma,\n'
              '\t\t\t\t\tsq_dma(sq->fetched_pos),\n'
              '\t\t\t\t\tsizeof(struct nvme_command) * num_fetch,\n'
              '\t\t\t\t\tNULL, NULL);')
    replace_once(disp, before,
                 '\t\t\t\t{\n\t\t\t\tstruct baseline_token baseline_t = '
                 'baseline_begin(dispatcher->dma_ctx->wq, BASELINE_SQ);\n' + before + '\n'
                 '\t\t\t\tif (!ret) baseline_end(&baseline_t, BASELINE_SQ, num_fetch);\n'
                 '\t\t\t\t}')
    batch = build / 'dsa/batch.c'
    replace_once(batch, '#include "dsa.h"',
                 '#include "dsa.h"\n#include <linux/ktime.h>\n#include "baseline_latency.h"')
    source = batch.read_text()
    # Instrument both normal oldest-first retirement and shutdown/all retirement.
    for name, last in (('dsa_dma_wait_oldest_batch', 'dsa_dma_wait_all_batch'),
                       ('dsa_dma_wait_all_batch', 'dsa_dma_batch_idle')):
        start = source.index('int ' + name + '(')
        end = source.index(('int ' if last.startswith('dsa_dma_wait') else 'bool ') + last + '(', start)
        body = source[start:end]
        indent = '\t' if name.endswith('oldest_batch') else '\t\t'
        poll = indent + 'ret = __dsa_sync_wait(batch_desc->idxd_desc, ctx->timeout);'
        prefix = (indent + 'struct baseline_token baseline_t = baseline_begin(ctx->wq, BASELINE_WAIT);\n'
                  + indent + 'u64 baseline_units = batch_desc->num_filled;\n'
                  + indent + '/* Tail padding is a NOOP, not a payload segment. */\n'
                  + indent + 'if (baseline_units == 2 && batch_desc->lv2_hw_descs[1].opcode == DSA_OPCODE_NOOP)\n'
                  + indent + '\tbaseline_units = 1;\n')
        if body.count(poll) != 1:
            raise RuntimeError('batch 轮询插桩位置不匹配')
        body = body.replace(poll, prefix + poll, 1)
        success = indent + 'clear_bit('
        position = body.index(success)
        body = (body[:position] + indent + 'baseline_end(&baseline_t, BASELINE_WAIT, baseline_units);\n'
                + indent + 'if (baseline_t.sampled) baseline_t.start = ktime_get_ns();\n' + body[position:])
        reset = indent + 'batch_desc->num_filled = 0;'
        # Last reset is the successful path; the earlier reset belongs to EIO.
        position = body.rindex(reset) + len(reset)
        body = body[:position] + '\n' + indent + 'baseline_end(&baseline_t, BASELINE_RECLAIM, baseline_units);' + body[position:]
        source = source[:start] + body + source[end:]
    batch.write_text(source)


def compile_module(build, include, contiguous, log):
    flags = FLAGS | {'use_cont_qid': 'y' if contiguous else 'n'}
    command = ['make', '-C', str(build), '-j8', f'KERNELDIR=/lib/modules/{os.uname().release}/build',
               'CC=gcc-12', f'IDXD_INCLUDE={include}', *(f'{k}={v}' for k, v in flags.items())]
    with log.open('w') as file:
        subprocess.run(command, stdout=file, stderr=subprocess.STDOUT, check=True)
    (build.parent / 'build.json').write_text(json.dumps({
        'command': command, 'flags': flags, 'module_sha256': digest(build / 'swarmio.ko'),
        'staged_source_sha256': {str(p.relative_to(build)): digest(p) for p in build.rglob('*')
                                if p.is_file() and p.suffix in ('.c', '.h')},
    }, indent=2) + '\n')
    return build / 'swarmio.ko'
