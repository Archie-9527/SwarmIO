"""验证EVL源码补丁只修改free函数，以及NULL/非NULL路径和幂等性。"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('evl_fix',HERE/'fix_idxd_evl_guard.py')
fix=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)
source=r'''
#include <assert.h>
struct device { int unused; };
struct pci_dev { struct device dev; };
struct mutex { int locked; };
struct idxd_evl { struct mutex lock; int freed; };
struct idxd_device { struct pci_dev *pdev; struct idxd_evl *evl; };
static int locks;
static void mutex_lock(struct mutex *m) { assert(m); m->locked++; locks++; }
static void mutex_unlock(struct mutex *m) { assert(m && m->locked); m->locked--; }
static void idxd_device_evl_free(struct idxd_device *idxd);
static int unrelated_setup(struct idxd_device *idxd)
{
    struct idxd_evl *evl = idxd->evl;
    return evl != 0;
}
static void idxd_device_evl_free(struct idxd_device *idxd)
{
    struct device *dev = &idxd->pdev->dev;
    struct idxd_evl *evl = idxd->evl;

    mutex_lock(&evl->lock);
    evl->freed += dev->unused + 1;
    mutex_unlock(&evl->lock);
}
int main(void)
{
    struct pci_dev pdev = {0};
    struct idxd_device no_evl = { &pdev, 0 };
    struct idxd_evl evl = {0};
    struct idxd_device with_evl = { &pdev, &evl };
    idxd_device_evl_free(&no_evl);
    assert(locks == 0);
    idxd_device_evl_free(&with_evl);
    assert(locks == 1 && evl.freed == 1 && evl.lock.locked == 0);
    assert(!unrelated_setup(&no_evl) && unrelated_setup(&with_evl));
}
'''
updated,changed=fix.transform(source)
assert changed
assert updated.count('if (!evl)')==1
assert updated.index('if (!evl)')>updated.index('static void idxd_device_evl_free(struct idxd_device *idxd)\n{')
assert fix.transform(updated)==(updated,False)
try:
    fix.transform('unrelated input')
except ValueError:
    pass
else:
    raise AssertionError('必须拒绝不匹配的源码')
with tempfile.TemporaryDirectory(prefix='dg-evl-guard-') as tmp:
    p=Path(tmp)
    (p/'test.c').write_text(updated)
    subprocess.run([shutil.which('cc'),'-std=c11','-Wall','-Wextra','-Werror',
                    str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
print('PASS: EVL patch scope/idempotence, NULL object skips lock, normal object cleanup preserved')
