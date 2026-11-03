"""Tests only inside this workspace, with all real transports/process launching denied."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/'build'/'runtime-tests';BUILD.mkdir(parents=True,exist_ok=True)
os.environ['TEMP']=os.environ['TMP']=str(BUILD)
tempfile.tempdir=str(BUILD)
os.environ['DAFEIYU_HOME']=str(BUILD/'instance')
os.environ['BOT_TEST_PATH']=str(ROOT/'runtime'/'core')
os.environ['CONSOLE_TEST_PATH']=str(ROOT/'runtime'/'console_base.py')
os.environ['PYTHONIOENCODING']='utf-8'
os.chdir(BUILD)
sys.path[:0]=[str(ROOT/'runtime'),str(ROOT/'runtime'/'core')]
from mobile_policy import initialize
initialize()
fixture_ports=set()
original_bind=socket.socket.bind
def fixture_bind(sock,address):
    if not isinstance(address,tuple)or address[0]not in('127.0.0.1','localhost','::1')or address[1]!=0:
        raise RuntimeError('测试只允许创建临时回环假服务')
    result=original_bind(sock,address)
    fixture_ports.add(sock.getsockname()[1])
    return result
socket.socket.bind=fixture_bind

def guard(event,args):
    if event=='socket.connect':
        address=args[1]
        if not isinstance(address,tuple)or address[0]not in('127.0.0.1','localhost','::1'):raise RuntimeError('隔离测试禁止连接外部服务')
        if address[1]not in fixture_ports:raise RuntimeError('隔离测试只允许连接本轮创建的假服务，禁止接入任何已有服务')
    if event=='subprocess.Popen':
        command=args[1]
        allowed=[sys.executable,str(ROOT/'tools/run-runtime-tests.py'),'--sqlite-stress']
        if command!=allowed and command!=subprocess.list2cmdline(allowed):raise RuntimeError('隔离测试禁止启动外部进程')
    if event in('os.system','os.exec','os.posix_spawn'):raise RuntimeError('隔离测试禁止启动外部进程')
    if event=='open':
        name,mode,flags=args
        writing=(isinstance(mode,str)and any(c in mode for c in'wa+x'))or(isinstance(flags,int)and bool(flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)))
        if writing and isinstance(name,(str,bytes,os.PathLike)):
            target=Path(os.fsdecode(name)).resolve()
            if not target.is_relative_to(ROOT):raise RuntimeError('测试写入越出本项目：'+str(target))
sys.addaudithook(guard)
if '--sqlite-stress'in sys.argv:
    sys.path.insert(0,str(ROOT/'tests/ported'))
    from test_sqlite_threading import startup_stress
    startup_stress()
    sys.exit(0)
suite=unittest.TestSuite()
for directory in('ported','mobile'):
    if (ROOT/'tests'/directory).is_dir():suite.addTests(unittest.TestLoader().discover(str(ROOT/'tests'/directory),pattern='test_*.py',top_level_dir=str(ROOT/'tests'/directory)))
result=unittest.TextTestRunner(verbosity=1).run(suite)
report={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'skip_reasons':[str(reason)for test,reason in result.skipped],'successful':result.wasSuccessful()}
(BUILD/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
sys.exit(0 if result.wasSuccessful()else 1)
