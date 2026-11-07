"""Reproduce glibc errno.h -> linux/errno.h while mock.h's guard is active."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from test_kernel_syntax import write_header_stub

with tempfile.TemporaryDirectory(prefix='dg-errno-') as tmp:
    p=Path(tmp); stub=p/'stub'; system=p/'system'
    (stub/'linux').mkdir(parents=True); (system/'linux').mkdir(parents=True)
    # These are deliberately separate headers, matching the Linux include
    # graph. Test-fixture constants do not supply fallbacks to production code.
    (stub/'errno.h').write_text('#include <linux/errno.h>\n')
    (system/'linux/errno.h').write_text('#define EBUSY 16\n#define EIO 5\n#define ENOMEM 12\n#define EOPNOTSUPP 95\n')
    (stub/'mock.h').write_text('#ifndef MOCK_H\n#define MOCK_H\n#include <errno.h>\n#endif\n')
    unit=p/'unit.c'
    unit.write_text('#include "mock.h"\n'
                    '_Static_assert(EBUSY==16 && EIO==5 && ENOMEM==12 && EOPNOTSUPP==95,"errno forwarding");\n')
    command=[shutil.which('cc'),'-std=gnu11','-Wall','-Wextra','-Werror','-fsyntax-only',
             '-I',str(stub),'-I',str(system),str(unit)]
    # Demonstrate that the old include cycle loses the error macros.
    (stub/'linux/errno.h').write_text('#include "mock.h"\n')
    broken=subprocess.run(command,capture_output=True,text=True)
    assert broken.returncode != 0, 'the regression did not reproduce the missing error definitions'
    write_header_stub(stub/'linux/errno.h','errno.h',platform='linux')
    subprocess.run(command,check=True)
print('PASS: reproduced Linux errno include cycle; fixed forwarding preserves EBUSY/EIO/ENOMEM/EOPNOTSUPP')
