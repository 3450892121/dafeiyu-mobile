"""Authenticated app-private control plane. The original bot/server is never contacted."""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from mobile_policy import initialize, config_for_mobile, RUNTIME, LAYOUT
from mobile_qq_status import public_status


class Control:
    def __init__(self, home=None):
        self.home=initialize(home)
        self.cfg=config_for_mobile()
        self.panel=None
        self.lock=threading.RLock()
        self.asr_job={'running':False,'message':'尚未下载 CPU 识别模型'}
        credentials=json.loads((self.home/'data/app-control.json').read_text(encoding='utf-8'))
        self.token=credentials['token']
        if not isinstance(self.token,str) or len(self.token)<32: raise ValueError('本机控制令牌无效')

    def ensure_panel(self):
        with self.lock:
            if self.panel and self.panel.poll() is None: return
            output=open(self.home/'data/mobile-panel.log','ab',buffering=0)
            try:
                self.panel=subprocess.Popen([sys.executable,str(RUNTIME/'mobile_console.py')],cwd=str(RUNTIME),
                    env=os.environ.copy(),stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                    start_new_session=(os.name!='nt'))
            finally: output.close()

    def panel_request(self, route, fields=None):
        self.ensure_panel()
        url='http://127.0.0.1:%d%s' % (LAYOUT['ports']['panel'],route)
        headers={'X-Admin-Token':str(self.cfg.get('admin_token') or '')}
        data=None
        if fields is not None:
            data=urllib.parse.urlencode(fields).encode()
            headers['Content-Type']='application/x-www-form-urlencoded'
        request=urllib.request.Request(url,data=data,headers=headers)
        with urllib.request.urlopen(request,timeout=30) as response:
            return json.loads(response.read(8*1024*1024))

    def status(self):
        result={'ok':True,'phase':'configured' if self.cfg.models() else 'needs_configuration',
                'model_configured':bool(self.cfg.models()),'owner_configured':bool(int(self.cfg.get('owner_qq') or 0)),
                'small_account_configured':bool(str(self.cfg.get('login_uin') or '')),
                'autostart':bool(self.cfg.get('mobile_autostart',False)),
                'qq_online':False,'brain_running':False,'panel_ready':False,
                'qq_connection':public_status('unknown')}
        try:
            state=self.panel_request('/api/state')
            result['panel_ready']=True
            # The local panel reports actual current QQ and brain state.
            components=state.get('components') or {}
            result['qq_online']=bool(components.get('login'))
            result['brain_running']=bool(components.get('brain'))
            result['connection_blocked']=bool(components.get('connection_blocked'))
            detail=state.get('qq_connection')
            result['qq_connection']=public_status(detail.get('state') if isinstance(detail,dict) else 'unknown')
        except Exception:
            result['phase']='starting_panel'
        return result

    def action(self, command):
        if command not in ('start_all','stop_all','start_napcat','stop_napcat','start_brain','stop_brain','restart_brain','refresh_qr','quick_login'):
            raise ValueError('不支持的本机操作')
        return self.panel_request('/api/action',{'cmd':command})

    def qq_diagnostics(self):
        patterns={
            '虚拟屏幕启动失败':('Xvfb failed to start','xvfb-run: error'),
            'QQ 用户目录被占用':('SingletonLock','ProcessSingleton','profile appears to be in use'),
            '运行文件缺失':('No such file or directory','cannot open shared object file'),
            '系统拒绝执行或访问':('Permission denied','Operation not permitted'),
            '图形进程不可用':("GPU process isn't usable",'GPU process exited unexpectedly'),
            '端口已被占用':('EADDRINUSE','Address already in use'),
        }
        text=''
        for name in ('napcat.log','runtime.log','mobile-panel.log'):
            file=self.home/'data'/name
            if file.is_file():
                with file.open('rb')as f:
                    f.seek(max(0,file.stat().st_size-32768))
                    text+=f.read(32768).decode('utf-8','replace')+'\n'
        return {'ok':True,'recent_flags':[label for label,terms in patterns.items() if any(term in text for term in terms)]}

    def save_basic(self, fields):
        allowed={'login_uin','owner_qq','bot_name','owner_name','allow_users','allow_groups','weather_home_city'}
        changes={}
        for key,value in fields.items():
            if key not in allowed: raise ValueError('不允许修改该字段')
            if key in ('owner_qq','login_uin'):
                value=str(value).strip()
                if not value.isdigit() or not 5<=len(value)<=12 or value.startswith('0'): raise ValueError('QQ 号格式无效')
                value=int(value) if key=='owner_qq' else value
            elif key in ('allow_users','allow_groups'):
                if not isinstance(value,list) or len(value)>1000: raise ValueError('名单格式无效')
                value=[int(v) for v in value if str(v).isdigit() and int(v)>0]
            else: value=str(value).strip()[:80]
            changes[key]=value
        if str(changes.get('login_uin',self.cfg.get('login_uin')))==str(changes.get('owner_qq',self.cfg.get('owner_qq'))):
            raise ValueError('机器人小号和管理员 QQ 必须不同')
        if changes.get('login_uin') and changes['login_uin']!=str(self.cfg.get('login_uin') or ''):
            if self.panel and self.panel.poll() is None:
                state=self.panel_request('/api/state').get('components',{})
                if state.get('napcat') or state.get('brain'):raise ValueError('先停止机器人，再更换本实例小号')
        self.cfg._reload_if_changed()
        with self.cfg._lock:
            self.cfg.data.update(changes)
            self.cfg.save()
        return {'ok':True}

    def urls(self):
        ports=LAYOUT['ports']
        panel='http://127.0.0.1:%d/?token=%s' % (ports['panel'],urllib.parse.quote(str(self.cfg.get('admin_token'))))
        qq=''
        config=self.home/'Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json'
        if config.exists():
            data=json.loads(config.read_text(encoding='utf-8'))
            qq='http://127.0.0.1:%d/webui/?token=%s' % (ports['qqWebui'],urllib.parse.quote(str(data.get('token') or '')))
        return {'ok':True,'panel':panel,'qq':qq}

    def asr_status(self):
        lock=json.loads((RUNTIME/'asr-model.lock.json').read_text(encoding='utf-8'))
        ready=(self.home/'asr'/lock['directory']/'model.int8.onnx').is_file()
        return {'ok':True,'local_ready':ready,'job':dict(self.asr_job),
                'settings':{key:self.cfg.get(key)for key in ('mobile_asr_enabled','mobile_asr_provider','mobile_asr_model_id','mobile_asr_base_url','mobile_asr_model')},
                'models':[{'id':m.get('id'),'name':m.get('name')or m.get('model')}for m in self.cfg.models()]}

    def asr_action(self,fields):
        if fields.get('action')=='download':
            with self.lock:
                if self.asr_job['running']:return {'ok':True,**self.asr_job}
                self.asr_job={'running':True,'message':'正在从官方来源下载并校验 CPU 识别模型……'}
                def download():
                    try:
                        from mobile_asr import install_model
                        install_model(self.home);self.asr_job={'running':False,'message':'CPU 识别模型已准备好'}
                    except Exception as error:self.asr_job={'running':False,'message':'模型准备未完成：'+type(error).__name__+'，可重试'}
                threading.Thread(target=download,daemon=True).start()
                return {'ok':True,**self.asr_job}
        if fields.get('action')!='settings':raise ValueError('识别设置操作无效')
        provider=fields.get('provider')
        if provider not in ('local','api'):raise ValueError('识别方式无效')
        model_id=str(fields.get('model_id')or'');base=str(fields.get('base_url')or'').strip().rstrip('/')
        if provider=='api':
            model=next((m for m in self.cfg.models()if str(m.get('id'))==model_id),None)
            if not model:raise ValueError('先在 AI 设置中添加转写接口，再选择该接口')
            supplied=urllib.parse.urlsplit(base or str(model.get('base_url')or''));origin=urllib.parse.urlsplit(str(model.get('base_url')or''))
            if supplied.scheme!='https' or not supplied.hostname or supplied.username or supplied.password or supplied.query or supplied.fragment or(supplied.hostname,supplied.port)!=(origin.hostname,origin.port):raise ValueError('转写接口需要同主机 HTTPS 地址')
        changes={'mobile_asr_provider':provider,'mobile_asr_enabled':bool(fields.get('enabled',True)),
                 'mobile_asr_model_id':model_id,'mobile_asr_model':str(fields.get('model')or'')[:200],'mobile_asr_base_url':base[:500]}
        self.cfg._reload_if_changed()
        with self.cfg._lock:self.cfg.data.update(changes);self.cfg.save()
        return {'ok':True}

    def privacy(self,fields=None):
        database=self.home/'data/bot.db'
        if not database.is_file():raise ValueError('管理服务尚未建好本实例数据库，请稍后重试')
        with sqlite3.connect(database,timeout=10)as connection:
            if fields is not None:
                if set(fields)!={'owner_public_sharing'} or not isinstance(fields['owner_public_sharing'],bool):raise ValueError('隐私设置格式无效')
                connection.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                    ('mobile_owner_public_sharing','1'if fields['owner_public_sharing']else'0'))
            row=connection.execute('SELECT v FROM settings WHERE k=?',('mobile_owner_public_sharing',)).fetchone()
        return {'ok':True,'owner_public_sharing':bool(row and row[0]=='1')}

    def stop(self):
        if self.panel and self.panel.poll() is None:
            try: self.action('stop_all')
            except Exception: pass
        if self.panel and self.panel.poll() is None:
            self.panel.terminate()
            try:self.panel.wait(timeout=20)
            except subprocess.TimeoutExpired:self.panel.kill()


