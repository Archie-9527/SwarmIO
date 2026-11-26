"""Check isolated weighted QP routing without loading a kernel module."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'script/baseline'


def staging_program():
    return (f'import sys\nsys.path.insert(0, {str(BASELINE)!r})\n'
            'from pathlib import Path\nfrom baseline_modules import stage_variant\n'
            'root, out = map(Path, sys.argv[1:])\n'
            'stage_variant("weighted", out, root=root, weights=(128,152))\n')


class QidWeightExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        headers = cls.base / 'headers'
        headers.mkdir()
        (headers / 'idxd.h').write_text('/* private header fixture */\n')
        cls.out = cls.base / 'experiment'
        cls.out.mkdir()
        cls.originals = {name: (ROOT / name).read_bytes()
                         for name in ('src/main.c', 'nvmev/admin.c', 'dsa/dsa.c')}
        subprocess.run(['python3', '-', str(ROOT), str(cls.out)],
                       input=staging_program(), text=True,
                       check=True, capture_output=True,
                       env=dict(os.environ, IDXD_INCLUDE=str(headers)))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_variant_only_changes_parameter_validation_and_queue_assignment(self):
        manifest = json.loads((self.out / 'variant.json').read_text())
        self.assertEqual(manifest['physical_dsas'], [0, 2])
        self.assertEqual(manifest['required_qps'], 256)
        self.assertEqual(manifest['tested_dsa0_qps'], [128, 152])
        self.assertEqual({e['path'] for e in manifest['edits']},
                         {'src/main.c', 'nvmev/admin.c'})
        for name, before in self.originals.items():
            with self.subTest(name=name):
                self.assertEqual((ROOT / name).read_bytes(), before)
                self.assertEqual(manifest['source_sha256'][name],
                                 hashlib.sha256(before).hexdigest())
                variant = (self.out / 'build-qid-weights' / name).read_text()
                for edit in reversed(manifest['edits']):
                    if edit['path'] == name:
                        variant = variant.replace(edit['after'], edit['before'], 1)
                self.assertEqual(variant.encode(), before)
        self.assertFalse(any((self.out / 'build-qid-weights').rglob('*.ko')))

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_every_weight_preserves_all_queues_and_all_service_units(self):
        source = (self.out / 'build-qid-weights/nvmev/admin.c').read_text()
        helper = re.search(r'^static unsigned int comparison_qid_count\(.*?^\}',
                           source, re.M | re.S)[0]
        program = r'''
#include <assert.h>
#include <string.h>
unsigned int qid_dsa0_qps;
'''+helper+r'''
static void check(unsigned int queues, unsigned int units) {
    unsigned int owned[257] = {0}, per_device[2] = {0};
    unsigned int next = 1;
    for (unsigned int i = 0; i < units; ++i) {
        unsigned int count = comparison_qid_count(queues, units, i);
        if (queues == 256 && units == 8) {
            assert(count > 0);
            per_device[i / 4] += count;
        }
        for (unsigned int j = 0; j < count; ++j) {
            unsigned int q = next + j;
            assert(q >= 1 && q <= queues);
            ++owned[q];
        }
        next += count;
    }
    assert(next == queues + 1);
    for (unsigned int q = 1; q <= queues; ++q) assert(owned[q] == 1);
    if (queues == 256 && units == 8) {
        assert(per_device[0] == qid_dsa0_qps);
        assert(per_device[1] == 256 - qid_dsa0_qps);
    }
}
int main(void) {
    for (qid_dsa0_qps = 4; qid_dsa0_qps <= 252; qid_dsa0_qps += 4) {
        for (unsigned int queues = 1; queues <= 256; ++queues)
            for (unsigned int units = 1; units <= 8; ++units)
                check(queues, units);
    }
    qid_dsa0_qps = 128;
    for (unsigned int i = 0; i < 8; ++i)
        assert(comparison_qid_count(256, 8, i) == 32);
    qid_dsa0_qps = 152;
    for (unsigned int i = 0; i < 8; ++i)
        assert(comparison_qid_count(256, 8, i) == (i < 4 ? 38 : 26));
    return 0;
}
'''
        binary = str(self.base / 'qid-allocation-check')
        subprocess.run(['cc', '-std=c11', '-x', 'c', '-', '-o', binary],
                       input=program, text=True, check=True)
        subprocess.run([binary], check=True, timeout=10)



if __name__ == '__main__':
    unittest.main()
