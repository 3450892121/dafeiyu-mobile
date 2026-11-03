"""Phone-local CPU ASR plus explicitly configured compatible transcription API."""
import array
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
import wave
from mobile_policy import RUNTIME, initialize, config_for_mobile

_INSTALL=threading.Lock()


def install_model(home=None):
    home=Path(home or os.environ['DAFEIYU_HOME']);root=home/'asr'
    lock=json.loads((RUNTIME/'asr-model.lock.json').read_text(encoding='utf-8'))
    destination=root/lock['directory']
    with _INSTALL:
        if (destination/'model.int8.onnx').is_file() and (destination/'tokens.txt').is_file():return destination
        root.mkdir(parents=True,exist_ok=True)
        archive=root/'sensevoice.tar.bz2';partial=root/'sensevoice.part'
        valid=False
        if archive.is_file():
            with archive.open('rb')as f:valid=hashlib.file_digest(f,'sha256').hexdigest()==lock['sha256']
        if not valid:
            total=0;digest=hashlib.sha256()
            with urllib.request.urlopen(lock['url'],timeout=60)as response,partial.open('wb')as out:
                if not response.geturl().startswith('https://'):raise ValueError('模型下载不能降级为 HTTP')
                while True:
                    data=response.read(256*1024)
                    if not data:break
                    total+=len(data)
                    if total>180*1024*1024:raise ValueError('模型大小超限')
                    digest.update(data);out.write(data)
            if digest.hexdigest()!=lock['sha256']:partial.unlink(missing_ok=True);raise ValueError('语音模型校验失败')
            os.replace(partial,archive)
        with tempfile.TemporaryDirectory(dir=root)as temp:
            stage=Path(temp)
            with tarfile.open(archive,'r:bz2')as package:
                members=package.getmembers()
                if len(members)>200 or sum(m.size for m in members)>350*1024*1024:raise ValueError('模型归档超限')
                for member in members:
                    relative=Path(member.name)
                    if relative.is_absolute() or '..'in relative.parts or '\\'in member.name or not relative.parts or relative.parts[0]!=lock['directory']:raise ValueError('模型归档路径越界')
                    if not(member.isfile()or member.isdir()):raise ValueError('模型归档不接受链接')
                    target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True)
                    if member.isfile():
                        with package.extractfile(member)as source,target.open('wb')as out:shutil.copyfileobj(source,out)
                if not(stage/lock['directory']/'model.int8.onnx').is_file():raise ValueError('模型归档缺少识别文件')
            os.replace(stage/lock['directory'],destination)
        return destination


