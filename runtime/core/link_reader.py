# -*- coding: utf-8 -*-
"""安全地读取网页正文（v0.59.0）。

只负责“把一条链接安全地抓回来”：只允许 http/https，挡本机/内网/云元数据，
跳转每一跳都复检，不执行 JS、不带 Cookie、限制大小和时间。
“只读哪条链接”由调用方（flow_media/flow_reply）做来源校验。
"""
import gzip
import io
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_URL = 2048
MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 20000
HOP_TIMEOUT = 8
TOTAL_TIMEOUT = 12
MAX_REDIRECTS = 3
ALLOWED_PORTS = (80, 443)
ALLOWED_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600
_CACHE_MAX = 50

_EXTRA_NETS = [ipaddress.ip_network(x) for x in ("100.64.0.0/10", "198.18.0.0/15")]


def normalize_url(raw):
    """把链接收拾成统一形式；不合格返回空串（调用方据此拒绝）。"""
    s = str(raw or "").strip().strip("<>\"'“”‘’")
    if not s or len(s) > MAX_URL:
        return ""
    try:
        u = urlsplit(s)
    except Exception:
        return ""
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https"):
        return ""
    if u.username or u.password:
        return ""
    host = (u.hostname or "").strip().rstrip(".").lower()
    if not host:
        return ""
    try:
        port = int(u.port or (443 if scheme == "https" else 80))
    except Exception:
        return ""
    if port not in ALLOWED_PORTS:
        return ""
    netloc = host
    if ":" in host:
        netloc = "[%s]" % host
    if (scheme == "http" and port != 80) or (scheme == "https" and port != 443):
        netloc = "%s:%d" % (netloc, port)
    path = u.path or "/"
    return urlunsplit((scheme, netloc, path, u.query, ""))


def _ip_public(ip):
    try:
        a = ipaddress.ip_address(str(ip))
    except Exception:
        return False
    mapped = getattr(a, "ipv4_mapped", None)
    if mapped is not None:
        a = mapped
    if a.version == 6:
        if (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                or a.is_reserved or a.is_unspecified):
            return False
        return True
    if (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
            or a.is_reserved or a.is_unspecified):
        return False
    for n in _EXTRA_NETS:
        if a in n:
            return False
    return True


def _literal_ip(host):
    """纯 IP / 整数型 / 十六进制型主机名都还原成 IP；不是则返回空串。"""
    h = str(host or "").strip().strip("[]")
    if not h:
        return ""
    if re.fullmatch(r"\d+", h):
        try:
            return str(ipaddress.ip_address(int(h)))
        except Exception:
            return ""
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", h):
        try:
            return str(ipaddress.ip_address(int(h, 16)))
        except Exception:
            return ""
    try:
        ipaddress.ip_address(h)
        return h
    except Exception:
        return ""


def _resolve_public(host, port):
    """返回 (可用 IP 列表, 拒绝原因)。域名解析出的 IP 必须全是公网地址。"""
    lit = _literal_ip(host)
    if lit:
        if _ip_public(lit):
            return [lit], ""
        return None, "这个网址指向本机/内网地址，不访问"
    h = str(host or "").lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return None, "本机地址不访问"
    try:
        infos = socket.getaddrinfo(h, port, type=socket.SOCK_STREAM)
    except Exception:
        return None, "域名解析失败"
    ips = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if not _ip_public(ip):
            return None, "这个网址指向本机/内网地址，不访问"
        if ip not in ips:
            ips.append(ip)
    if not ips:
        return None, "域名没有可用地址"
    return ips, ""


