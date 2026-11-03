"""Authenticated backup of this instance only, with SQLite snapshots and bounded restore."""
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import PurePosixPath
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC=b'DAFMOB1\n'
MAX_BYTES=512*1024*1024
MAX_FILES=25000
BASES=('data','appassets','bili')
RESERVED=('app-control.json','runtime-ready.json','brain.pid')

def key_for(password,salt):
    if len(password)<8: raise ValueError('备份密码至少 8 个字符')
    return hashlib.pbkdf2_hmac('sha256',password.encode(),salt,250000,dklen=32)

def create(home,destination,password):
    home=Path(home).resolve();destination=Path(destination)
    with tempfile.TemporaryDirectory(dir=home/'backups') as staging:
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w',zipfile.ZIP_DEFLATED) as archive:
            count=0;uncompressed=0
            for base in BASES:
                for file in (home/base).rglob('*'):
                    if file.is_symlink() or not file.is_file(): continue
                    relative=file.relative_to(home).as_posix()
                    if base=='data' and (file.name.endswith(('-wal','-shm','.log')) or file.name in RESERVED): continue
                    count+=1;uncompressed+=file.stat().st_size
                    if count>MAX_FILES or uncompressed>MAX_BYTES:raise ValueError('备份内容超过大小或数量上限')
                    if file.name=='bot.db':
                        snapshot=Path(staging)/'bot.db'
                        source=sqlite3.connect(str(file));target=sqlite3.connect(str(snapshot))
                        try: source.backup(target)
                        finally: target.close();source.close()
                        archive.write(snapshot,relative)
                    else: archive.write(file,relative)
        data=payload.getvalue()
        if len(data)>MAX_BYTES: raise ValueError('备份超过大小上限')
        salt=os.urandom(16);nonce=os.urandom(12)
        encrypted=AESGCM(key_for(password,salt)).encrypt(nonce,data,MAGIC)
        temporary=destination.with_name(destination.name+'.tmp')
        temporary.write_bytes(MAGIC+salt+nonce+encrypted)
        os.chmod(temporary,0o600);os.replace(temporary,destination)
    return {'ok':True,'size':destination.stat().st_size}

def restore(home,source,password):
    home=Path(home).resolve();raw=Path(source).read_bytes()
    if not len(MAGIC)+44<=len(raw)<=MAX_BYTES or not raw.startswith(MAGIC): raise ValueError('不是有效的手机版加密备份')
    pos=len(MAGIC);salt=raw[pos:pos+16];nonce=raw[pos+16:pos+28]
    try: payload=AESGCM(key_for(password,salt)).decrypt(nonce,raw[pos+28:],MAGIC)
    except Exception as error: raise ValueError('备份密码错误或文件损坏') from error
    with tempfile.TemporaryDirectory(dir=home/'backups') as temporary:
        staging=Path(temporary)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries=archive.infolist()
            if len(entries)>MAX_FILES or sum(e.file_size for e in entries)>MAX_BYTES: raise ValueError('备份解压超出限制')
            seen=set()
            for entry in entries:
                relative=PurePosixPath(entry.filename)
                parts=relative.parts
                if '\\' in entry.filename or ':' in entry.filename or not parts or parts[0] not in BASES or '..' in parts or relative.is_absolute(): raise ValueError('备份路径越界')
                if entry.filename in seen:raise ValueError('备份包含重复路径')
                seen.add(entry.filename)
                if parts[0]=='data' and (parts[-1] in RESERVED or parts[-1].endswith(('-wal','-shm'))):raise ValueError('备份包含禁止恢复的运行状态')
                if (entry.external_attr>>16)&0o170000==0o120000: raise ValueError('不接受符号链接')
                target=staging.joinpath(*parts)
                target.parent.mkdir(parents=True,exist_ok=True)
                if not entry.is_dir(): target.write_bytes(archive.read(entry));os.chmod(target,0o600)
        database=staging/'data'/'bot.db'
        if database.exists():
            connection=sqlite3.connect(str(database))
            try:
                if connection.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('备份数据库校验失败')
            finally: connection.close()
        configuration=staging/'data'/'config.json'
        if not configuration.is_file() or not (staging/'data'/'.secret_key').is_file():raise ValueError('备份缺少本实例配置或解密材料')
        config=json.loads(configuration.read_text(encoding='utf-8'))
        if not isinstance(config,dict):raise ValueError('备份配置格式无效')
        # Restored data never grants permission to resume sending automatically.
        config['mobile_autostart']=False
        configuration.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        for base in BASES:
            if (home/base).is_symlink():raise ValueError('恢复目标含符号链接')
            (staging/base).mkdir(exist_ok=True)
        for reserved in RESERVED[:2]:
            current=home/'data'/reserved
            if current.is_file():shutil.copyfile(current,staging/'data'/reserved)
        rollback=staging/'rollback';rollback.mkdir()
        moved=[];installed=[]
        try:
            for base in BASES:
                if (home/base).exists():os.replace(home/base,rollback/base);moved.append(base)
                os.replace(staging/base,home/base);installed.append(base)
        except BaseException:
            for base in reversed(installed):shutil.rmtree(home/base)
            for base in reversed(moved):os.replace(rollback/base,home/base)
            raise
    return {'ok':True,'restart_required':True}
