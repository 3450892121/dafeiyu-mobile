# 已废弃（2026-10-21）：main.py 不再 import 本文件；管理页唯一入口是 pc/console.py。
# 保留文件仅作历史参考，admin_port=0 时不会启动。
import hmac, html, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import metrics

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


def _int_list_text(v):
    return ",".join(str(x) for x in (v or []))


def _parse_ids(text):
    out = []
    for part in str(text or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except Exception:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


class Admin(object):
    def __init__(self, cfg, store, port=8090, host="127.0.0.1", status_provider=None):
        self.cfg = cfg
        self.store = store
        self.port = port
        self.host = host
        self.status_provider = status_provider or (lambda: {})
        self.bot_home = os.path.dirname(os.path.dirname(os.path.abspath(cfg.path)))
        self.sticker_dir = os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "stickers", "images")
        self.history = []
        self.history_lock = threading.Lock()
        threading.Thread(target=self._sample_memory, name="memory", daemon=True).start()

    def _token(self):
        return str(self.cfg.get("admin_token") or "")

    # ---------- 采样 ----------
    def _memory(self):
        procs = metrics.process_list([self.bot_home], [os.getpid()])
        qq = sum(p["ws"] for p in procs if p["pid"] != os.getpid())
        bot = sum(p["ws"] for p in procs if p["pid"] == os.getpid())
        return {"total": qq + bot, "qq": qq, "bot": bot, "procs": len(procs), "list": procs}

    def _sample_memory(self):
        while True:
            try:
                m = self._memory()
                with self.history_lock:
                    self.history.append({"t": int(time.time()), "mb": round(m["total"] / 1048576.0, 1)})
                    if len(self.history) > 180:
                        self.history = self.history[-180:]
            except Exception:
                pass
            time.sleep(10)

    def _state(self):
        cfg = self.cfg
        mem = self._memory()
        with self.history_lock:
            hist = list(self.history)
        if not hist:
            hist = [{"t": int(time.time()), "mb": round(mem["total"] / 1048576.0, 1)}]
        recent = []
        try:
            recent = self.store.recent_messages(40)
        except Exception:
            pass
        logs = []
        try:
            logs = ["%s [%s] %s" % (r["t"], r["tag"], r["detail"]) for r in self.store.recent_logs(60)]
        except Exception:
            pass
        today = {"in": 0, "out": 0, "total": 0}
        try:
            today = self.store.today_counts()
        except Exception:
            pass
        stickers = 0
        try:
            stickers = len([n for n in os.listdir(self.sticker_dir) if not n.startswith(".")])
        except Exception:
            pass
        return {
            "bot": self.status_provider() or {},
            "config": cfg.public_dict(),
            "today": today,
            "stickers": stickers,
            "recent": recent,
            "logs": logs,
            "memory": {"total": mem["total"], "qq": mem["qq"], "bot": mem["bot"], "procs": mem["procs"], "list": mem["list"]},
            "history": hist,
        }

    # ---------- HTTP ----------
    def serve(self):
        admin = self
        token = admin._token()

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
                data = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

            def _qs(self):
                return parse_qs(urlsplit(self.path).query)

            def _token_from_request(self):
                q = self._qs()
                if q.get("token"):
                    return q["token"][0]
                h = self.headers.get("X-Admin-Token") or ""
                if h:
                    return h
                cookie = self.headers.get("Cookie") or ""
                for part in cookie.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "dafeiyu_admin":
                        return v
                return ""

            def _ok(self, supplied=""):
                if not token:
                    return False
                return hmac.compare_digest(str(supplied or ""), token)

            def _origin_ok(self):
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return origin == ("http://127.0.0.1:%d" % admin.port)

            def _deny(self):
                page = ("<meta charset='utf-8'><body style='background:#0f1520;color:#e7eefc;font-family:system-ui;padding:40px'>"
                        "<h3>需要访问口令</h3><p>请用 SSH 隧道打开 http://127.0.0.1:8090/，"
                        "或在地址后面加 <code>?token=口令</code>（口令在服务器 data/config.json 的 admin_token）。</p></body>")
                self._send(403, page)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/health":
                    self._json(200, {"ok": True})
                    return
                if not self._ok(self._token_from_request()):
                    self._deny()
                    return
                if path == "/api/state":
                    self._json(200, admin._state())
                    return
                if path == "/api/sticker":
                    try:
                        names = [n for n in os.listdir(admin.sticker_dir) if not n.startswith(".")]
                        name = names[int(time.time() * 7) % len(names)]
                        with open(os.path.join(admin.sticker_dir, name), "rb") as f:
                            data = f.read()
                        ctype = "image/gif" if name.endswith(".gif") else ("image/webp" if name.endswith(".webp") else "image/png")
                        self._send(200, data, ctype, {"Cache-Control": "public, max-age=300"})
                    except Exception:
                        self._send(404, "not found")
                    return
                try:
                    with open(PAGE_PATH, "r", encoding="utf-8") as f:
                        page = f.read()
                except Exception as e:
                    page = "<meta charset='utf-8'>dashboard.html 读取失败：%s" % html.escape(str(e))
                self._send(200, page, "text/html; charset=utf-8",
                           {"Set-Cookie": "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % token})

            def do_POST(self):
                path = urlsplit(self.path).path
                if not self._origin_ok():
                    self._json(403, {"ok": False, "error": "origin"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                except Exception:
                    raw = ""
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                if not self._ok(form.get("token") or self._token_from_request()):
                    self._json(403, {"ok": False, "error": "token"})
                    return
                if path == "/api/save":
                    try:
                        admin.apply(form)
                        self._json(200, {"ok": True})
                    except Exception as e:
                        self._json(500, {"ok": False, "error": str(e)})
                    return
                self._json(404, {"ok": False})

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer((self.host, self.port), H)
        srv.daemon_threads = True
        srv.serve_forever()

    # ---------- 保存设置 ----------
    def apply(self, form):
        cfg = self.cfg
        if form.get("api_key"):
            cfg.set("api_key", form["api_key"].strip())
        if "owner_qq" in form:
            try:
                cfg.set("owner_qq", int(str(form["owner_qq"]).strip() or 0))
            except Exception:
                cfg.set("owner_qq", 0)
        if "owner_name" in form:
            cfg.set("owner_name", form["owner_name"].strip()[:40])
        for key in ("model", "vision_model"):
            if key in form:
                cfg.set(key, form[key].strip())
        if "allow_users" in form:
            cfg.set("allow_users", _parse_ids(form["allow_users"]))
        if "allow_groups" in form:
            cfg.set("allow_groups", _parse_ids(form["allow_groups"]))
        if "group_need_at" in form:
            cfg.set("group_need_at", form.get("group_need_at") in ("1", "on", "true", "True"))
        for key in ("group_trigger_name", "group_trigger_quote"):
            if key in form:
                cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
        for key in ("group_at_reply", "group_at_reply_at", "group_at_reply_name", "group_at_reply_quote"):
            if key in form:
                cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
        if "bot_rounds_enabled" in form:
            cfg.set("bot_rounds_enabled", form.get("bot_rounds_enabled") in ("1", "on", "true", "True"))
        if "bot_peers_qq" in form:
            cfg.set("bot_peers_qq", str(form.get("bot_peers_qq") or "").replace("，", ",").strip()[:200])
        for key in ("bot_round_limit", "bot_round_reset_min"):
            if key in form:
                try:
                    cfg.set(key, max(0, int(str(form.get(key)).strip() or 0)))
                except Exception:
                    pass
        # v0.40.0 表情包判定参数（在「表情包」页单独保存；只改提交上来的键）
        for key, lo, hi in (("sticker_recall_count", 1, 20),):
            if key in form:
                try:
                    cfg.set(key, max(lo, min(hi, int(float(str(form[key]).strip() or 0)))))
                except Exception:
                    pass
        if "sticker_send_gap_sec" in form:
            try:
                cfg.set("sticker_send_gap_sec",
                        max(0.0, min(10.0, float(str(form.get("sticker_send_gap_sec")).strip() or 0))))
            except Exception:
                pass
        for key in ("sticker_judge_enabled", "sticker_wait_ready", "sticker_text_only"):
            if key in form:
                cfg.set(key, str(form.get(key) or "").strip().lower() in ("1", "on", "true", "yes"))
        # v0.41.0 新引擎参数
        for key, lo, hi in (("sticker_brief_history", 5, 50), ("sticker_panel_size", 4, 8)):
            if key in form:
                try:
                    cfg.set(key, max(lo, min(hi, int(float(str(form[key]).strip() or 0)))))
                except Exception:
                    pass
        for key in ("sticker_conf_min", "sticker_text_match_min", "sticker_rel_min"):
            if key in form:
                try:
                    cfg.set(key, max(0.0, min(1.0, float(str(form.get(key)).strip() or 0))))
                except Exception:
                    pass
        for key, allowed in (("sticker_recheck", ("high", "always", "off")),):
            if key in form:
                v = str(form.get(key) or "").strip().lower()
                if v in allowed:
                    cfg.set(key, v)
