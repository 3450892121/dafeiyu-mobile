# -*- coding: utf-8 -*-
"""看视频：下载（B站/抖音/快手）→ 抽帧/抽音轨 → 交给上层做语音转写和看图。

只负责“拿到视频和处理素材”，不碰模型调用；安全边界由调用方（候选链接）保证。
下载引擎照搬番茄工具箱的做法：yt-dlp + 抖音匿名解析（ttwid + iesdouyin）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse

MAX_MB = 60
MAX_SEC = 600
MAX_FRAMES = 14
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"
YTDLP = shutil.which("yt-dlp") or "yt-dlp"

DOUYIN_MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36")
DOUYIN_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
BILI_REFERER = "https://www.bilibili.com/"

VIDEO_HOSTS = {
    "bilibili.com": "bilibili", "b23.tv": "bilibili",
    "douyin.com": "douyin", "iesdouyin.com": "douyin",
    "kuaishou.com": "kuaishou",
}
DOUYIN_MEDIA_HOSTS = ("www.douyin.com", "aweme.snssdk.com", "douyinvod.com", "byteimg.com", "douyin.com")


class VideoError(Exception):
    pass


def _host(url):
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""


def platform_of(url):
    host = _host(url)
    for suffix, name in VIDEO_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return ""


def is_video_url(url):
    return bool(platform_of(url))


def _douyin_media_ok(url):
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in DOUYIN_MEDIA_HOSTS)


def _http_get(url, headers=None, timeout=20, max_bytes=2 * 1024 * 1024):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(max_bytes)
        hdrs = {}
        try:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
        except Exception:
            pass
        return {"status": getattr(r, "status", 0), "url": getattr(r, "url", url),
                "headers": hdrs, "body": body}


def _http_post_json(url, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": DOUYIN_DESKTOP_UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {"status": getattr(r, "status", 0), "body": r.read(64 * 1024),
                "set_cookie": r.headers.get_all("Set-Cookie") or []}


def _douyin_id(url):
    m = re.search(r"/(?:video|note)/(\d{15,25})(?:[/?#]|$)", str(url or ""))
    if m:
        return m.group(1)
    try:
        q = urlparse(str(url or "")).query
        for key in ("modal_id", "aweme_id", "item_id"):
            m = re.search(key + r"=(\d{15,25})", q)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _douyin_ttwid():
    try:
        r = _http_post_json("https://ttwid.bytedance.com/ttwid/union/register/", {
            "region": "cn", "aid": 1768, "needFid": False, "service": "www.douyin.com",
            "migrate_info": {"ticket": "", "source": "node"}, "cbUrlProtocol": "https", "union": True,
        })
        for line in r.get("set_cookie") or []:
            pair = str(line).split(";")[0].strip()
            if pair.lower().startswith("ttwid="):
                return pair
    except Exception:
        pass
    return ""


def _find_douyin_item(value, vid):
    if isinstance(value, dict):
        if str(value.get("aweme_id") or "") == str(vid) and value.get("video"):
            return value
        for child in value.values():
            got = _find_douyin_item(child, vid)
            if got:
                return got
    elif isinstance(value, list):
        for child in value:
            got = _find_douyin_item(child, vid)
            if got:
                return got
    return None


def _douyin_info(url):
    vid = _douyin_id(url)
    if not vid:
        r = _http_get(url, {"User-Agent": DOUYIN_MOBILE_UA})
        vid = _douyin_id(r.get("url") or "")
        if not vid:
            m = re.search(rb'"(?:aweme_id|itemId)"\s*:\s*"(\d{15,25})"', r.get("body") or b"")
            vid = m.group(1).decode() if m else ""
    if not vid:
        raise VideoError("没从抖音链接里认出视频编号")
    ttwid = _douyin_ttwid()
    headers = {"User-Agent": DOUYIN_MOBILE_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if ttwid:
        headers["Cookie"] = ttwid
    share = _http_get("https://www.iesdouyin.com/share/video/%s/" % vid, headers, timeout=25)
    html = (share.get("body") or b"").decode("utf-8", "replace")
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{[\s\S]*?\})\s*</script>", html)
    if not m:
        raise VideoError("抖音游客页没给数据（可能要登录）")
    try:
        data = json.loads(m.group(1))
    except Exception:
        raise VideoError("抖音游客页数据解析失败")
    item = _find_douyin_item(data, vid)
    if not item:
        raise VideoError("抖音游客页没找到这条视频")
    play = (item.get("video") or {}).get("play_addr") or {}
    uri = play.get("uri") or ""
    if not uri:
        for rate in (item.get("video") or {}).get("bit_rate") or []:
            uri = ((rate.get("play_addr") or {}).get("uri") or "")
            if uri:
                break
    if not uri:
        raise VideoError("抖音没给播放地址")
    media = "https://aweme.snssdk.com/aweme/v1/play/?video_id=%s&ratio=480p&line=0" % uri
    if not _douyin_media_ok(media):
        raise VideoError("抖音播放地址没通过安全检查")
    title = re.sub(r"\s+", " ", str(item.get("desc") or "")).strip()[:80] or "抖音视频"
    return {"id": vid, "title": title, "url": media,
            "headers": {"User-Agent": DOUYIN_MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}}


def _stream_video(url, dest, headers):
    req = urllib.request.Request(url, headers=headers)
    total = 0
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = str(r.headers.get("Content-Type") or "").lower()
        if ctype and not (ctype.startswith("video/") or "octet-stream" in ctype):
            raise VideoError("抖音返回的不是视频（%s）" % ctype[:30])
        clen = int(r.headers.get("Content-Length") or 0)
        if clen > MAX_MB * 1024 * 1024:
            raise VideoError("视频超过 %dMB" % MAX_MB)
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MB * 1024 * 1024:
                    raise VideoError("视频超过 %dMB" % MAX_MB)
                f.write(chunk)
    if total < 10 * 1024:
        raise VideoError("抖音返回的视频是空的")


_BILI_COOKIE = {"at": 0, "cookie": ""}


def _http_json(url, headers=None, timeout=15):
    r = _http_get(url, headers, timeout=timeout, max_bytes=4 * 1024 * 1024)
    return json.loads((r.get("body") or b"{}").decode("utf-8", "replace"))


def _bili_cookie():
    """B站匿名指纹 Cookie（buvid3/buvid4）；不带它 view/playurl 会被 412 挡。"""
    now = time.time()
    if _BILI_COOKIE["cookie"] and now - _BILI_COOKIE["at"] < 86400:
        return _BILI_COOKIE["cookie"]
    try:
        d = _http_json("https://api.bilibili.com/x/frontend/finger/spi",
                       {"User-Agent": DOUYIN_DESKTOP_UA, "Referer": BILI_REFERER})
        b3 = str(((d.get("data") or {}).get("b_3")) or "")
        b4 = str(((d.get("data") or {}).get("b_4")) or "")
        ck = ("buvid3=%s; buvid4=%s" % (b3, b4)) if b3 else ""
        if ck:
            _BILI_COOKIE.update({"at": now, "cookie": ck})
        return ck
    except Exception:
        return ""


def _bili_bvid(url):
    m = re.search(r"(BV[0-9A-Za-z]{10,12})", str(url or ""))
    if m:
        return m.group(1)
    try:
        r = _http_get(url, {"User-Agent": DOUYIN_DESKTOP_UA}, timeout=20)
        m = re.search(r"(BV[0-9A-Za-z]{10,12})", str(r.get("url") or ""))
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _bilibili_download(url, workdir):
    bvid = _bili_bvid(url)
    if not bvid:
        raise VideoError("没认出 B站视频号")
    ck = _bili_cookie()
    h = {"User-Agent": DOUYIN_DESKTOP_UA, "Referer": BILI_REFERER}
    if ck:
        h["Cookie"] = ck
    view = _http_json("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid, h)
    if view.get("code") != 0:
        raise VideoError("B站接口返回 %s" % (view.get("message") or "错误"))
    data = view.get("data") or {}
    cid = data.get("cid")
    title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:80] or "B站视频"
    if not cid:
        raise VideoError("B站没给 cid")
    media = ""
    for qn in (32, 16):
        pu = _http_json(
            "https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%d&fnval=0"
            "&platform=html5&high_quality=1" % (bvid, cid, qn), h)
        durl = (pu.get("data") or {}).get("durl") or []
        if durl:
            media = str(durl[0].get("url") or "")
            break
    if not media:
        raise VideoError("B站没给公开下载地址")
    dest = os.path.join(workdir, "bili.mp4")
    _stream_video(media, dest, {"User-Agent": DOUYIN_DESKTOP_UA, "Referer": BILI_REFERER,
                                "Cookie": ck})
    return {"path": dest, "title": title}


def _ytdlp_download(url, workdir):
    if not os.path.exists(YTDLP):
        raise VideoError("没装 yt-dlp")
    fmt = "bv*[ext=mp4][height<=480]+ba[ext=m4a]/b[height<=480]/b"
    args = [YTDLP, "--no-config", "--no-playlist", "--no-warnings", "--no-progress", "--newline",
            "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
            "--concurrent-fragments", "4", "--max-filesize", "%dM" % MAX_MB,
            "-f", fmt, "--merge-output-format", "mp4",
            "--ffmpeg-location", os.path.dirname(FFMPEG),
            "-o", os.path.join(workdir, "%(id)s.%(ext)s"),
            "--print", "after_move:__FINAL_FILE__%(filepath)s",
            "--print", "after_move:__TITLE__%(title)s"]
    if platform_of(url) == "bilibili":
        args += ["--add-header", "Referer:" + BILI_REFERER]
        ck = _bili_cookie()
        if ck:
            args += ["--add-header", "Cookie:" + ck]
    args += ["--", url]
    try:
        r = subprocess.run(args, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise VideoError("下载超时（超过 5 分钟）")
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    final, title = "", ""
    for line in out.splitlines():
        if line.startswith("__FINAL_FILE__"):
            final = line[len("__FINAL_FILE__"):].strip()
        elif line.startswith("__TITLE__"):
            title = line[len("__TITLE__"):].strip()[:80]
    if not final or not os.path.exists(final):
        files = []
        for name in os.listdir(workdir):
            if name.lower().endswith((".mp4", ".mkv", ".webm", ".flv", ".mov")):
                files.append(os.path.join(workdir, name))
        final = files[0] if files else ""
    if not final or not os.path.exists(final):
        msg = (err.strip().splitlines() or ["下载失败"])[-1][:120]
        if "larger than max-filesize" in err:
            msg = "视频超过 %dMB" % MAX_MB
        raise VideoError(msg)
    if os.path.getsize(final) > MAX_MB * 1024 * 1024:
        try:
            os.remove(final)
        except Exception:
            pass
        raise VideoError("视频超过 %dMB" % MAX_MB)
    return {"path": final, "title": title or os.path.splitext(os.path.basename(final))[0][:80]}


def download(url, workdir, log=None):
    """把链接下成视频文件；返回 {path,title}，失败抛 VideoError。"""
    os.makedirs(workdir, exist_ok=True)
    plat = platform_of(url)
    if plat not in ("bilibili", "douyin", "kuaishou"):
        raise VideoError("只支持 B站/抖音/快手 的视频链接")
    if plat == "bilibili":
        try:
            return _bilibili_download(url, workdir)
        except VideoError as e:
            if log:
                log("[video] B站接口失败：%s，改试 yt-dlp" % e)
        except Exception as e:
            if log:
                log("[video] B站接口异常 %r，改试 yt-dlp" % (e,))
    if plat == "douyin":
        try:
            info = _douyin_info(url)
            dest = os.path.join(workdir, "douyin.mp4")
            _stream_video(info["url"], dest, info.get("headers") or {})
            if log:
                log("[video] 抖音匿名解析成功 id=%s" % info["id"])
            return {"path": dest, "title": info["title"]}
        except VideoError as e:
            if log:
                log("[video] 抖音匿名解析失败：%s，改试 yt-dlp" % e)
        except Exception as e:
            if log:
                log("[video] 抖音匿名解析异常 %r，改试 yt-dlp" % (e,))
    got = _ytdlp_download(url, workdir)
    return got


def probe(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, timeout=30)
        data = json.loads((r.stdout or b"{}").decode("utf-8", "replace"))
    except Exception:
        return {"ok": False}
    streams = data.get("streams") or []
    vid = [s for s in streams if s.get("codec_type") == "video"]
    aud = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = data.get("format") or {}
    try:
        dur = float(fmt.get("duration") or (vid[0].get("duration") if vid else 0) or 0)
    except Exception:
        dur = 0
    return {"ok": bool(vid), "duration": dur, "has_video": bool(vid), "has_audio": bool(aud),
            "width": int(vid[0].get("width") or 0) if vid else 0,
            "height": int(vid[0].get("height") or 0) if vid else 0}


def extract_audio(path, wav, limit_sec=MAX_SEC):
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-t", str(limit_sec),
           "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    return r.returncode == 0 and os.path.exists(wav) and os.path.getsize(wav) > 1000


def extract_frames(path, duration, outdir, limit_sec=MAX_SEC):
    os.makedirs(outdir, exist_ok=True)
    dur = min(float(duration or 0), float(limit_sec))
    if dur <= 0:
        dur = 30
    n = 8 if dur <= 120 else (12 if dur <= 300 else MAX_FRAMES)
    times = [max(1.0, dur * (i + 0.5) / n) for i in range(n)]
    out = []
    for i, t in enumerate(times):
        fp = os.path.join(outdir, "f%02d.jpg" % i)
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-ss", "%.2f" % t, "-i", path,
               "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", fp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            continue
        if r.returncode == 0 and os.path.exists(fp) and os.path.getsize(fp) > 2000:
            out.append({"time": int(t), "path": fp})
    return out


def _parse_srt(text):
    segs = []
    cur = None
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", s)
        if m:
            start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            cur = {"start": start, "text": ""}
            segs.append(cur)
            continue
        if s.isdigit() or s.startswith("["):
            continue
        if cur is not None:
            cur["text"] = (cur["text"] + s).strip()
    return [x for x in segs if x["text"]]


def transcribe_srt(wav, asr_dir, timeout=420):
    """整段转写，带时间戳；返回 {text, segments}。没装 ASR 或失败返回空。"""
    exe = os.path.join(asr_dir, "llama-funasr-sensevoice")
    if not os.path.exists(exe):
        return {"text": "", "segments": [], "error": "没装语音识别"}
    cmd = [exe, "-m", "models/sensevoice-small-q8.gguf",
           "--vad", "models/fsmn-vad.gguf", "-a", wav, "--srt"]
    if os.name != "nt":
        cmd = ["nice", "-n", "10"] + cmd
    try:
        r = subprocess.run(cmd, cwd=asr_dir, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"text": "", "segments": [], "error": "语音识别超时"}
    out = (r.stdout or b"").decode("utf-8", "replace")
    segs = _parse_srt(out)
    text = "".join(x["text"] for x in segs)
    if not text:
        lines = [s.strip() for s in out.splitlines() if s.strip() and not s.strip().isdigit()
                 and "-->" not in s and not s.strip().startswith("[")]
        text = "".join(lines)
    return {"text": text[:12000], "segments": segs[:200],
            "error": "" if text else "没听到人声"}


_CACHE_LOCK = threading.Lock()


def cache_get(path, key, ttl=7 * 86400):
    try:
        with _CACHE_LOCK:
            data = json.load(open(path, encoding="utf-8"))
        item = data.get(key)
        if item and time.time() - float(item.get("ts") or 0) < ttl:
            return item
    except Exception:
        pass
    return None


def cache_put(path, key, value):
    try:
        with _CACHE_LOCK:
            data = {}
            if os.path.exists(path):
                try:
                    data = json.load(open(path, encoding="utf-8"))
                except Exception:
                    data = {}
            value = dict(value or {})
            value["ts"] = int(time.time())
            data[key] = value
            if len(data) > 200:
                for k in sorted(data, key=lambda x: data[x].get("ts", 0))[:50]:
                    data.pop(k, None)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except Exception:
        pass


def cache_key(url):
    u = str(url or "").split("#")[0]
    if platform_of(u) == "bilibili":
        m = re.search(r"(BV[0-9A-Za-z]{10,12}|av\d+)", u)
        if m:
            return "bili:" + m.group(1)
    if platform_of(u) == "douyin":
        vid = _douyin_id(u)
        if vid:
            return "dy:" + vid
    return "url:" + hashlib.sha256(u.encode("utf-8")).hexdigest()[:32]
