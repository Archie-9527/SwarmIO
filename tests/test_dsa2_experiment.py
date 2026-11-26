"""Validate physical DSA selection without loading a kernel module."""
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
            'stage_variant("dsa2", out, root=root)\n')


def function(source, name):
    return re.search(rf'^(?:static )?int {name}\([^;]*?\n\{{.*?^\}}',
                     source, re.M | re.S)[0]


STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <string.h>
#define DSA_SCAN_MAX_DEVS 16
#define DSA_MAX_DEVS 4
#define DSA_MAX_GROUPS 4
#define DSA_MAX_WQS 16
#define DSA_MAX_ENGINES 4
#define DSA_MAX_WQ_SIZE 256
#define DSA_MIN_PRIORITY 1
#define DSA_MAX_PRIORITY 15
#define SWARMIO_ERROR(...) ((void)0)
#define SWARMIO_WARN(...) ((void)0)
struct device { int driver, id; };
struct idxd_device {
    int id, max_wqs, max_engines, max_wq_size;
    struct { struct device conf_dev; } idxd_dev;
};
struct dsa_cfg_params {
    unsigned int num_devs, num_groups_per_dev, num_wqs_per_group, num_engines_per_group;
    const unsigned int *wq_sizes, *wq_priorities;
};
static struct idxd_device devices[16];
static struct idxd_device *dsa_idxd_list[4];
static int present[16], enabled[16], touched[16], lookups[16], fail_id;
static unsigned int dsa_num_devs;
static struct idxd_device *__dev_id_to_idxd(int id)
{
    assert(id >= 0 && id < 16);
    ++lookups[id];
    return present[id] ? &devices[id] : NULL;
}
static int dsa_wqs_config(struct idxd_device *d) { ++touched[d->id]; return 0; }
static int dsa_engines_config(struct idxd_device *d) { ++touched[d->id]; return 0; }
static int device_attach(struct device *d) { d->driver = 1; return 1; }
static int dsa_wqs_enable(struct idxd_device *d)
{
    if (d->id == fail_id) return -EIO;
    enabled[d->id] = 1;
    return 0;
}
static void device_release_driver(struct device *d)
{
    d->driver = 0;
    enabled[d->id] = 0;
}
static void dsa_disable_devices(void)
{
    for (unsigned int i = 0; i < dsa_num_devs; ++i) {
        device_release_driver(&dsa_idxd_list[i]->idxd_dev.conf_dev);
        dsa_idxd_list[i] = NULL;
    }
    dsa_num_devs = 0;
}
static void reset(void)
{
    memset(present, 0, sizeof(present));
    memset(enabled, 0, sizeof(enabled));
    memset(touched, 0, sizeof(touched));
    memset(lookups, 0, sizeof(lookups));
    memset(dsa_idxd_list, 0, sizeof(dsa_idxd_list));
    fail_id = -1;
    dsa_num_devs = 1;
    for (int i = 0; i < 16; ++i) {
        devices[i] = (struct idxd_device){
            .id = i, .max_wqs = 8, .max_engines = 4, .max_wq_size = 128,
            .idxd_dev.conf_dev = { .driver = 1, .id = i }
        };
    }
}
'''


class Dsa2ExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.headers = cls.base / 'private-headers'
        cls.headers.mkdir()
        (cls.headers / 'idxd.h').write_text('/* staging fixture */\n')
        cls.output = cls.base / 'experiment'
        cls.output.mkdir()
        cls.source = (ROOT / 'dsa/dsa.c').read_text()
        subprocess.run(['python3', '-', str(ROOT), str(cls.output)], input=staging_program(),
                       text=True, check=True, capture_output=True,
                       env=dict(os.environ, IDXD_INCLUDE=str(cls.headers)))
        cls.variant = (cls.output / 'build-dsa2/dsa/dsa.c').read_text()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_isolation_and_manifest(self):
        self.assertEqual((ROOT / 'dsa/dsa.c').read_text(), self.source)
        manifest = json.loads((self.output / 'variant.json').read_text())
        self.assertEqual(manifest['selected_physical_dsa'], 2)
        self.assertEqual(manifest['required_dsa_num_devs'], 1)
        self.assertEqual(manifest['idxd_include'], str(self.headers.resolve()))
        self.assertEqual(manifest['source_sha256']['dsa/dsa.c'],
                         hashlib.sha256(self.source.encode()).hexdigest())
        self.assertEqual(manifest['variant_dsa_c_sha256'],
                         hashlib.sha256(self.variant.encode()).hexdigest())
        restored = self.variant
        for edit in reversed(manifest['edits']):
            restored = restored.replace(edit['after'], edit['before'], 1)
        self.assertEqual(restored, self.source)
        self.assertFalse(any((self.output / 'build-dsa2').rglob('*.ko')))

    def test_rejects_unknown_source_before_building(self):
        fixture = self.base / 'unknown-source'
        fixture.mkdir()
        for name in ('Makefile', 'Kbuild'):
            shutil.copy2(ROOT / name, fixture / name)
        for folder in ('dsa', 'src', 'nvmev'):
            (fixture / folder).mkdir()
        changed = self.source.replace('for (dsa_id = 0;', 'for (dsa_id = 4;', 1)
        (fixture / 'dsa/dsa.c').write_text(changed)
        output = self.base / 'rejected-experiment'
        output.mkdir()
        result = subprocess.run(['python3', '-', str(fixture), str(output)],
                                input=staging_program(), text=True, capture_output=True,
                                env=dict(os.environ, IDXD_INCLUDE=str(self.headers)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('隔离构建源码不匹配', result.stderr)
        self.assertEqual((fixture / 'dsa/dsa.c').read_text(), changed)

    def test_recovers_private_headers_from_a_successful_build_command(self):
        fixture = self.base / 'header-discovery'
        fixture.mkdir()
        for name in ('Makefile', 'Kbuild'):
            shutil.copy2(ROOT / name, fixture / name)
        for folder in ('dsa', 'src', 'nvmev'):
            (fixture / folder).mkdir()
        (fixture / 'dsa/dsa.c').write_text(self.source)
        (fixture / 'dsa/.dsa.o.cmd').write_text(
            f'savedcmd_dsa.o := gcc -I/does-not-exist -I{self.headers}\n')
        output = self.base / 'discovered-header-experiment'
        output.mkdir()
        environment = dict(os.environ)
        environment.pop('IDXD_INCLUDE', None)
        subprocess.run(['python3', '-', str(fixture), str(output)],
                       input=staging_program(), text=True, check=True, capture_output=True,
                       env=environment)
        self.assertEqual((output / 'idxd-include.txt').read_text().strip(),
                         str(self.headers.resolve()))

    def c_check(self, source, exercise):
        program = '\n'.join((STUBS, function(source, 'dsa_validate_params'),
                              function(source, 'dsa_enable_devices'), exercise))
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / 'selection-check')
            subprocess.run(['cc', '-std=c11', '-x', 'c', '-', '-o', binary],
                           input=program, text=True, check=True)
            subprocess.run([binary], check=True, timeout=5)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_original_scans_device_holes_and_selects_both_devices(self):
        self.c_check(self.source, r'''
