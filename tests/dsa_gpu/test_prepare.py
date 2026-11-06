"""在临时sysfs/proc替身上执行真实准备脚本的只读与拒绝路径，不操作硬件。"""
from pathlib import Path
import hashlib
import os
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='dg-prepare-policy-') as tmp:
    p=Path(tmp)
    base=p/'sys/dsa0'
    base.mkdir(parents=True)
    proc=p/'proc'
    proc.mkdir()
    (proc/'tainted').write_text('0\n')
    (proc/'modules').write_text('')
    binpath=p/'bin'
    binpath.mkdir()
    (binpath/'uname').write_text('#!/bin/sh\nprintf "Linux\\n"\n')
    (binpath/'uname').chmod(0o755)
    # 仅替换测试副本的只读输入路径；不移除生产脚本中的权限/状态检查。
    script=(HERE/'prepare_dsa.sh').read_text().replace('/proc/sys/kernel/tainted',str(proc/'tainted'))
    script=script.replace('/proc/modules',str(proc/'modules'))
    script=script.replace('root=/sys/bus/dsa/devices','root='+str(p/'sys'))
    (p/'prepare.sh').write_text(script)
    for key,value in {'max_work_queues_size':128,'max_transfer_size':2147483648,
                      'max_groups':4,'max_engines':4,'max_work_queues':8,'max_batch_size':1024,
                      'state':'disabled'}.items():
        (base/key).write_text(str(value)+'\n')
    for i in range(8):
        q=base/f'wq0.{i}'
        q.mkdir()
        for key,value in {'group_id':i//2,'size':16,'mode':'dedicated','type':'kernel',
                          'name':'dmaengine','driver_name':'dmaengine','max_batch_size':1024,
                          'max_transfer_size':2147483648,'state':'disabled'}.items():
            (q/key).write_text(str(value)+'\n')
    for i in range(4):
        e=base/f'engine0.{i}'
        e.mkdir()
        (e/'group_id').write_text(str(i)+'\n')
    env=dict(os.environ,PATH=str(binpath)+os.pathsep+os.environ['PATH'])
    def snapshot():
        return {str(x.relative_to(base)):hashlib.sha256(x.read_bytes()).digest()
                for x in base.rglob('*') if x.is_file()}
    def run(expected,flag='--check',extra=()):
        before=snapshot()
        result=subprocess.run(['bash',str(p/'prepare.sh'),'dsa0',flag,*extra],env=env,
                              capture_output=True,text=True,errors='replace')
        assert result.returncode==expected,(result.stdout,result.stderr)
        assert before==snapshot(),'准备脚本意外修改了设备状态'
    run(0)  # disabled状态默认只检查，不配置
    run(0,extra=('--fetch-size','2','--payload-size','30'))
    run(1,extra=('--fetch-size','2','--payload-size','32'))
    run(1,extra=('--fetch-size','-1'))
    (base/'state').write_text('enabled\n')
    for i in range(8): (base/f'wq0.{i}/state').write_text('enabled\n')
    run(0)
    run(0,'--apply')  # 匹配的已启用配置不会反复停用/启用
    run(1,'--apply',('--fetch-size','2','--payload-size','30'))  # 不自动重配活动设备
    (base/'wq0.3/size').write_text('8\n')
    run(1,'--apply')  # enabled但配置不符：拒绝自动重配
    (proc/'modules').write_text('dsa_gpu_test 28672 -1 - Unloading 0x0 (OE-)\n')
    run(2,'--apply')
    (proc/'modules').write_text('')
    (proc/'tainted').write_text('128\n')
    run(2,'--apply')
print('PASS: real prepare script read-only defaults, idempotent ready state, busy/mismatch/going/Oops rejection')
