# -*- coding: utf-8 -*-
"""QQ空间配图：从必应图片（免版权筛选）找图 → 视觉审核 → 压缩落盘 → 去重记账。

用在哪
    flow_qzone 写说说前调 pick_photo()：
    search_photos 找候选 → fetch_photo 下载 → pool.vision 看一眼
    （有没有水印/大字/真人正脸/和内容不搭）→ 缩放压成 JPEG 落盘 → 记 30 天内不重复用。

为什么只走必应（2026-10 实测，服务器与本机都试过）
    用户明确不要 AI 生图，要网上的免版权照片。
    - 必应图片 async 接口可用：
      https://cn.bing.com/images/async?q=...&first=1&count=30&mkt=zh-CN&adlt=strict
      &qft=+filterui:license-L1+filterui:photo-photo
      license-L1 = 允许再使用/修改（免版权），photo-photo = 只要照片不要插画；
    - 加了筛选后结果多来自 pixabay / pixnio / goodfreephotos / hippopx /
      publicdomainpictures / peakpx。**但能直连下载的没几个**（本机实测）：
        cdn.pixabay.com          OK 0.3~2.4s（最稳，还能按尺寸升档）
        www.goodfreephotos.com   OK 2~34s（很慢，原图 1920 宽）
        pixnio.com               403（带不带 Referer 都 403）
        www.publicdomainpictures.net 403
        i0.hippopx.com           403
        as2.ftcdn.net（Adobe 付费图库）403 / 挂 8 秒
        c1.peakpx.com            本机 DNS 解析失败
    - Openverse / Wikimedia 在服务器上连不通，所以这里不接。

坑（都是实测踩出来的，改代码前先看）
    1. murl 在 HTML 里是转义的（murl&quot;:&quot;...），个别链接还带 &amp; 实体，
       抽出来必须 html.unescape，否则下载直接 404。
    2. mkt=zh-CN 的结果会混进国内商业图库和聚合站（sinaimg / 堆糖 / veer 等），
       所以有 _BLOCK_HOSTS 黑名单；黑名单只堵大头，真正把关的是后面的视觉审核。
    3. 必应给的 murl 很多是**缩略图**（pixabay 的 __180.jpg 只有 180 宽、
       goodfreephotos 的 /cache/..._w80_h80_..._thumb.jpg 只有 80 宽），
       直接下会全部被 640 宽的门槛拒掉。_variants() 会把它们升档成原图：
       pixabay __180 → _1280/_960_720/_640，goodfreephotos /cache/xx_w80_.. → /albums/xx.jpg。
       升档失败就当这张没有，不报错。
    4. 有图站慢得离谱（goodfreephotos 实测单张 30 秒 +）。所以 pick_photo 全程带
       40 秒总预算，fetch_photo 内部也按「总超时」给每个变体分时间，超预算立刻放弃返回 None，
       让说说退回纯文字，绝不能把发帖线程卡死。
    5. 一页结果里能直连的站太少（night city lights 首页 13 张全是 403 的站），
       所以 search_photos 会往下多翻几页（最多 4 页，每页约 0.5 秒），
       并按「能直连的站优先」稳定排序 —— 顺序只在同站内保持必应原序。
    6. fetch_photo 顺手把原始字节（data）带回来，pick_photo 不用再下载第二次；
       回给业务方的 "bytes" 是体积（int），别和 data 混了。
    7. md5 取「下载到的原始字节」前 16 位：下载完立刻能算，于是「30 天内去过重」
       的检查能放在视觉审核之前，省一次模型调用；同一张图重下 md5 稳定。
    8. 服务器 2C2G：HTML 只读 512KB，图片最多 8MB，缩图用 thumbnail 就地改不 copy，
       index.json 超过 180 天或超过 500 条的旧账顺手清掉，别让文件无限长。

对外 API
    search_photos(queries, want=8, timeout=12) -> list[str]
    fetch_photo(url, timeout=15, max_bytes=8MB) -> dict | None
    pick_photo(pool, cfg, queries, avoid_md5=(), min_width=720, max_mb=2) -> dict | None
    license_note(md5) -> str

原则：任何异常都吞掉（只记日志），返回 [] / None，绝不抛给调用方；
      所有网络请求都带超时；拿不准就不发图（fail-closed）。
"""
import hashlib
import html as _html
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from util import log, log_exc

try:
    from config import DATA_DIR