def server(control,port=None):
    class Handler(BaseHTTPRequestHandler):
        def response(self,code,value,ctype='application/json; charset=utf-8',extra=None):
            data=(json.dumps(value,ensure_ascii=False) if ctype.startswith('application/json') else value).encode('utf-8')
            self.send_response(code)
            for key,val in {'Content-Type':ctype,'Content-Length':str(len(data)),'Cache-Control':'no-store',
                'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',**(extra or {})}.items():self.send_header(key,val)
            self.end_headers();self.wfile.write(data)

        def authorized(self):
            try:
                address=urllib.parse.urlsplit('http://'+self.headers.get('Host',''))
                if address.hostname not in ('127.0.0.1','localhost')or address.username or address.password or address.path or address.query or address.fragment or address.port!=self.server.server_port:return False
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host'):return False
                supplied=self.headers.get('Authorization','').removeprefix('Bearer ')
                if not supplied:
                    for cookie in self.headers.get('Cookie','').split(';'):
                        key,_,value=cookie.strip().partition('=')
                        if key=='mobile_control':supplied=value
                return bool(supplied) and hmac.compare_digest(supplied,control.token)
            except ValueError:return False

        def do_GET(self):
            if not self.authorized():return self.response(403,{'ok':False,'error':'未授权'})
            route=urllib.parse.urlsplit(self.path).path
            try:
                if route=='/api/status':return self.response(200,control.status())
                if route=='/api/qqdiagnostic':return self.response(200,control.qq_diagnostics())
                if route=='/api/urls':return self.response(200,control.urls())
                if route=='/api/asr':return self.response(200,control.asr_status())
                if route=='/api/privacy':return self.response(200,control.privacy())
                if route=='/api/basic':return self.response(200,{'ok':True,**{k:control.cfg.get(k) for k in ('login_uin','owner_qq','bot_name','owner_name','allow_users','allow_groups','weather_home_city')}})
                if route=='/api/log':
                    lines=[]
                    for name in ('bot.log','console.log','brain-process.log'):
                        file=control.home/'data'/name
                        if file.exists():
                            with file.open('rb')as f:f.seek(max(0,file.stat().st_size-20000));lines.extend(f.read().decode('utf-8','replace').splitlines()[-80:])
                    import re
                    text='\n'.join(lines)[-30000:]
                    text=re.sub(r'(?i)(token|api_key|secret|authorization)([\s:=]+)[^\s,;\"\']+',r'\1\2[已隐藏]',text)
                    return self.response(200,{'ok':True,'text':text})
                if route=='/':
                    return self.response(200,(RUNTIME/'mobile-home.html').read_text(encoding='utf-8'),'text/html; charset=utf-8',
                                         {'Set-Cookie':'mobile_control=%s; Path=/; HttpOnly; SameSite=Strict'%control.token})
                return self.response(404,{'ok':False})
            except Exception as error:return self.response(400,{'ok':False,'error':str(error)[:300]})

        def do_POST(self):
            if not self.authorized():return self.response(403,{'ok':False,'error':'未授权'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<4*1024*1024:raise ValueError('请求大小无效')
                fields=json.loads(self.rfile.read(size))
                if not isinstance(fields,dict):raise ValueError('请求格式无效')
                route=urllib.parse.urlsplit(self.path).path
                if route=='/api/action':result=control.action(str(fields.get('command','')))
                elif route=='/api/asr':result=control.asr_action(fields)
                elif route=='/api/privacy':result=control.privacy(fields)
                elif route=='/api/basic':result=control.save_basic(fields)
                elif route=='/api/backup':
                    from mobile_backup import create,restore
                    action=fields.get('action');password=str(fields.get('password',''))
                    file=control.home/'backups'/'transfer.dafm'
                    if action=='export':
                        result=create(control.home,file,password)
                    elif action=='import':
                        control.stop()
                        result=restore(control.home,file,password)
                        control.cfg=config_for_mobile()
                        control.cfg.set('mobile_autostart',False)
                        control.ensure_panel()
                    else:raise ValueError('备份操作无效')
                elif route=='/api/bili':
                    import bili_bridge
                    action=fields.get('action')
                    commands={'login':['auth','login'],'status':['auth','status'],'owner':['owner','set',str(fields.get('uid',''))]}
                    if action not in commands:raise ValueError('B站操作无效')
                    if action=='owner' and not str(fields.get('uid','')).isdigit():raise ValueError('UID 无效')
                    ok,data,error=bili_bridge.run(commands[action],timeout=60)
                    result={'ok':ok,'data':data,'error':error}
                    qr=control.home/'bili/login_qrcode.png'
                    if action=='login' and ok and qr.is_file() and qr.stat().st_size<1024*1024:
                        result['qr_data']='data:image/png;base64,'+base64.b64encode(qr.read_bytes()).decode()
                else:return self.response(404,{'ok':False})
                return self.response(200,result)
            except Exception as error:return self.response(400,{'ok':False,'error':str(error)[:300]})

        def log_message(self,*args):pass

    class Server(ThreadingHTTPServer):
        allow_reuse_address=True
        daemon_threads=True
    return Server(('127.0.0.1',LAYOUT['ports']['control'] if port is None else port),Handler)


if __name__=='__main__':
    controller=Control()
    controller.ensure_panel()
    http=server(controller)
    def stop(*_):
        controller.stop()
        threading.Thread(target=http.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:http.serve_forever()
    finally:controller.stop();http.server_close()
