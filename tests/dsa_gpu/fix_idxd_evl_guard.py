#!/usr/bin/env python3
"""仅给Linux源码的idxd_device_evl_free补空指针检查，不编译/安装/替换运行驱动。

用法：python3 fix_idxd_evl_guard.py /path/to/linux/drivers/dma/idxd/device.c
对应上游修复52d2edea0d63c的free路径部分；不包含该提交的FLR恢复路径修复。
必须将源码修复构建为真正运行的内核/idxd驱动后才生效。
"""
import argparse
from pathlib import Path
import re
import shutil

def transform(source):
    declaration = re.search(
        r'static\s+void\s+idxd_device_evl_free\s*\(\s*struct\s+idxd_device\s*\*\s*idxd\s*\)\s*\{',
        source)
    if not declaration:
        raise ValueError('找不到idxd_device_evl_free函数定义；未修改')
    end = re.search(r'^}', source[declaration.end():], re.M)
    if not end:
        raise ValueError('无法确定函数边界；未修改')
    stop = declaration.end() + end.end()
    body = source[declaration.start():stop]
    if re.search(r'if\s*\(\s*!\s*evl\s*(?:\)|\|\|)', body):
        return source, False
    pointer = re.search(r'\bstruct\s+idxd_evl\s*\*\s*evl\s*=\s*idxd->evl\s*;[^\S\n]*\n', body)
    if not pointer:
        raise ValueError('找不到预期evl声明；未修改')
    guard = '\n\t/* EVL is optional; do not lock a NULL event-log object. */\n\tif (!evl)\n\t\treturn;\n'
    pos = declaration.start() + pointer.end()
    return source[:pos] + guard + source[pos:], True

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('device_c', type=Path)
    args = parser.parse_args()
    path = args.device_c.expanduser().resolve()
    old = path.read_text()
    new, changed = transform(old)
    if not changed:
        print('源码已有evl空指针检查，未修改；需继续核对已加载驱动与完整故障原因。')
        return
    backup = path.with_name(path.name + '.before-dg-evl-guard')
    if backup.exists():
        raise SystemExit('备份已存在，拒绝覆盖；未修改')
    shutil.copy2(path, backup)
    path.write_text(new)
    print(f'仅修复了源码：{path}\n备份：{backup}\n运行内核/idxd尚未改变；重新make本测试模块不会使此修复生效。')

if __name__ == '__main__':
    main()