except Exception:  # config 起不来（缺依赖/权限）时退回 util 里同名的常量，别为一张图崩掉整个大脑
    from util import DATA_DIR

try:
    from PIL import Image

    HAS_PIL = True
    # Pillow 10 起把重采样常量挪进了 Image.Resampling，这里两种都兜住
    _RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
    # 防解压炸弹：超大图直接当坏图（2G 内存扛不住一张 1 亿像素的图）
    try:
        Image.MAX_IMAGE_PIXELS = 40000000
    except Exception:
        pass
except Exception:  # 没装 PIL：校验降级成正则读 JPEG/PNG 头，缩图和转码就不做了
    Image = None
    HAS_PIL = False
    _RESAMPLE = 1

TAG = "qzone_img"

BING_ASYNC = "https://cn.bing.com/images/async"
BING_REFERER = "https://cn.bing.com/images/search?q="
# license-L1=允许再使用/修改，photo-photo=只要照片。开头的 + 是必应自己的写法，别 urlencode 掉
QFT = "+filterui:license-L1+filterui:photo-photo"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SEARCH_TIMEOUT = 12          # 找图整批预算（不是每个请求）
FETCH_TIMEOUT = 15           # 单张下载（含升档变体的总时间）
MAX_BYTES = 8 * 1024 * 1024  # 单张原图上限
HTML_CAP = 512 * 1024        # 必应结果页只读这么多，够 30 张图的 murl 了
MIN_FETCH_WIDTH = 640        # fetch_photo 的及格线
SEARCH_PAGES = 4             # 每个查询最多翻几页（0.5 秒一页，够凑出能下的站）
PAGE_STEP = 30               # 必应翻页步长：first=1/31/61/91
MAX_SIDE = 1600              # 落盘最长边
FIT_MIN = 6                  # 视觉审核的配适度及格线
TOTAL_BUDGET = 40.0          # pick_photo 总预算（秒）
SEARCH_WANT = 10             # 内部多找两张，抵掉 403/超时的损耗
MAX_VISION_TRIES = 6         # 最多给 6 张图花视觉调用的钱
RECENT_SEC = 30 * 86400      # 30 天去重窗口
INDEX_KEEP_DAYS = 180
INDEX_MAX = 500

# 付费图库 / 国内商业素材站 / 聚合号：结果里混进来一律不要
# （后面四个是实测补的：adobe 家的 ftcdn 是付费图库还老挂 8 秒、rawpixel 付费、
#   freepngimg 是抠图 PNG 不是照片）
_BLOCK_HOSTS = (
    "699pic", "588ku", "qiantucdn", "veer", "hellorf", "tukuppt", "51miz", "ooopic",
    "zcool", "huitu", "shutterstock", "picdn", "almy", "dreamstime", "istock", "gettyimages",
    "ftcdn", "rawpixel", "freepngimg",
)

# 下载成功率分档：0 最快最稳，2 常见 403（本机），5 没见过的站。排序只在同档内保持必应原序。
_HOST_RANK = (
    (("pixabay.com",), 0),
    (("goodfreephotos.com", "pxfuel.com", "piqsels.com", "peakpx.com"), 1),
    (("pixnio.com", "hippopx.com", "publicdomainpictures.net"), 2),
)

QZONE_IMG_DIR = os.path.join(DATA_DIR, "qzone_images")
INDEX_PATH = os.path.join(QZONE_IMG_DIR, "index.json")

_idx_lock = threading.Lock()

# 两种写法都见过：HTML 转义版和裸 JSON 版。&amp; 之类在 _extract_murls 里统一 unescape
_MURL_RES = (
    re.compile(r"murl&quot;:&quot;(.*?)&quot;", re.S),
    re.compile(r'"murl"\s*:\s*"(.*?)"', re.S),
)

_GFG_CACHE_RE = re.compile(
    r"^(https?://www\.goodfreephotos\.com)/cache/(.+?)_w\d+_h\d+[^/]*?\.jpg", re.I)


# ---------------- 小工具 ----------------

def _cfg_get(cfg, key, default=None):
    """cfg 可能是 dict 也可能是 Config 对象；缺键就吃默认值，别让它抛。"""
    try:
        v = cfg.get(key)
    except Exception:
        v = None
    return default if v is None else v


