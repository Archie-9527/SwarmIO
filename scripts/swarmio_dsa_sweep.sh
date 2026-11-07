#!/usr/bin/env bash
# Reuse existing builds; no compilation, no WQ reconfiguration, no retries.
# Usage: bash scripts/swarmio_dsa_sweep.sh CURRENT_LOADED_BUILD|--unloaded
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: bash scripts/swarmio_dsa_sweep.sh CURRENT_LOADED_BUILD|--unloaded" >&2
    exit 2
fi
SWEEP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SWEEP_ROOT"
SWEEP_ACTIVE="$1"
SWEEP_SCALE=build-dsa-scale-q512-su4-b128-d16
SWEEP_TOTAL=134217728
SWEEP_REPEATS=3
SWEEP_OUTPUT="results/sweep-$(date +%Y%m%d-%H%M%S)"

# Check the recorded configurations before any module change.
python3 - "$SWEEP_ACTIVE" <<'PY'
import json, sys
from pathlib import Path

active = Path(sys.argv[1])
if sys.argv[1] == '--unloaded':
    if any(Path('/sys/module',name).exists() for name in ('swarmio','libnvm')):
        raise SystemExit('--unloaded requires both swarmio and libnvm to be absent.')
elif not (active/'loaded.json').is_file():
    raise SystemExit('Current build needs a loaded.json; pass the actually loaded build directory.')
configs = []
for batch in (32,64,128):
    directory = Path(f'build-dsa-sq-b{batch}-d16')
    manifest = json.loads((directory/'manifest.json').read_text())
    assert manifest['state'] == 'built', f'{directory}: not built'
    c = manifest['config']
    assert c['module']['dma_batch_size'] == batch, directory
    assert c['module']['num_dma_descs_per_worker'] == 16, directory
    assert int(c['build']['NR_MAX_IO_QUEUE']) >= 64, directory
    assert int(c.get('libnvm',{}).get('max_gpu_pinned_mb',32)) >= 24, directory
    # Benchmark defaults are overridden below. The worker/model settings must match.
    module = {k:v for k,v in c['module'].items() if k != 'dma_batch_size'}
    configs.append((module,c['build'],c.get('gpu')))
assert all(c == configs[0] for c in configs), 'Batch builds differ beyond batch size; inspect their manifests first.'
scale = json.loads(Path('build-dsa-scale-q512-su4-b128-d16/manifest.json').read_text())
assert scale['state'] == 'built'
c = scale['config']
assert int(c['build']['NR_MAX_IO_QUEUE']) >= 512
assert c['module']['dma_batch_size'] == 128
assert c['module']['num_dma_descs_per_worker'] == 16
assert int(c.get('libnvm',{}).get('max_gpu_pinned_mb',32)) >= 192
print('Existing builds checked. 3 independent sweeps; 13 settings x 3 repeats = 39 runs.')
PY

mkdir -p "$SWEEP_OUTPUT"
sudo -v
# Check every target before unloading the current build. No insmod/rmmod here.
for SWEEP_BUILD in build-dsa-sq-b32-d16 build-dsa-sq-b64-d16 \
    build-dsa-sq-b128-d16 "$SWEEP_SCALE"; do
    sudo python3 scripts/swarmio_bam.py --build-dir "$SWEEP_BUILD" check-load \
        | tee "$SWEEP_OUTPUT/preflight-${SWEEP_BUILD}.log"
done
if [[ "$SWEEP_ACTIVE" == --unloaded ]]; then
    SWEEP_ACTIVE=""
fi
sudo python3 scripts/swarmio_bam.py status \
    --output "$SWEEP_OUTPUT/before.log"

switch_build() {
    local next="$1"
    if [[ "$SWEEP_ACTIVE" != "$next" ]]; then
        if [[ -n "$SWEEP_ACTIVE" ]]; then
            sudo python3 scripts/swarmio_bam.py --build-dir "$SWEEP_ACTIVE" unload
        fi
        sudo python3 scripts/swarmio_bam.py --build-dir "$next" load
        SWEEP_ACTIVE="$next"
    fi
}

run_point() {
    local dimension="$1" threads="$2" qp="$3" pages="$4"
    local reqs=$((SWEEP_TOTAL / threads)) repeat logfile
    for repeat in 1 2 3; do
        logfile="$SWEEP_OUTPUT/${dimension}-t${threads}-q${qp}-r${repeat}.log"
        echo "[$dimension] build=$SWEEP_ACTIVE threads=$threads reqs=$reqs pages=$pages QP=$qp repeat=$repeat/$SWEEP_REPEATS"
        # pipefail stops the script immediately on a benchmark failure.
        sudo python3 scripts/swarmio_bam.py --build-dir "$SWEEP_ACTIVE" run \
            --threads "$threads" --reqs "$reqs" --pages "$pages" \
            --num-queues "$qp" --queue-depth 1024 \
            --page-size 512 --num-blks 262144 --random --monitor-interval 0.1 \
            2>&1 | tee "$logfile"
        python3 - "$logfile" "$SWEEP_OUTPUT/index.jsonl" "$dimension" \
            "$SWEEP_ACTIVE" "$threads" "$reqs" "$pages" "$qp" "$repeat" <<'PY'
import json, re, sys
from pathlib import Path
log, index, dimension, build, threads, reqs, pages, qp, repeat = sys.argv[1:]
match = re.search(r'^输出目录：\s*(.+)$',Path(log).read_text(),re.M)
if not match:
    raise SystemExit('Cannot identify result directory; inspect ' + log)
record = dict(dimension=dimension,build_dir=build,threads=int(threads),reqs=int(reqs),
              pages=int(pages),num_queues=int(qp),repeat=int(repeat),
              result_dir=match.group(1).strip(),log=log)
with Path(index).open('a') as stream:
    stream.write(json.dumps(record,ensure_ascii=False)+'\n')
PY
    done
}

# 1. Only DSA payload batch size changes; each setting has 32768 threads and 64 QPs.
#    P2P pin budget = 16 MiB payload + 8 MiB queues = 24 MiB.
for SWEEP_BATCH in 32 64 128; do
    switch_build "build-dsa-sq-b${SWEEP_BATCH}-d16"
    run_point "batch${SWEEP_BATCH}" 32768 64 32768
done

# 2. Only GPU thread count changes (reqs inversely adjusted for constant total work).
#    Keep the allocated payload buffer fixed at 262144 pages (128 MiB).
switch_build "$SWEEP_SCALE"
for SWEEP_THREADS in 16384 32768 65536 131072 262144; do
    run_point threads "$SWEEP_THREADS" 256 262144
done

# 3. Only QP count changes. Same threads, requests and payload buffer for all points.
for SWEEP_QP in 32 64 128 256 512; do
    run_point qp 131072 "$SWEEP_QP" 262144
done

echo "Completed. Group/result mapping: $SWEEP_OUTPUT/index.jsonl"
echo "Modules remain loaded from: $SWEEP_ACTIVE"