def _decode(data, ctype):
    cs = ""
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    if m:
        cs = m.group(1)
    if not cs:
        head = data[:4096].decode("ascii", "ignore")
        m = re.search(r"<meta[^>]+charset=[\"']?([\w\-]+)", head, re.I)
        if m:
            cs = m.group(1)
    for enc in (cs, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return data.decode(enc, "replace")
        except Exception:
            continue
    return data.decode("utf-8", "replace")


def _decompress(body, enc):
    if not body:
        return body
    if len(body) > MAX_BYTES:
        return None
    e = str(enc or "").lower()
    try:
        if "gzip" in e:
            # Bound expanded bytes too; a tiny response may contain a compression bomb.
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                decoded = stream.read(MAX_BYTES + 1)
        elif "deflate" in e:
            try:
                stream = zlib.decompressobj()
                decoded = stream.decompress(body, MAX_BYTES + 1)
            except zlib.error:
                stream = zlib.decompressobj(-zlib.MAX_WBITS)
                decoded = stream.decompress(body, MAX_BYTES + 1)
            if not stream.eof:
                return None
        else:
            return body
        return decoded if len(decoded) <= MAX_BYTES else None
    except Exception:
        return None


class _TextExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "title":
            self._in_title = True
        if t in ("script", "style", "noscript", "svg", "template", "iframe", "canvas"):
            self._skip += 1

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        if t in ("script", "style", "noscript", "svg", "template", "iframe", "canvas") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        s = str(data or "").strip()
        if s:
            self.parts.append(s)


def _extract(body):
    p = _TextExtractor()
    try:
        p.feed(body)
    except Exception:
        pass
    title = re.sub(r"\s+", " ", p.title or "").strip()
    text = "\n".join(p.parts)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return title[:120], text.strip()


def _request(url, timeout):
    u = urlsplit(url)
    scheme = (u.scheme or "").lower()
    host = (u.hostname or "").lower()
    port = int(u.port or (443 if scheme == "https" else 80))
    ips, why = _resolve_public(host, port)
    if not ips:
        return None, why
    ip = ips[0]
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    conn = None
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        # 连到刚才校验过的 IP，避免“校验后又解析到内网”的 DNS rebinding
        conn._create_connection = lambda address, t=None, sa=None: socket.create_connection(
            (ip, port), t or timeout, sa)
        host_header = host if port in (80, 443) else "%s:%d" % (host, port)
        conn.request("GET", path, headers={
            "Host": host_header,
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Connection": "close",
        })
        resp = conn.getresponse()
        headers = {}
        for k, v in resp.getheaders():
            headers[str(k).lower()] = v
        body = b""
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        return {"status": resp.status, "headers": headers, "body": body[:MAX_BYTES]}, ""
    except Exception as e:
        return None, "抓取失败（%s）" % type(e).__name__
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _fail(reason, url=""):
    return {"ok": False, "reason": str(reason or "没读成")[:80], "url": url}


def fetch_page(raw_url):
    url = normalize_url(raw_url)
    if not url:
        return _fail("这个链接格式不支持")
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    deadline = now + TOTAL_TIMEOUT
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        left = deadline - time.time()
        if left <= 0:
            return _fail("抓取超时", current)
        res, why = _request(current, max(1, min(HOP_TIMEOUT, left)))
        if res is None:
            return _fail(why, current)
        status = int(res.get("status") or 0)
        headers = res.get("headers") or {}
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location") or ""
            if not loc:
                return _fail("跳转没给地址", current)
            nxt = normalize_url(urljoin(current, loc))
            if not nxt:
                return _fail("跳转到了不支持的地址", current)
            current = nxt
            continue
        if status >= 400:
            return _fail("对方网站返回 %d" % status, current)
        ct = str(headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct and not any(ct.startswith(a) for a in ALLOWED_TYPES):
            return _fail("这个不是网页（%s），读不了" % ct[:40], current)
        body = _decompress(res.get("body") or b"", headers.get("content-encoding"))
        if body is None:
            return _fail("网页解压失败或超过大小上限", current)
        text = _decode(body, headers.get("content-type"))
        title, main = _extract(text)
        if len(main) < 30:
            return _fail("没读到正文（可能要登录或是动态页）", current)
        out = {"ok": True, "url": current, "title": title or current,
               "domain": (urlsplit(current).hostname or ""), "text": main[:MAX_CHARS]}
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[url] = (time.time(), out)
        return out
    return _fail("跳转太多次", current)
