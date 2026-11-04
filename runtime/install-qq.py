"""Install unchanged official components into this app's private guest home only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile

BASE=Path('/home/mobile')
RUNTIME=Path(__file__).resolve().parent
OFFLINE=Path(os.environ.get('DAFEIYU_OFFLINE') or '/opt/offline')

def verified(path,component):
    if not path.is_file() or path.is_symlink():return False
    if path.stat().st_size!=int(component['size']):return False
    with path.open('rb')as stream:return hashlib.file_digest(stream,'sha256').hexdigest()==component['sha256']


def fetch(component):
    if component.get('file')=='napcat-shell.zip':
        bundled=OFFLINE/'archives/napcat.zip'
        if not verified(bundled,component):
            raise ValueError('内置 NapCat 归档缺失或校验失败；请安装完整安装包，不会从 GitHub 补下')
        return bundled
    if component.get('file')!='qq-arm64.deb':raise ValueError('不支持的客户端组件')
    origin=urllib.parse.urlsplit(str(component.get('url')or''))
    if (origin.scheme!='https' or origin.hostname!='qqdl.gtimg.cn' or origin.port not in(None,443)
            or origin.username is not None or origin.password is not None or origin.query or origin.fragment):
        raise ValueError('QQ 客户端只允许固定的腾讯官方 HTTPS 下载源')
    target=BASE/'downloads'/'qq-arm64.deb'
    if verified(target,component):return target
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.part')
    request=urllib.request.Request(component['url'],headers={'User-Agent':'DafeiyuMobile/1'})
    digest=hashlib.sha256();total=0
    with urllib.request.urlopen(request,timeout=60) as response, temporary.open('wb') as output:
        if not response.geturl().startswith('https://'): raise ValueError('下载不能降级为 HTTP')
        while True:
            chunk=response.read(256*1024)
            if not chunk: break
            total+=len(chunk)
            if total>min(400*1024*1024,int(component['size'])): raise ValueError('QQ 客户端超过固定大小上限')
            output.write(chunk);digest.update(chunk)
    if total!=int(component['size']) or digest.hexdigest()!=component['sha256']:
        temporary.unlink(missing_ok=True)
        raise ValueError('组件内容校验失败，未执行安装')
    temporary.replace(target)
    return target

def safe_zip(source,destination):
    with zipfile.ZipFile(source) as archive:
        if sum(i.file_size for i in archive.infolist())>600*1024*1024: raise ValueError('组件解压超限')
        for entry in archive.infolist():
            relative=Path(entry.filename.replace('\\','/'))
            if relative.is_absolute() or '..' in relative.parts: raise ValueError('组件路径越界')
            if (entry.external_attr>>16)&0o170000==0o120000: raise ValueError('组件不接受符号链接')
            target=destination/relative
            if relative.parts and relative.parts[0] in ('config','cache','logs') and target.exists():
                continue
            target.parent.mkdir(parents=True,exist_ok=True)
            if not entry.is_dir():
                with archive.open(entry) as stream,target.open('wb') as output: shutil.copyfileobj(stream,output)
                target.chmod(0o700 if entry.external_attr>>16&0o111 else 0o600)

def main():
    lock=json.loads((RUNTIME/'qq-components.lock.json').read_text(encoding='utf-8'))
    target=BASE/'Napcat'
    target.mkdir(parents=True,exist_ok=True)
    qq=fetch(lock['qq'])
    # dpkg-deb only extracts into this explicit private prefix; it does not install a system package.
    subprocess.run(['dpkg-deb','-x',str(qq),str(target)],check=True,timeout=180)
    package=target/'opt/QQ/resources/app/package.json'
    if not package.is_file(): raise ValueError('QQ 包结构与固定版本不符')
    shell=target/'opt/QQ/resources/app/app_launcher/napcat'
    shell.mkdir(parents=True,exist_ok=True)
    # Configs/cache belong to this new instance and are not overwritten by extraction.
    safe_zip(fetch(lock['napcat']),shell)
    content=json.loads(package.read_text(encoding='utf-8'))
    # Match upstream's CommonJS bootstrap; loading .mjs directly breaks some QQ launches.
    bootstrap=package.parent/'loadNapCat.js'
    bootstrap.write_text("const path=require('node:path');\nconst {pathToFileURL}=require('node:url');\nimport(pathToFileURL(path.join(__dirname,'app_launcher/napcat/napcat.mjs')).href);\n",encoding='utf-8')
    content['main']='./loadNapCat.js'
    package.write_text(json.dumps(content,ensure_ascii=False),encoding='utf-8')
    # Retain all upstream files, copyright and license notices; no original QQ app is changed.

if __name__=='__main__': main()