def _json_obj(text):
    """从模型输出里抠第一个 JSON 对象；抠不出返回 None（调用方按不合格处理）。"""
    s = str(text or "")
    i = s.find("{")
    while i >= 0:
        depth = 0
        for j in range(i, len(s)):
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[i:j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
        i = s.find("{", i + 1)
    return None


def _truthy(v):
    """模型给的 true/false 可能是 bool、0/1 或字符串，统一认一遍。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in ("true", "1", "yes", "y", "t", "是", "有")


def _size_from_header(raw):
    """没 PIL 时的兜底：只读 JPEG/PNG 头拿宽高，读不出返回 (0, 0)。"""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
        if raw[:2] == b"\xff\xd8":
            i, n = 2, len(raw)
            while i + 9 < n:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(raw[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(raw[i + 5:i + 7], "big")
                    w = int.from_bytes(raw[i + 7:i + 9], "big")
                    return w, h
                if seg <= 0:
                    return 0, 0
                i += 2 + seg
    except Exception:
        pass
    return 0, 0


def _image_size(raw):
    """能打开就算过（PIL 只读头，不解整个像素）；返回 (w, h) 或 (0, 0)。"""
    if not raw:
        return 0, 0
    if HAS_PIL:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                return int(im.width), int(im.height)
        except Exception:
            return 0, 0
    return _size_from_header(raw)


def _host_ok(url):
    """黑名单域名过滤 + 只收 http(s)。"""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    host = parts.netloc.lower()
    for bad in _BLOCK_HOSTS:
        if bad in host:
            return False
    return True


def _host_rank(url):
    """给候选图排个队：能直连的站先试，免得把 40 秒预算全花在 403 上。"""
    try:
        host = urllib.parse.urlsplit(str(url or "")).netloc.lower()
    except Exception:
        return 9
    for names, rank in _HOST_RANK:
        for name in names:
            if name in host:
                return rank
    return 5


def _bing_url(query, count=30, first=1):
    """拼必应图片 async 地址。qft 里的 + 和 : 必须原样留着，encode 掉就不生效了。"""
    return (BING_ASYNC + "?q=" + urllib.parse.quote(query)
            + "&first=" + str(max(1, int(first or 1)))
            + "&count=" + str(max(1, min(35, int(count or 30))))
            + "&mkt=zh-CN&adlt=strict&qft=" + QFT)


# ---------------- ① 找图 ----------------

def _fetch_html(query, timeout, first=1):
    """抓一次必应结果页；失败返回空串（不抛）。"""
    req = urllib.request.Request(_bing_url(query, first=first), headers={
        "User-Agent": UA,
        "Referer": BING_REFERER + urllib.parse.quote(query),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=max(2.0, min(20.0, float(timeout)))) as resp:
            return resp.read(HTML_CAP).decode("utf-8", "replace")
    except Exception as e:
        log(TAG, "必应接口失败 q=%s first=%s %r" % (str(query)[:30], first, e))
        return ""


def _extract_murls(text):
    """从结果页里抽图片直链。两种转义写法都扫，重复的交给外层去重。"""
    out = []
    for pat in _MURL_RES:
        for m in pat.finditer(text or ""):
            u = _html.unescape(m.group(1) or "").strip()
            if u.startswith(("http://", "https://")):
                out.append(u)
    return out


def search_photos(queries, want=8, timeout=12):
    """按查询词去必应找免版权照片，返回候选直链（最多 want 个）。

    注意三件事：
    1) timeout 是「整批查询」的总预算，不是每个查询给满 12 秒——后面的查询只能分到
       剩下的时间，免得 4 个词各卡 12 秒把发帖流程拖死；
    2) 会往下多翻几页（最多 SEARCH_PAGES 页）：首页常常全是 403 的图站
       （night city lights 实测首页 13 张里只有 1 张能下），不翻页凑不出能下的图；
    3) 返回前按「能直连的站优先」稳定排序（同站内保持必应原序），
       让调用方先试 pixabay 这种 0.5 秒能下的站，而不是先撞一串 403。

    黑名单过滤 + 按 url 去重；拿不到就返回 []，不抛异常。
    """
    out, seen = [], set()
    try:
        want = max(1, min(30, int(want or 8)))
    except Exception:
        want = 8
    try:
        budget = max(4.0, float(timeout or SEARCH_TIMEOUT))
    except Exception:
        budget = float(SEARCH_TIMEOUT)
    t0 = time.time()
    for q in list(queries or [])[:4]:
        q = re.sub(r"\s+", " ", str(q or "")).strip()[:80]
        if not q:
            continue
        for page_no in range(SEARCH_PAGES):
            # 候选够多、能直连的站也够了，就别再多打请求
            good = sum(1 for u in out if _host_rank(u) <= 1)
            if len(out) >= want and good >= max(2, min(4, want // 2)):
                break
            left = budget - (time.time() - t0)
            if left < 3:
                log(TAG, "找图预算用完（已有 %d 张候选）" % len(out))
                break
            page = _fetch_html(q, left, first=1 + page_no * PAGE_STEP)
            if not page:
                break
            added = 0
            for u in _extract_murls(page):
                if not _host_ok(u) or u in seen:
                    continue
                seen.add(u)
                out.append(u)
                added += 1
            if not added:          # 这一页全是重复的，再翻也没东西了
                break
        log(TAG, "找图 q=%s 累计候选 %d 张" % (q[:30], len(out)))
    # sort 是稳定的：同档位内部保持必应给的顺序
    out.sort(key=_host_rank)
    return out[:want]


# ---------------- ② 下载 ----------------

def _variants(url):
    """把必应给的缩略图地址升档成原图地址（推不出来返回空列表）。

    - pixabay：.../xxx__180.jpg → xxx_1280.jpg / xxx_960_720.jpg / xxx_640.jpg
      （同一个 slug 在 pixabay CDN 上有多档尺寸，__180 只有 180 宽，过不了 640 门槛）
    - goodfreephotos：/cache/猫/名字_w80_h80_cw80_ch80_thumb.jpg → /albums/猫/名字.jpg
      （cache 里全是 80x80 缩略图，原图在 albums 下）
    """
    u = str(url or "")
    out = []
    try:
        if "cdn.pixabay.com" in u or "pixabay.com" in u:
            for old in ("__180.", "__340."):
                if old in u:
                    stem = u.split(old)[0]
                    out = [stem + "_1280.jpg", stem + "_960_720.jpg", stem + "_640.jpg"]
                    break
        elif "goodfreephotos.com" in u and "/cache/" in u:
            m = _GFG_CACHE_RE.match(u)
            if m:
                out = [m.group(1) + "/albums/" + m.group(2) + ".jpg"]
    except Exception:
        out = []
    return out


def _download(url, timeout, limit):
    """真正的下载 + 体检。返回 {"url","data","bytes","mime","w","h"} 或 None。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://cn.bing.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, min(30.0, float(timeout)))) as resp:
            mime = str(resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not mime.startswith("image/"):
                log(TAG, "不是图片，跳过 %s（%s）" % (url[:60], mime or "无 Content-Type"))
                return None
            try:
                clen = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                clen = 0
            if clen and clen > limit:
                log(TAG, "体积超限，跳过 %s（%.1fMB）" % (url[:60], clen / 1048576.0))
                return None
            # 多读 1 字节：能判断「正好读满 = 其实更大」，避免把 50MB 的图读进内存
            raw = resp.read(limit + 1)
    except Exception as e:
        log(TAG, "下载失败 %s %r" % (url[:60], e))
        return None
    if not raw or len(raw) > limit:
        return None
    w, h = _image_size(raw)
    if w < MIN_FETCH_WIDTH:
        log(TAG, "太窄跳过 %dx%d %s" % (w, h, url[:60]))
        return None
    return {"url": url, "data": raw, "bytes": len(raw), "mime": mime, "w": w, "h": h}


def fetch_photo(url, timeout=15, max_bytes=MAX_BYTES):
    """下载一张图并做最基本的体检，缩略图会自动升级成原图再试。

    要求：Content-Type 是 image/*、体积不超上限、PIL 能打开、宽 >= 640。
    返回 {"data": 原始字节, "bytes": 体积, "mime": ..., "w": ..., "h": ..., "url": 真正下载的地址}；
    不合格返回 None。
    注意：
    - "data" 是顺手带回来的原始字节，调用方要落盘就不用再下一次了；
    - timeout 是整张图（含升档变体）的总时间，不是每个变体都给满；
    - 回不了原图（403/超时）就当这张没有，绝不抛；
    - 不校验高度，横幅图（1600x300）也算能用。
    """
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return None
    try:
        limit = max(64 * 1024, int(max_bytes or MAX_BYTES))
    except Exception:
        limit = MAX_BYTES
    try:
        budget = max(1.0, min(30.0, float(timeout or FETCH_TIMEOUT)))
    except Exception:
        budget = float(FETCH_TIMEOUT)
    deadline = time.time() + budget
    tried = set()
    for cand in _variants(u) + [u]:
        if cand in tried:
            continue
        tried.add(cand)
        left = deadline - time.time()
        if left < 1:
            break
        got = _download(cand, left, limit)
        if got:
            return got
    return None


# ---------------- ③ 视觉审核 ----------------

VISION_SYS = (
    "你是配图审核员。用户要发一条 QQ空间说说（给所有好友看的生活随笔），"
    "系统从网上找了一张免版权照片想配上去。你只看这一个任务：这张图能不能公开发出去当配图。\n"
    "命中任一条就判不能：\n"
    "1. 有水印、图库 logo、站点名字、网址、二维码；\n"
    "2. 图上有大段文字（字幕条、大字标题、海报文案、九宫格文字）——只有一两个很小的字可以放过；\n"
    "3. 有真人正脸（大脸特写、人像写真的主体）——远景背影、路人剪影、手部或局部可以放过；\n"
    "4. 血腥、暴力、色情、低俗、恐怖、恶心、烟酒毒品、政治敏感、旗帜标语；\n"
    "5. 明显是截图、表情包、商品详情页、拼图、带相框的网图；\n"
    "6. 和要说的事完全不搭（比如写吃面却配工地照片）。\n"
    "拿不准就判不能：宁可不发图，也不要发错图。\n"
    '只输出一行 JSON，不要解释：{"ok":true,"fit":0-10,"watermark":false,"text":false,'
    '"face":false,"why":"10字内原因"}\n'
    "fit 是「和要说的事搭不搭」：10=非常贴，6=能用，3=勉强，0=完全不搭；"
    "watermark/text/face 是「有没有」；ok 是你自己的最终结论。"
)


def _vision_payload(raw, mime, max_px=512):
    """发给视觉模型的小图：最长边 512 的 JPEG。原图 2MB 直传既慢又贵，
    服务器 2C2G 上 base64 一放大很容易爬满内存。压不出来就退回原图，不能不审。"""
    if not HAS_PIL:
        return raw, (mime or "image/jpeg")
    try:
        im = Image.open(io.BytesIO(raw))
        try:
            im.seek(0)  # 动图取第一帧
        except Exception:
            pass
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px), _RESAMPLE)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
        if data:
            return data, "image/jpeg"
    except Exception as e:
        log(TAG, "缩略图生成失败，按原图送审 %r" % (e,))
    return raw, (mime or "image/jpeg")