int main(void) {
    reset(); present[0] = present[2] = 1; dsa_num_devs = 2;
    assert(dsa_enable_devices() == 0);
    assert(dsa_num_devs == 2 && dsa_idxd_list[0] == &devices[0]);
    assert(dsa_idxd_list[1] == &devices[2]);
    assert(lookups[0] == 1 && lookups[1] == 1 && lookups[2] == 1);
    return 0;
}
''')

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_variant_checks_dsa2_limits_and_rejects_multiple_devices(self):
        self.c_check(self.variant, r'''
int main(void) {
    reset(); present[0] = present[2] = 1;
    unsigned int sizes[2] = {2, 30}, priorities[2] = {1, 15};
    struct dsa_cfg_params p = {1, 4, 2, 1, sizes, priorities};
    devices[0].max_wqs = 1;
    assert(dsa_validate_params(&p) == 0);
    assert(lookups[0] == 0 && lookups[2] == 1);
    devices[2].max_wqs = 7;
    assert(dsa_validate_params(&p) == -EINVAL);
    devices[2].max_wqs = 8;
    p.num_devs = 2;
    assert(dsa_validate_params(&p) == -EINVAL);
    for (int i = 0; i < 16; ++i) assert(touched[i] == 0);
    return 0;
}
''')

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_variant_never_configures_another_dsa_or_falls_back(self):
        self.c_check(self.variant, r'''
int main(void) {
    reset(); present[0] = present[2] = present[4] = 1;
    assert(dsa_enable_devices() == 0);
    assert(dsa_num_devs == 1 && dsa_idxd_list[0] == &devices[2]);
    for (int i = 0; i < 16; ++i) {
        assert(touched[i] == (i == 2 ? 2 : 0));
        assert(enabled[i] == (i == 2));
    }
    reset(); present[0] = present[4] = 1;
    assert(dsa_enable_devices() == -ENODEV && dsa_num_devs == 0);
    for (int i = 0; i < 16; ++i) {
        assert(lookups[i] == (i == 2));
        assert(touched[i] == 0 && enabled[i] == 0);
    }
    reset(); present[0] = present[2] = 1; fail_id = 2;
    assert(dsa_enable_devices() == -ENODEV && dsa_num_devs == 0);
    assert(devices[2].idxd_dev.conf_dev.driver == 0);
    assert(touched[0] == 0 && devices[0].idxd_dev.conf_dev.driver == 1);
    return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
