"""Pin the private QQ kernel directory; inspect metadata, never copy login tickets."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request


def candidates(home, guest_legacy=None):
    home=Path(home).resolve()
    roots={'xdg':home/'.config/QQ','profile':home/'qq-profile'}
    if guest_legacy is None and os.name!='nt':guest_legacy=Path('/root/.config/QQ')
    if guest_legacy is not None:roots['guest_legacy']=Path(guest_legacy)
    for name,root in roots.items():
        # Only these app-private guest paths are allowed; a symlink cannot redirect them.
        if root.resolve()!=root.absolute():raise ValueError('QQ 登录目录包含链接，未改变现有资料')
    return roots


def has_login_database(root):
    return any((root/part).is_file() for part in ('global/nt_db/login.db','nt_qq/global/nt_db/login.db'))


def session_root(home,guest_legacy=None):
    home=Path(home).resolve();roots=candidates(home,guest_legacy)
    marker=home/'data/qq-session-directory.json'
    if marker.is_file():
        data=json.loads(marker.read_text(encoding='utf-8'))
        name=data.get('directory')
        if name not in roots:raise ValueError('QQ 登录目录记录无效，未重建或覆盖资料')
    else:
        found=[name for name,root in roots.items() if has_login_database(root)]
        if len(found)>1:raise ValueError('发现多个 QQ 登录目录，未自动切换或合并，请先核对')
        name=found[0] if found else 'xdg'
        marker.parent.mkdir(parents=True,exist_ok=True)
        temp=marker.with_suffix('.tmp')
        temp.write_text(json.dumps({'schemaVersion':1,'directory':name})+'\n',encoding='utf-8')
        os.chmod(temp,0o600);os.replace(temp,marker)
    roots[name].mkdir(parents=True,exist_ok=True)
    return roots[name]


def qq_environment(home,workdir,environ=None,guest_legacy=None):
    env=dict(os.environ if environ is None else environ)
    env.update({'HOME':str(home),'LANG':'C.UTF-8',
                'NAPCAT_QQ_DATA_DIR':str(session_root(home,guest_legacy)),
                'NAPCAT_WORKDIR':str(workdir)})
    return env


def saved_login_status(home,uin,port):
    """Read-only backend probe; never returns an upstream payload or login URL."""
    result={'available':False,'saved_account':False}
    config=Path(home)/'Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json'
    try:
        token=str(json.loads(config.read_text(encoding='utf-8')).get('token')or'')
        if not token:return result
        def post(route,body,credential=''):
            request=urllib.request.Request('http://127.0.0.1:%d/api/'%port+route,
                data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+credential})
            with urllib.request.urlopen(request,timeout=6)as response:
                data=response.read(65537)
            if len(data)>65536:raise ValueError('QQ status response too large')
            return json.loads(data)
        auth=post('auth/login',{'hash':hashlib.sha256((token+'.napcat').encode()).hexdigest()})
        credential=(auth.get('data')or{}).get('Credential')
        if not credential:return result
        reply=post('QQLogin/GetQuickLoginList',{},credential)
        values=reply.get('data')
        if reply.get('code')!=0 or not isinstance(values,list):return result
        result['available']=True
        result['saved_account']=any(str(value)==str(uin) for value in values if isinstance(value,(str,int)))
        return result
    except Exception:return result