def recognize_local(wav,home):
    import numpy as np
    import sherpa_onnx
    model=Path(home)/'asr'/json.loads((RUNTIME/'asr-model.lock.json').read_text())['directory']
    if not(model/'model.int8.onnx').is_file():return {'text':'','segments':[],'error':'请在手机设置页下载 CPU 语音识别模型'}
    with wave.open(str(wav),'rb')as audio:
        if audio.getsampwidth()!=2 or audio.getnchannels()!=1 or audio.getframerate()!=16000:raise ValueError('识别音轨必须是 16kHz 单声道 PCM')
        length=audio.getnframes()/16000
        if length>1200:raise ValueError('音轨超过 20 分钟识别上限')
        recognizer=sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model/'model.int8.onnx'),tokens=str(model/'tokens.txt'),num_threads=2,use_itn=True,provider='cpu',debug=False)
        segments=[]
        # Bound each decoding allocation; long videos don't expand into one huge tensor.
        for offset in range(0,int(length)+1,25):
            samples=np.frombuffer(audio.readframes(25*16000),dtype=np.int16).astype(np.float32)/32768
            if not samples.size:break
            stream=recognizer.create_stream();stream.accept_waveform(16000,samples);recognizer.decode_stream(stream)
            text=re.sub(r'<\|[^|]+\|>','',stream.result.text).strip()
            if text:segments.append({'start':offset,'text':text})
    text=''.join(item['text']for item in segments)
    return {'text':text[:12000],'segments':segments[:200],'error':'' if text else '没有识别到人声'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('转写接口重定向被拒绝，避免发送接口密钥到其他地址')


def recognize_api(wav,cfg):
    selected=str(cfg.get('mobile_asr_model_id')or'')
    model=next((m for m in cfg.models()if str(m.get('id'))==selected),None)
    if not model:return {'text':'','segments':[],'error':'请先选择转写所用的 AI 接口'}
    base=str(cfg.get('mobile_asr_base_url')or model.get('base_url')or'').rstrip('/')
    url=urllib.parse.urlsplit(base)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:raise ValueError('转写接口需要有效的 HTTPS 地址')
    # Never send a selected key to a different host via an unrelated transcription URL.
    origin=urllib.parse.urlsplit(str(model.get('base_url')or''))
    if (url.hostname,url.port)!=(origin.hostname,origin.port):raise ValueError('转写地址必须与所选接口属于同一主机')
    audio=Path(wav).read_bytes()
    if len(audio)>24*1024*1024:raise ValueError('API 转写音轨超过 24 MB，请使用本机 CPU 识别')
    boundary='mobile-'+os.urandom(12).hex();chunks=[]
    for name,value in [('model',str(cfg.get('mobile_asr_model')or model.get('model')or'')),('response_format','verbose_json')]:
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n').encode())
    chunks.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),audio,('\r\n--'+boundary+'--\r\n').encode()])
    request=urllib.request.Request(base+'/audio/transcriptions',data=b''.join(chunks),headers={
        'Authorization':'Bearer '+str(model.get('api_key')or''),'Content-Type':'multipart/form-data; boundary='+boundary})
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=120)as response:data=json.loads(response.read(2*1024*1024))
    text=str(data.get('text')or'')[:12000]
    segments=[{'start':float(s.get('start')or 0),'text':str(s.get('text')or'')}for s in data.get('segments',[])if isinstance(s,dict)][:200]
    return {'text':text,'segments':segments,'error':''if text else'接口未返回转写内容'}


def transcribe(wav,timeout=420,cfg=None):
    cfg=cfg or config_for_mobile();home=os.environ['DAFEIYU_HOME']
    if not cfg.get('mobile_asr_enabled',True):return {'text':'','segments':[],'error':'声音转写已关闭'}
    try:
        if cfg.get('mobile_asr_provider')=='api':return recognize_api(wav,cfg)
        result=subprocess.run([sys.executable,str(RUNTIME/'mobile_asr.py'),'--decode',str(wav)],capture_output=True,timeout=timeout,env=os.environ.copy())
        if result.returncode!=0:return {'text':'','segments':[],'error':'本机转写未完成，请查看运行条件'}
        return json.loads(result.stdout.decode('utf-8'))
    except subprocess.TimeoutExpired:return {'text':'','segments':[],'error':'本机转写超时，已终止当前任务'}
    except Exception as error:return {'text':'','segments':[],'error':'转写失败：'+type(error).__name__}


def install_overrides():
    import video_reader
    import flow_media
    video_reader.transcribe_srt=lambda wav,asr_dir=None,timeout=420:transcribe(wav,timeout)
    def speech(bot,path):
        try:
            with bot.asr_sem,tempfile.TemporaryDirectory(dir=Path(os.environ['DAFEIYU_HOME'])/'data')as temp:
                wav=Path(temp)/'speech.wav'
                conversion=subprocess.run(['ffmpeg','-hide_banner','-nostdin','-y','-i',str(path),'-vn','-t','60','-ac','1','-ar','16000',str(wav)],capture_output=True,timeout=60)
                if conversion.returncode!=0:return ''
                return transcribe(wav,timeout=180,cfg=bot.cfg).get('text','')[:500]
        except Exception:return ''
    flow_media.MediaMixin._transcribe_file=speech


if __name__=='__main__':
    home=initialize()
    if len(sys.argv)==3 and sys.argv[1]=='--decode':print(json.dumps(recognize_local(sys.argv[2],home),ensure_ascii=False))
