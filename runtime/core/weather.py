# -*- coding: utf-8 -*-
"""实时天气（Open-Meteo 公开接口，免 Key）。

背景（v0.24.1，2026-10-22 22:27 现场）：
用户问「大肥鱼明天天气呢」，搜索判断模型自己编了个「2026年2月23日」，
搜出来全是 2 月的老页面；网页快照又常缺发布时间，模型分不清新旧，
只能回「全是 2 月的老页面，数没刷出新的」。
现在天气类问题直接查 Open-Meteo：地点由地名解析，给现在 + 今天/明天/后天三天的数字。

不联网也能用：任何异常都返回 None，调用方退回原来的网页搜索。
"""
import datetime
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from util import log

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FC_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8.0
_GEO_TTL = 24 * 3600
_FC_TTL = 10 * 60
HOME_CITY = ""

# WMO 天气代码 → 中文
WMO_CN = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

WEATHER_RE = re.compile(
    r"(天气|气温|几度|多少度|下雨|降雨|下雪|冰雹|带伞|台风|空气质量|雾霾|降水|冷不冷|热不热|晴天|阴天)")

# 地名清洗：先去日期，再去这些词；长的排前面，避免「天气预报」被拆成「预报」
_CITY_NOISE = (
    "蓝色大肥鱼", "大肥鱼", "天气预报", "天气", "预报", "气温", "温度", "多少度", "几度",
    "怎么样", "怎样", "如何", "咋样", "帮我", "给我", "麻烦", "查一下", "搜一下", "查查", "搜搜",
    "看看", "现在", "今天", "今日", "明天", "明日", "后天", "这几天", "最近", "这几天",
    "什么", "多少", "下不下雨", "下不下雪", "下雨吗", "带伞", "要不要", "冷不冷", "热不热",
    "空气质量", "雾霾", "晴天", "阴天", "预报查询", "查", "搜", "的", "吗", "呢", "啊", "呀",
    "吧", "嘛", "了", "是", "有", "没有", "度", "几",
)
_CITY_DATE_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"
    r"|\d{1,2}\s*[日号]")

_GEO = {}
_GEO_LOCK = threading.Lock()
_FC = {}
_FC_LOCK = threading.Lock()
_LAST = {}
_LAST_LOCK = threading.Lock()


def looks_like_weather(text):
    """这句话是不是在问天气（含「要不要带伞」这种）。"""
    return bool(WEATHER_RE.search(str(text or "")))


def clean_city(text):
    """从「大肥鱼明天郴州天气怎么样」里抠出地名；抠不出返回空串。"""
    s = _CITY_DATE_RE.sub(" ", str(text or ""))
    for w in _CITY_NOISE:
        s = s.replace(w, " ")
    s = re.sub(r"[\s\u3000,，。.、:：!！?？~～'\"（）()《》]", "", s)
    if len(s) < 2 or len(s) > 8:
        return ""
    if re.search(r"\d", s):
        return ""
    return s


def _http_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "DafeiyuBot-weather/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def geocode(city, timeout=TIMEOUT):
    """地名 → 经纬度；Open-Meteo 自带中文地名库。"""
    name = str(city or "").strip()
    if not name:
        return None
    key = name.lower()
    now = time.time()
    with _GEO_LOCK:
        hit = _GEO.get(key)
        if hit and now - hit[0] < _GEO_TTL:
            return hit[1]
    try:
        url = GEO_URL + "?" + urllib.parse.urlencode({
            "name": name, "count": 5, "language": "zh", "format": "json", "countryCode": "CN"})
        obj = _http_json(url, timeout=timeout)
    except Exception as e:
        log("weather", "地名解析失败 %s %r" % (name, e))
        return None
    results = obj.get("results") or []
    best = None
    for r in results:
        n = str(r.get("name") or "")
        if n and (n in name or name in n):
            best = r
            break
    with _GEO_LOCK:
        _GEO[key] = (now, best)
        if len(_GEO) > 500:
            for k in sorted(_GEO, key=lambda k: _GEO[k][0])[:100]:
                _GEO.pop(k, None)
    if not best:
        log("weather", "地名没查到时 %s" % name)
    return best


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _pick(arr, i):
    try:
        return arr[i]
    except Exception:
        return None