def _vision_gate(pool, queries, raw, mime):
    """让视觉模型看一眼。合格返回它的判定 dict，不合格/调用失败返回 None（fail-closed）。"""
    vision = None
    try:
        vision = getattr(pool, "vision", None) if pool is not None else None
    except Exception:
        vision = None
    if not callable(vision):
        log(TAG, "没有视觉能力，配图流程放弃")
        return None
    data, vmime = _vision_payload(raw, mime)
    topic = "、".join([str(q) for q in (queries or [])[:3]]) or "一件小事"
    prompt = ("这张图准备配一条 QQ空间说说（生活随笔，所有好友可见）。\n"
              "要说的事：%s\n\n"
              "按标准自己看图判断能不能用，只输出一行 JSON。" % topic)
    try:
        r = vision(VISION_SYS, prompt, data, vmime)
    except Exception as e:
        log_exc(TAG, "视觉审核异常 %r" % (e,))
        return None
    if not getattr(r, "ok", False):
        log(TAG, "视觉审核调用失败：%s" % str(getattr(r, "error", ""))[:120])
        return None
    obj = _json_obj(getattr(r, "text", ""))
    if not obj:
        log(TAG, "视觉审核没给 JSON，按不合格处理：%s" % str(getattr(r, "text", ""))[:80])
        return None
    try:
        fit = float(obj.get("fit") or 0)
    except Exception:
        fit = 0.0
    wm, tx, fc = _truthy(obj.get("watermark")), _truthy(obj.get("text")), _truthy(obj.get("face"))
    why = str(obj.get("why") or "")[:40]
    # 模型自己都写 ok=false 了，就算 fit 给得高也不能用（自相矛盾 = 拿不准）
    if "ok" in obj and not _truthy(obj.get("ok")):
        log(TAG, "视觉审核自判不合格：%s" % why)
        return None
    if wm or tx or fc:
        log(TAG, "视觉审核不合格（水印=%s 文字=%s 正脸=%s）：%s" % (wm, tx, fc, why))
        return None
    if fit < FIT_MIN:
        log(TAG, "视觉审核不搭（fit=%.0f）：%s" % (fit, why))
        return None
    return obj


