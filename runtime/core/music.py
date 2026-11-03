# -*- coding: utf-8 -*-
"""点歌：网易云（163 卡片）或 QQ 音乐（链接，NapCat 不支持 QQ 音乐卡片）。

v0.65.5：明确点歌改成「先列候选、回序号再发」：
  - search_many() 一次返回多首候选（只搜不验播放，列表要快）；
  - resolve_song() 用户选中后再确认能真交付（网易云验可播放、QQ 补封面+直链）；
  - search_first() 保留原样，给「随便唱」和出站补歌兜底用。
"""
import json
import re
import urllib.parse
import urllib.request

NETEASE_API = "https://music.163.com/api/cloudsearch/pc"
QQ_API = "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg"


def _netease_playable(song_id, timeout=8):
    try:
        u = "https://music.163.com/song/media/outer/url?id=%s.mp3" % song_id
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
            return "/404" not in final
    except Exception:
        return False


def _netease_songs(keyword, limit=6, timeout=15):
    """v0.65.5：一次搜索返回多首候选（不预先验可播放，选了再验）。"""
    q = str(keyword or "").strip()
    if not q:
        return []
    url = NETEASE_API + "?" + urllib.parse.urlencode(
        {"s": q, "type": 1, "limit": max(1, min(15, int(limit or 6))), "offset": 0})
    req = urllib.request.Request(url, headers={
        "Referer": "https://music.163.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Cookie": "appver=2.0.2",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for s in ((obj.get("result") or {}).get("songs") or []):
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        artists = [str(a.get("name") or "").strip() for a in (s.get("ar") or s.get("artists") or [])]
        artists = [a for a in artists if a]
        out.append({
            "source": "netease",
            "id": sid,
            "name": str(s.get("name") or "").strip(),
            "artist": "、".join(artists),
            "album": str((s.get("al") or {}).get("name") or "").strip(),
            "cover": str((s.get("al") or {}).get("picUrl") or "").strip(),
            "url": "https://music.163.com/song?id=%s" % sid,
        })
    return out


def search_netease(keyword, timeout=15):
    songs = _netease_songs(keyword, 6, timeout)
    if not songs:
        return None
    s = songs[0]
    for cand in songs:
        if _netease_playable(cand.get("id")):
            s = cand
            break
    return s


def search_netease_many(keyword, limit=5, timeout=15):
    """v0.65.5：列候选用，只搜不验播放（省 5~6 次网络请求）。"""
    n = max(1, min(9, int(limit or 5)))
    return _netease_songs(keyword, max(n, 6), timeout)[:n]


def _qq_post(body, timeout=15):
    req = urllib.request.Request("https://u.y.qq.com/cgi-bin/musicu.fcg",
                                 data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _qq_detail(mid, timeout=15):
    try:
        o = _qq_post({"req_0": {"module": "music.pf_song_detail_svr", "method": "get_song_detail",
                                "param": {"song_mid": mid}},
                      "comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0}}, timeout)
        tr = (((o.get("req_0") or {}).get("data") or {}).get("track_info") or {})
        albummid = str(((tr.get("album") or {}).get("mid")) or "")
        return albummid
    except Exception:
        return ""


def _netease_audio_for(name, artist, timeout=15):
    """QQ 音乐拿不到免费播放地址时，用网易云同名的可播放版本兜底。"""
    q = (str(name or "") + " " + str(artist or "")).strip()
    if not q:
        return ""
    try:
        for s in _netease_songs(q, 6, timeout):
            sid = s.get("id")
            if _netease_playable(sid):
                return "https://music.163.com/song/media/outer/url?id=%s.mp3" % sid
    except Exception:
        pass
    return ""


def _qq_audio(mid, timeout=15):
    try:
        o = _qq_post({"req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                                "param": {"guid": "10000", "songmid": [mid], "songtype": [0],
                                          "uin": "0", "loginflag": 1, "platform": "20"}},
                      "comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0}}, timeout)
        info = (((o.get("req_0") or {}).get("data") or {}).get("midurlinfo") or [{}])[0]
        return str(info.get("purl") or "")
    except Exception:
        return ""


def _qq_songs(keyword, limit=5, timeout=15):
    """v0.65.5：QQ 音乐只返回歌名/歌手/mid 候选；封面和直链等选中后再补。"""
    q = str(keyword or "").strip()
    if not q:
        return []
    n = max(1, min(9, int(limit or 5)))
    url = QQ_API + "?" + urllib.parse.urlencode({
        "key": q, "format": "json", "inCharset": "utf8", "outCharset": "utf-8", "platform": "yqq"})
    req = urllib.request.Request(url, headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    songs = (((obj.get("data") or {}).get("song") or {}).get("itemlist") or [])
    out = []
    for s in songs:
        mid = str(s.get("mid") or "").strip()
        if not mid:
            continue
        out.append({
            "source": "qq",
            "id": mid,
            "name": str(s.get("name") or "").strip(),
            "artist": str(s.get("singer") or "").strip(),
            "album": "",
            "cover": "",
            "audio": "",
            "url": "https://y.qq.com/n/ryqq/songDetail/" + mid,
        })
        if len(out) >= n:
            break
    return out


def resolve_qq_song(song, timeout=15):
    mid = str((song or {}).get("id") or "").strip()
    if not mid:
        return None
    albummid = _qq_detail(mid, timeout)
    cover = ("https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg" % albummid) if albummid else ""
    audio = _qq_audio(mid, timeout)
    if not audio:
        audio = _netease_audio_for((song or {}).get("name"), (song or {}).get("artist"), timeout)
    out = dict(song or {})
    out.update({"cover": cover, "audio": audio})
    return out


def search_qq(keyword, timeout=15):
    songs = _qq_songs(keyword, 5, timeout)
    if not songs:
        return None
    return resolve_qq_song(songs[0], timeout)


def search_qq_many(keyword, limit=5, timeout=15):
    return _qq_songs(keyword, limit, timeout)


def search_first(keyword, source="netease", timeout=15):
    if source == "qq":
        try:
            return search_qq(keyword, timeout=timeout)
        except Exception:
            return None
    try:
        return search_netease(keyword, timeout=timeout)
    except Exception:
        return None


def search_many(keyword, source="netease", limit=5, timeout=15):
    """v0.65.5：明确点歌时列候选用。返回 list（最多 limit 首），不做可播放预检。"""
    if source == "qq":
        try:
            return search_qq_many(keyword, limit=limit, timeout=timeout)
        except Exception:
            return []
    try:
        return search_netease_many(keyword, limit=limit, timeout=timeout)
    except Exception:
        return []


def resolve_song(song, timeout=15):
    """v0.65.5：用户选了第 N 首后，确认这一首真能交付。

    网易云：验可播放（不可播放返回 None，让对方换一首）；
    QQ 音乐：补封面和音频直链（补不到音频时用网易云同名版本兜底）。
    """
    song = dict(song or {})
    if not song:
        return None
    if song.get("source") == "qq":
        return resolve_qq_song(song, timeout)
    sid = str(song.get("id") or "").strip()
    if not sid or not _netease_playable(sid, timeout):
        return None
    return song


def music_seg(song):
    song = song or {}
    if song.get("source") == "qq":
        return {"type": "music", "data": {
            "type": "custom",
            "url": song.get("url") or "",
            "audio": song.get("audio") or song.get("url") or "",
            "title": song.get("name") or "",
            "content": song.get("artist") or "",
            "image": song.get("cover") or "",
        }}
    return {"type": "music", "data": {"type": "163", "id": str(song.get("id") or "")}}

# v0.65.0：识别「嘴上说放歌/唱歌，其实没真发」的句子，用来兜底补一首真的。
# 只认「动作词 + 书名号」这种明确说法（来首《大鱼》/给你放《告白气球》），
# 聊天里正常提歌名（《大鱼》挺好听）不会被当成承诺。
_CLAIM_RE = re.compile(
    r"(?:来一首|来首|放一首|放首|点一首|点首|唱一首|唱首|给你放首?|给你唱首?|给你点首?)"
    r"\s*[《「“]([^》」”\s]{1,20})[》」”]")
_SANG_RE = re.compile(
    r"(?:歌|音乐)(?:都|已经|早|早就)?(?:放|发|点)(?:出去|好了|掉了)|(?:已经|刚|刚刚)(?:给你)?(?:放|点)(?:好了|了|出去)")
_SKIP_RE = re.compile(r"不会真唱|真唱不了|不会唱|不唱|懒得唱|要不|要不要|想不想|不如")


def parse_song_claim(text):
    """v0.65.0：这句回复是不是在宣称放歌/唱歌（但这一轮其实没发歌）。

    返回 None（不是）或 {"kw": 歌名或空串, "sang": 是否在说「已经放了」}。
    """
    t = str(text or "").strip()
    if not t or len(t) > 300 or _SKIP_RE.search(t):
        return None
    m = _CLAIM_RE.search(t)
    if m:
        tail = t[m.end():m.end() + 3]
        if "？" in tail or "?" in tail:
            return None  # 「要不来首《X》？」是提议，不是承诺
        kw = re.sub(r"\s+", " ", m.group(1)).strip()
        if not kw or kw in ("歌", "首歌", "什么", "这个", "那个"):
            return None
        return {"kw": kw, "sang": False}
    if _SANG_RE.search(t):
        return {"kw": "", "sang": True}
    return None