def fetch(city, timeout=TIMEOUT):
    """查一个城市的现在天气 + 今天/明天/后天；失败返回 None。"""
    geo = geocode(city, timeout=timeout)
    if not geo:
        return None
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if lat is None or lon is None:
        return None
    key = "%s|%.3f,%.3f" % (geo.get("name"), float(lat), float(lon))
    now = time.time()
    with _FC_LOCK:
        hit = _FC.get(key)
        if hit and now - hit[0] < _FC_TTL:
            return hit[1]
    params = {
        "latitude": "%.4f" % float(lat),
        "longitude": "%.4f" % float(lon),
        "current": "temperature_2m,apparent_temperature,weather_code,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Shanghai",
        "forecast_days": "3",
    }
    try:
        obj = _http_json(FC_URL + "?" + urllib.parse.urlencode(params), timeout=timeout)
    except Exception as e:
        log("weather", "预报接口失败 %s %r" % (city, e))
        return None
    cur = obj.get("current") or {}
    daily = obj.get("daily") or {}
    days = []
    for i, d in enumerate((daily.get("time") or [])[:3]):
        days.append({
            "date": str(d),
            "code": _num(_pick(daily.get("weather_code"), i)),
            "hi": _num(_pick(daily.get("temperature_2m_max"), i)),
            "lo": _num(_pick(daily.get("temperature_2m_min"), i)),
            "pop": _num(_pick(daily.get("precipitation_probability_max"), i)),
        })
    if not days:
        log("weather", "预报没有日数据 %s" % city)
        return None
    data = {
        "name": str(geo.get("name") or city),
        "admin1": str(geo.get("admin1") or ""),
        "country": str(geo.get("country") or ""),
        "current": {
            "time": str(cur.get("time") or ""),
            "temp": _num(cur.get("temperature_2m")),
            "feels": _num(cur.get("apparent_temperature")),
            "code": _num(cur.get("weather_code")),
            "humidity": _num(cur.get("relative_humidity_2m")),
        },
        "days": days,
        "fetched_at": time.time(),
    }
    with _FC_LOCK:
        _FC[key] = (now, data)
        if len(_FC) > 200:
            for k in sorted(_FC, key=lambda k: _FC[k][0])[:50]:
                _FC.pop(k, None)
    return data


def code_cn(code):
    try:
        c = int(round(float(code)))
    except Exception:
        return "天气未知"
    return WMO_CN.get(c, "天气代码%d" % c)


def _day_label(date_str, base=None):
    base = base or datetime.date.today()
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except Exception:
        return str(date_str)
    offset = (d - base).days
    tag = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(offset, "")
    return "%d月%d日%s" % (d.month, d.day, ("（" + tag + "）") if tag else "")


def _t(v, unit=""):
    if v is None:
        return "?"
    if unit == "℃":
        return "%.1f℃" % v
    return "%d" % int(round(v))


def format_block(d, base=None):
    """给模型看的天气块：用接口数字，不让它再去翻网页。"""
    if not d:
        return ""
    cur = d.get("current") or {}
    where = "".join([x for x in (d.get("admin1"), d.get("name")) if x])
    stamp = str(cur.get("time") or "").replace("T", " ")
    lines = ["[实时天气（Open-Meteo 接口，不是网页快照；直接按这些数字回答，别再说没刷出来，也别贴链接）]"]
    lines.append("地点：%s%s" % (where or d.get("name") or "?",
                                 ("（数据时间 %s）" % stamp) if stamp else ""))
    if cur.get("temp") is not None:
        lines.append("现在：%s，%s，体感 %s，湿度 %s" % (
            _t(cur.get("temp"), "℃"), code_cn(cur.get("code")),
            _t(cur.get("feels"), "℃"),
            ("%d%%" % int(round(cur["humidity"]))) if cur.get("humidity") is not None else "?"))
    for day in d.get("days") or []:
        pop = day.get("pop")
        lines.append("%s：%s，%s~%s%s" % (
            _day_label(day.get("date"), base), code_cn(day.get("code")),
            _t(day.get("lo"), "℃").replace(".0℃", "℃"),
            _t(day.get("hi"), "℃").replace(".0℃", "℃"),
            (("，降水概率 %d%%" % int(round(pop))) if pop is not None else "")))
    lines.append("（问到哪天就答哪天；只答这些，不要编。）")
    return "\n".join(lines)


def _last_city(conv):
    with _LAST_LOCK:
        return _LAST.get(str(conv or ""))

def _set_last_city(conv, city):
    key = str(conv or "")
    with _LAST_LOCK:
        _LAST[key] = city
        if len(_LAST) > 500:
            for k in list(_LAST)[:100]:
                _LAST.pop(k, None)


def resolve(text, queries=None, conv="", timeout=TIMEOUT):
    """用户原话中的地点优先；未指定地点默认郴州，跨轮指代沿用上次地点。"""
    texts = [text] + list(queries or [])
    if not any(looks_like_weather(t) for t in texts):
        return None
    raw = str(text or "")
    if looks_like_weather(raw):
        city = clean_city(raw)
        # 「我们这儿」「你那边」是指代，不是地名。
        if city and re.fullmatch(r"(?:我|我们|你|你们|咱|咱们)?(?:这里|这儿|这边|这|那边|那里|那儿|那|家里|当地|本地)", city):
            city = ""
        if not city:
            city = (_last_city(conv) if re.search(r"那边|那里|那儿", raw) else None) or HOME_CITY
    else:
        # 例如上一轮问了北京天气，这一轮问「明天呢」。
        city = _last_city(conv) or HOME_CITY
    if not city:return None
    d = fetch(city, timeout=timeout)
    if d:
        _set_last_city(conv, d["name"])
    return d