# ---------------- ④ 压缩落盘 ----------------

def _encode_jpeg(raw, max_side=MAX_SIDE, max_mb=2):
    """等比缩到最长边 <= max_side，转 JPEG；体积压不下 max_mb 就降质量 88→80→72。
    返回 (jpeg 字节, w, h)；实在压不小返回 None（宁可不配图，也别发一张几 MB 的图）。"""
    try:
        limit = int(float(max_mb or 2) * 1024 * 1024)
    except Exception:
        limit = 2 * 1024 * 1024
    limit = max(8 * 1024, limit)
    if not HAS_PIL:
        # 没 PIL：只放行本来就是 JPEG、体积够小的原图（缩不了图就不缩）
        w, h = _size_from_header(raw)
        if w and raw[:2] == b"\xff\xd8" and len(raw) <= limit:
            return raw, w, h
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)  # 手机拍的图带旋转标记，不转就歪着发出去
        except Exception:
            pass
        im = im.convert("RGB")  # P/CMYK/RGBA 都得转，JPEG 存不了透明通道
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), _RESAMPLE)
        for q in (88, 80, 72):
            buf = io.BytesIO()
            # 不传 exif：原图的机型/GPS 别跟着照片发到空间去
            im.save(buf, "JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if data and len(data) <= limit:
                return data, int(im.size[0]), int(im.size[1])
        log(TAG, "压到 quality=72 还超 %dKB，放弃这张" % (limit // 1024))
        return None
    except Exception as e:
        log(TAG, "转码失败 %r" % (e,))
        return None


def _write_atomic(path, data):
    """先写 .part 再 replace，写一半断电也不会留半张坏图。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log_exc(TAG, "落盘失败 %r" % (e,))
        try:
            os.remove(path + ".part")
        except Exception:
            pass
        return False


# ---------------- ⑤ 去重索引 ----------------

def _load_index():
    """读 index.json：{md5: {"url","ts","used_at"}}。坏了/没有都当空表，不影响出图。"""
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_index(idx):
    try:
        os.makedirs(QZONE_IMG_DIR, exist_ok=True)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False)
        os.replace(tmp, INDEX_PATH)
        return True
    except Exception as e:
        log_exc(TAG, "写索引失败 %r" % (e,))
        return False


def _prune_index(idx, now=None):
    """旧账清理：180 天前的删掉，最多留 500 条。index.json 不能无限长。"""
    now = now or time.time()
    cold = now - INDEX_KEEP_DAYS * 86400
    keep = {}
    for k, v in (idx or {}).items():
        try:
            used = int((v or {}).get("used_at") or 0)
        except Exception:
            used = 0
        if used and used < cold:
            continue
        keep[k] = v
    if len(keep) > INDEX_MAX:
        rows = sorted(keep.items(),
                      key=lambda kv: int((kv[1] or {}).get("used_at") or 0), reverse=True)
        keep = dict(rows[:INDEX_MAX])
    return keep


def _used_recently(idx, md5, url, now):
    """这张图（按 md5）或这个地址（按 url）30 天内用过没有。"""
    try:
        row = (idx or {}).get(md5) or {}
        used = int(row.get("used_at") or 0)
        if used and now - used < RECENT_SEC:
            return True
    except Exception:
        pass
    if not url:
        return False
    for _k, v in (idx or {}).items():
        try:
            if str((v or {}).get("url") or "") != url:
                continue
            used = int((v or {}).get("used_at") or 0)
            if used and now - used < RECENT_SEC:
                return True
        except Exception:
            continue
    return False


# ---------------- ⑥ 主流程 ----------------

def pick_photo(pool, cfg, queries, avoid_md5=(), min_width=720, max_mb=2):
    """完整管线：找图 → 下载 → 30 天去重 → 视觉过闸 → 压缩落盘 → 记一笔。

    返回 {"path", "md5", "url", "w", "h", "bytes", "source":"web"}；
    一个都没过就返回 None（调用方退回纯文字说说，不许卡住发帖流程）。

    cfg 只用在两个可选开关上（默认都按「开」走）：
        qzone_image_web       False = 整条网图流程关掉，直接返回 None
        qzone_image_max_mb    覆盖 max_mb
    """
    t0 = time.time()
    deadline = t0 + TOTAL_BUDGET
    if not bool(_cfg_get(cfg, "qzone_image_web", True)):
        return None
    try:
        max_mb = float(_cfg_get(cfg, "qzone_image_max_mb", max_mb) or max_mb)
    except Exception:
        max_mb = 2.0
    try:
        min_width = max(1, int(min_width or 720))
    except Exception:
        min_width = 720
    avoid = set(str(x or "").strip().lower() for x in (avoid_md5 or ()))
    queries = [re.sub(r"\s+", " ", str(q or "")).strip()[:80] for q in (queries or [])]
    queries = [q for q in queries if q]
    if not queries:
        return None

    # 找图只能吃掉一部分预算：至少给下载和送审留 28 秒
    urls = search_photos(queries, want=SEARCH_WANT,
                         timeout=min(SEARCH_TIMEOUT, max(4.0, deadline - time.time() - 28)))
    if not urls:
        log(TAG, "没找到候选图（queries=%r）" % (queries[:2],))
        return None

    idx = _load_index()
    now = time.time()
    tried = 0
    for url in urls:
        if tried >= MAX_VISION_TRIES:
            break
        left = deadline - time.time()
        if left < 8:
            log(TAG, "总预算快用完，停止找图（已送审 %d 张）" % tried)
            break
        f = fetch_photo(url, timeout=min(FETCH_TIMEOUT, max(3.0, left - 6)), max_bytes=MAX_BYTES)
        if not f:
            continue
        if int(f.get("w") or 0) < min_width:
            log(TAG, "宽度不够（%dx%d < %d）跳过" % (int(f.get("w") or 0), int(f.get("h") or 0), min_width))
            continue
        raw = f.get("data") or b""
        if not raw:
            continue
        real_url = str(f.get("url") or url)   # 升档后真正下载到的地址，记账用它
        md5 = hashlib.md5(raw).hexdigest()[:16]
        if md5 in avoid:
            log(TAG, "最近发过这张（avoid_md5 命中 %s），换下一张" % md5)
            continue
        if _used_recently(idx, md5, real_url, now):
            log(TAG, "30 天内用过这张（md5=%s），换下一张" % md5)
            continue
        tried += 1
        if not _vision_gate(pool, queries, raw, f.get("mime") or ""):
            continue
        enc = _encode_jpeg(raw, MAX_SIDE, max_mb)
        if not enc:
            continue
        data, w, h = enc
        path = os.path.join(QZONE_IMG_DIR, md5 + ".jpg")
        if not _write_atomic(path, data):
            continue
        # 记账：md5 用原始字节算的，同一张图重下还是同一个 key
        try:
            with _idx_lock:
                idx = _load_index()
                idx[md5] = {"url": real_url, "ts": int(t0), "used_at": int(time.time())}
                _save_index(_prune_index(idx))
        except Exception as e:
            log_exc(TAG, "记账失败 %r" % (e,))
        log(TAG, "选到配图 %s %dx%d %.0fKB 用时 %.1fs" % (
            md5, w, h, len(data) / 1024.0, time.time() - t0))
        return {"path": os.path.abspath(path), "md5": md5, "url": real_url,
                "w": w, "h": h, "bytes": len(data), "source": "web"}

    log(TAG, "候选试完没一张能用（送审 %d 张，用时 %.1fs）" % (tried, time.time() - t0))
    return None


def license_note(md5):
    """给日记/面板用的一句来源说明；查不到返回空串（面板照常显示，别报错）。"""
    key = str(md5 or "").strip().lower()
    if not key:
        return ""
    row = (_load_index().get(key) or {})
    if not row:
        return ""
    host = ""
    try:
        host = urllib.parse.urlsplit(str(row.get("url") or "")).netloc.lower()
    except Exception:
        host = ""
    try:
        used = int(row.get("used_at") or row.get("ts") or 0)
    except Exception:
        used = 0
    when = time.strftime("%Y-%m-%d", time.localtime(used)) if used else "日期不明"
    return ("网上免版权照片（必应 license-L1 筛选%s）· 抓取于 %s"
            % ((" · 来源 " + host) if host else "", when))
