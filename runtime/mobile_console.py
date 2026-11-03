"""One local instance, one private QQ profile, bounded reconnect and no server control."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from mobile_policy import initialize, config_for_mobile, RUNTIME, LAYOUT
from mobile_qq_status import identity_state, public_status

HOME = initialize()
CFG = config_for_mobile()
import console_base as panel

QQ_ROOT = HOME / 'Napcat' / 'opt' / 'QQ'
NAPCAT = QQ_ROOT / 'resources' / 'app' / 'app_launcher' / 'napcat'
panel.NAPCAT_DIR = str(NAPCAT)
panel.PROFILE_DIR = str(HOME / 'qq-profile')
panel.BRAIN = str(RUNTIME / 'mobile_brain.py')
panel.PROJECT_ROOT = str(RUNTIME)
# Omitted desktop backends must never be probed from this independent phone app.
panel._cover_online=lambda cfg:False
panel._cover_health=lambda:{'online':False,'running':False,'supported':False}
_BaseSupervisor = panel.Supervisor


def private_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.chmod(temporary,0o600);os.replace(temporary,path)


def qq_configs(cfg):
    uin=str(cfg.get('login_uin') or '').strip()
    if not re.fullmatch(r'[1-9][0-9]{4,11}',uin):
        raise ValueError('先填写本项目新小号的 QQ 号，再扫码登录')
    if uin == str(cfg.get('owner_qq') or ''):
        raise ValueError('机器人小号与管理员账号必须不同')
    ports=LAYOUT['ports']
    onebot={
        'network':{
            'httpServers':[{'name':'mobile-http','enable':True,'host':'127.0.0.1','port':ports['onebot'],
                            'enableCors':False,'enableWebsocket':False,'token':cfg.get('onebot_token'),'messagePostFormat':'array'}],
            'httpClients':[{'name':'mobile-events','enable':True,'url':'http://127.0.0.1:%d/' % ports['webhook'],
                            'token':cfg.get('onebot_token'),'messagePostFormat':'array','reportSelfMessage':False}],
            'websocketServers':[],'websocketClients':[]},
        'musicSignUrl':'','enableLocalFile2Url':True,'parseMultMsg':True,
    }
    private_json(NAPCAT/'config'/('onebot11_'+uin+'.json'),onebot)
    # Preserve new-instance tokens and login state; no rotation on reconnect.
    webui=NAPCAT/'config'/'webui.json'
    old={}
    if webui.exists():
        try: old=json.loads(webui.read_text(encoding='utf-8'))
        except ValueError: raise ValueError('QQ 管理配置损坏，未覆盖原文件')
    old.update({'host':'127.0.0.1','port':ports['qqWebui']})
    if not old.get('token'):
        import secrets
        old['token']=secrets.token_urlsafe(32)
    private_json(webui,old)
    return uin


class MobileSupervisor(_BaseSupervisor):
    def __init__(self):
        self._qq_process=None
        self._brain_process=None
        self._connection_blocked=False
        self._had_connection=False
        self._connection_failures=0
        self._closing=False
        self._identity_lock=threading.Lock()
        self._identity_checked_at=None
        self._identity_expected=''
        self._identity_state='unknown'
        super().__init__()
        self.user_stopped=not bool(self.cfg.get('mobile_autostart',False))

    def qq_pids(self):
        return [self._qq_process.pid] if self._qq_process and self._qq_process.poll() is None else []

    def brain_pids(self):
        return [self._brain_process.pid] if self._brain_process and self._brain_process.poll() is None else []

    def login_info(self):
        from onebot import OneBot
        try:
            client=OneBot(self.cfg.get('onebot_api'),self.cfg.get('onebot_token'),timeout=3)
            status=client.call('get_status') or {}
            if status.get('online') is not True:
                if self._had_connection and status.get('online') is False:
                    self._stop(self._brain_process)
                return None
            info=client.get_login_info()or{}
            if str(info.get('user_id'))!=str(self.cfg.get('login_uin')or''):
                self._remember_uin(info.get('user_id'));return None
            self._identity_state='connected'
            return info
        except Exception:
            # A different scanned account has no configured OneBot port. Check its
            # live WebUI identity independently instead of treating it as logged out.
            self._probe_qq_identity()
            return None

    def _probe_qq_identity(self):
        with self._identity_lock:
            expected=str(self.cfg.get('login_uin') or '')
            now=time.monotonic()
            if self._identity_checked_at is not None and now-self._identity_checked_at<30 and self._identity_expected==expected:
                return self._identity_state
            self._identity_checked_at=now
            self._identity_expected=expected
            if not self.napcat_running():
                self._identity_state='stopped'
            else:
                response=self._webui_post('/api/QQLogin/GetQQLoginInfo',{})
                self._identity_state=identity_state(response,expected)
            if self._identity_state=='identity_mismatch':
                self.block_connection(public_status(self._identity_state)['message'])
            return self._identity_state

    def _qq_output(self,process):
        path=HOME/'data/napcat.log'
        if path.exists()and path.stat().st_size>4*1024*1024:
            os.replace(path,path.with_suffix('.previous.log'))
        with path.open('a',encoding='utf-8')as log:
            for line in iter(process.stdout.readline,b''):
                text=line.decode('utf-8','replace')
                safe=re.sub(r'(?i)(token|credential|authorization|cookie)([\s:=]+)[^\s,;]+',r'\1\2[已隐藏]',text)
                log.write(safe);log.flush()
                if re.search(r'风控|被踢下线|登录受限|安全验证|异常登录|账号冻结',text):
                    self.block_connection('QQ 提示登录限制或验证，已暂停大脑；请手动检查，不自动重登')

    def start_napcat(self):
        with self.lock:
            if self.napcat_running(): return True
            if self._closing: return False
            try:
                uin=qq_configs(self.cfg)
                executable=QQ_ROOT/'qq'
                if not executable.is_file(): raise ValueError('QQ 运行组件未初始化')
                self._connection_blocked=False
                self._had_connection=False
                self._identity_checked_at=None
                self._identity_state='unknown'
                self.user_stopped=False
                self._qq_process=subprocess.Popen([
                    'xvfb-run','-a',str(executable),'--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
                    '--user-data-dir='+str(HOME/'qq-profile'),'-q',uin],
                    cwd=str(NAPCAT),env={**os.environ,'HOME':str(HOME),'LANG':'C.UTF-8'},
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
                threading.Thread(target=self._qq_output,args=(self._qq_process,),daemon=True).start()
                return True
            except Exception as error:
                self._log_console('QQ 启动失败：'+str(error))
                return False

    def start_brain(self):
        with self.lock:
            if self.brain_running(): return True
            if self._connection_blocked: return False
            info=self.login_info()
            if not info or str(info.get('user_id'))!=str(self.cfg.get('login_uin')):
                self._log_console('QQ 身份尚未确认，大脑未启动')
                return False
            if not self.cfg.models() or not int(self.cfg.get('owner_qq') or 0):
                self._log_console('先配置本实例管理员和 AI 接口')
                return False
            log=open(HOME/'data'/'brain-process.log','ab',buffering=0)
            try:
                self._brain_process=subprocess.Popen([sys.executable,str(RUNTIME/'mobile_brain.py')],
                    cwd=str(RUNTIME),env=os.environ.copy(),stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,start_new_session=True)
            finally: log.close()
            self.user_stopped=False
            return True

    @staticmethod
    def _stop(process):
        if not process or process.poll() is not None: return True
        try:
            os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=16)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=4)
        except ProcessLookupError: pass
        return process.poll() is not None

    def stop_brain(self):
        self.user_stopped=True
        return self._stop(self._brain_process)

    def stop_napcat(self):
        self.user_stopped=True
        brain=self._stop(self._brain_process)
        return self._stop(self._qq_process) and brain

    def _poll_connection(self):
        if self.user_stopped or self._connection_blocked:return
        info=self.login_info() if self.napcat_running() else None
        if info:
            if str(info.get('user_id'))!=str(self.cfg.get('login_uin')):
                self.block_connection('当前登录号与本实例新小号不符，已暂停；请手动核对');return
            self._had_connection=True;self._connection_failures=0
            if not self.brain_running():self.start_brain()
        elif self._had_connection:
            self._connection_failures+=1
            if self._connection_failures>=3:
                self.block_connection('QQ 连接已中断，已暂停主动动作；请在登录页检查后手动恢复')

    def _auto_start(self):
        while not self._closing:
            time.sleep(8)
            try:self._poll_connection()
            except Exception:self._log_console('连接状态暂时无法读取，等待下一次有限检查')
            # Only observe QQ's own transport recovery; never refresh a QR, force login or change identity.

    def quick_login(self):
        self._connection_blocked=False
        self._had_connection=False
        self.user_stopped=False
        return super().quick_login()

    def _remember_uin(self, uin):
        # Observing another account never grants permission to replace the selected small account.
        if str(uin) != str(self.cfg.get('login_uin') or ''):
            self._identity_state='identity_mismatch'
            self.block_connection('登录身份不匹配，未修改本实例的小号配置')

    def block_connection(self,reason):
        if getattr(self,'_connection_blocked',False):return
        self._connection_blocked=True
        self.cfg.set('mobile_autostart',False)
        self._stop(self._brain_process)
        self._log_console(reason)

    def state(self):
        result=super().state()
        result['components']['connection_blocked']=self._connection_blocked
        if self._connection_blocked: result['components']['login']=False
        state=self._identity_state
        if self._connection_blocked and state!='identity_mismatch':state='paused'
        elif result['components'].get('login'):state='connected'
        elif state=='connected':state='unknown'
        result['qq_connection']=public_status(state)
        return result

    def action(self, cmd):
        aliases={'start_napcat':'napcat_start','stop_napcat':'napcat_stop','start_brain':'brain_start','stop_brain':'brain_stop','restart_brain':'brain_restart'}
        cmd=aliases.get(cmd,cmd)
        if cmd in ('start_all','napcat_start','brain_start','brain_restart','napcat_restart','quick_login'):
            self.cfg.set('mobile_autostart',True)
            self._connection_blocked=False
            self.user_stopped=False
            self._connection_failures=0
        if cmd in ('stop_all','napcat_stop','brain_stop'):
            self.cfg.set('mobile_autostart',False)
        if cmd.startswith('local_'):
            self._log_console('手机版使用设置中的联网 AI，不启动原电脑模型服务')
            return False
        if cmd=='start_all':
            # A QR/login wait is an intermediate state, never a fabricated online result.
            started=self.start_napcat()
            if started and self.login_info(): self.start_brain()
            return started
        return super().action(cmd)


def main():
    instances=[]
    class OwnedSupervisor(MobileSupervisor):
        def __init__(self):
            super().__init__();instances.append(self)
    panel.Supervisor=OwnedSupervisor
    def terminate(*_): raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGINT,terminate)
    try: panel.main()
    finally:
        for supervisor in instances:
            supervisor._closing=True
            supervisor.stop_napcat()

if __name__=='__main__': main()
