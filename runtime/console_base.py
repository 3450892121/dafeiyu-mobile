# -*- coding: utf-8 -*-
"""大肥鱼 · 控制台（常驻）：所有操作都从这里走。
- 管理 NapCat 协议端（启动/停止/重启/二维码）
- 管理机器人大脑（启动/停止/重启）
- 面板：状态、内存、消息、日志、设置
"""
import hashlib, hmac, json, os, re, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BOT_DIR)
sys.path.insert(0, os.path.join(BOT_DIR, "core"))

import metrics
import localllm
from model import Ark, ModelPool
from version import VERSION
import sticker_index
import sticker_cards
import sticker_duplicates
import ops_api
import usage_board
import voice
import recent_topic
import reply_autonomy
from config import Config
from store import Store
from util import acquire_single_instance, dpapi_decrypt as DEC, clean_time_windows

IS_WIN = os.name == "nt"
BOT_HOME = os.environ.get("DAFEIYU_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DafeiyuBot")
DATA_DIR = os.path.join(BOT_HOME, "data")
NAPCAT_DIR = os.path.join(BOT_HOME, "napcat")
QQ_EXE = os.path.join(BOT_HOME, "qq", "QQ.exe")
PROFILE_DIR = os.path.join(BOT_HOME, "qq-profile")
# 服务器版：协议端跑在 Docker 里，配置和缓存都挂到 NAPCAT_DIR（路径和 Windows 版同名，面板不用改）
DEPLOY_DIR = os.environ.get("DAFEIYU_DEPLOY_DIR") or os.path.join(BOT_DIR, "deploy")
COMPOSE_FILE = os.path.join(DEPLOY_DIR, "docker-compose.yml")
CONTAINER = os.environ.get("DAFEIYU_NAPCAT_CONTAINER") or "dafeiyu-napcat"
BRAIN = os.path.join(BOT_DIR, "mobile_brain.py")
PAGE = os.path.join(BOT_DIR, "core", "dashboard.html")
OPS_PAGE = os.path.join(BOT_DIR, "core", "ops.html")
VIDEO_PAGE = os.path.join(BOT_DIR, "core", "video.html")
KB_PAGE = os.path.join(BOT_DIR, "core", "kb.html")
ICON = os.path.join(BOT_DIR, "assets", "dafeiyu.ico")
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
# v0.66.0：多实例（第二只小号）每只一把锁，锁名按 BOT_HOME 区分；老的单实例行为不变。
# 想手动指定锁名时用环境变量 DAFEIYU_CONSOLE_MUTEX。
CONSOLE_MUTEX = os.environ.get("DAFEIYU_CONSOLE_MUTEX") or (
    "Global\\DafeiyuConsole_" + hashlib.sha1(BOT_HOME.encode("utf-8")).hexdigest()[:12])


def _embed_one(cfg, text):
    try:
        text = str(text or "").strip()
        if not text:
            return None
        vs = sticker_index.embed_texts(cfg, [text[:800]])
        return vs[0] if vs else None
    except Exception:
        return None


def _id_list(v):
    out = []
    for x in v or []:
        try:
            n = int(x)
        except Exception:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def _pythonw():
    if not IS_WIN:
        return sys.executable
    exe = sys.executable
    pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return pw if os.path.exists(pw) else exe


VENDORS = [
    ("volces.com", "火山方舟"),
    ("api.deepseek.com", "DeepSeek 官方"),
    ("dashscope.aliyuncs.com", "阿里百炼"),
    ("api.openai.com", "OpenAI"),
    ("api.moonshot.cn", "月之暗面"),
    ("open.bigmodel.cn", "智谱"),
    ("api.siliconflow.cn", "硅基流动"),
    ("api.minimax.chat", "MiniMax"),
    ("api.baichuan-ai.com", "百川"),
    ("api.lingyiwanwu.com", "零一万物"),
]


def _vendor_of(base_url):
    """按接口域名认出厂商；本地地址就是「本地」；认不出就把域名照实给出来，别编。"""
    u = str(base_url or "").lower()
    try:
        host = urlsplit(u).hostname or ""
    except Exception:
        host = ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return "本地"
    for key, name in VENDORS:
        if key in u:
            return name
    return host or "未知厂商"


def _write_jsonl(path, items):
    """原子写 stickers.jsonl，避免面板写到一半被大脑 reload。

    added 是面板根据文件时间算出来的展示字段，不许写回文件（会越写越乱）。
    """
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            row = {k: v for k, v in dict(it).items() if k != "added"}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


AUTO_TAG_PROMPT = (
    "这是一张 QQ 聊天用的表情包。只输出两行，不要任何解释、不要加标点说明：\n"
    "第一行：3~6 个中文短标签，用英文逗号分隔（例：开心,可爱,无语）\n"
    "第二行：不超过 25 字的画面描述（谁在做什么、什么情绪）"
)


def _sha1_of(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dhash_of(path):
    """64 位感知哈希：图被缩放/另存一遍也能认出来是同一张。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("L").resize((9, 8), Image.LANCZOS)
            px = list(im.getdata())
    except Exception:
        return ""
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return "%016x" % bits


def _ham(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 99


def _vision_client(cfg):
    """挑一个能看图的模型：优先 vision_model_id，否则第一个 enabled 的。"""
    models = list(cfg.get("models") or [])
    vid = str(cfg.get("vision_model_id") or "")
    order = [m for m in models if m.get("id") == vid] + [m for m in models if m.get("id") != vid]
    for m in order:
        if not m.get("enabled"):
            continue
        conf = dict(m)
        if not conf.get("api_key"):
            conf["api_key"] = cfg.get("api_key") or ""
        if not conf.get("api_key") or not conf.get("base_url") or not conf.get("model"):
            continue
        if "127.0.0.1" in str(conf.get("base_url")) or "localhost" in str(conf.get("base_url")):
            continue
        return Ark(conf, fallback_key=cfg.get("api_key") or "", thinking_enabled=False)
    return None


def _auto_tag_image(cfg, path, name):
    """让视觉模型看一眼，返回 (tags, note)；失败返回 (None, '')。"""
    ark = _vision_client(cfg)
    if ark is None:
        return None, ""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > 6 * 1024 * 1024:
            return None, ""
        mime = "image/png" if str(name).lower().endswith(".png") else "image/jpeg"
        r = ark.vision(AUTO_TAG_PROMPT, data, mime, timeout=40)
        if not r.ok or not r.text:
            return None, ""
        lines = [x.strip() for x in r.text.replace("\r", "").split("\n") if x.strip()]
        if not lines:
            return None, ""
        raw_tags = lines[0]
        for sep in ("，", "、", ";", "；", " "):
            raw_tags = raw_tags.replace(sep, ",")
        tags = [t.strip(" #·-。.") for t in raw_tags.split(",")]
        tags = [t for t in dict.fromkeys(tags) if t and len(t) <= 8][:6]
        note = lines[1][:40] if len(lines) > 1 else ""
        if not tags:
            return None, ""
        return tags, note
    except Exception:
        return None, ""


class Supervisor(object):
    def __init__(self):
        self.cfg = Config()
        self.store = Store(os.path.join(DATA_DIR, "bot.db"))
        # v0.53.0 运维页后端：清单/探针结果/告警统一放在大肥鱼数据目录（三只共用一个）
        # 户口本/探针/告警放主实例的数据目录（多只共用一份清单）；自测可用 DAFEIYU_SHARED_DATA 覆盖
        _shared = os.environ.get("DAFEIYU_SHARED_DATA") or DATA_DIR
        if not (os.name != "nt" and os.path.isdir(_shared)):
            _shared = DATA_DIR
        _deploy = os.environ.get("DAFEIYU_DEPLOY_DIR") or os.path.join(BOT_DIR, "deploy")
        self.ops = ops_api.OpsApi(_shared, PROJECT_ROOT, cfg=self.cfg,
                                  fallback_persona_dir=os.path.join(PROJECT_ROOT, "appassets"),
                                  deploy_dir=_deploy)
        self.port = int(self.cfg.get("admin_port") or 0) or 8090
        # v0.66.0：NapCat WebUI / OneBot 端口也从配置读（多实例各用各的，不再写死 6099/3000）
        self.webui_port = int(self.cfg.get("napcat_webui_port") or 0) or 6099
        _api = str(self.cfg.get("onebot_api") or "")
        _m = re.search(r":(\d+)", _api)
        self.onebot_port = int(_m.group(1)) if _m else 3000
        self.lock = threading.RLock()
        self.history = []
        self.started = int(time.time())
        self.user_stopped = False
        self.last_qr_mtime = 0
        self._brain_starting = 0
        self._sticker_lock = threading.RLock()
        self._sticker_mtime = 0
        self._sticker_items = []
        self.idx_state = {"running": False, "done": 0, "total": 0, "msg": "", "ok": None, "files": 0}
        self.st_job = {"running": False, "kind": "", "done": 0, "total": 0, "msg": "", "ok": None,
                       "removed": 0, "tagged": 0}
        self._proc_cache = None
        self._proc_cache_at = 0
        threading.Thread(target=self._sample, daemon=True).start()
        threading.Thread(target=self._auto_start, daemon=True).start()
        threading.Thread(target=self._clean_logs, daemon=True).start()
        threading.Thread(target=self._backup_loop, daemon=True).start()
        if self.cfg.get("local_llm_autostart"):
            threading.Thread(target=self._local_autostart, daemon=True).start()

    def _local_autostart(self):
        """配置里开了「随控制台自动启动本地模型」就先把它拉起来（默认关，省显存）。"""
        try:
            time.sleep(6)
            if not localllm.status(self.cfg)["running"]:
                localllm.start(self.cfg)
        except Exception as e:
            try:
                with open(os.path.join(DATA_DIR, "console.log"), "a", encoding="utf-8") as f:
                    f.write("%s local_autostart failed: %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
            except Exception:
                pass

    def _clean_logs(self):
        """NapCat 文件日志只留 7 天、最多 10 个。"""
        while True:
            try:
                d = os.path.join(NAPCAT_DIR, "logs")
                if os.path.isdir(d):
                    files = sorted([os.path.join(d, f) for f in os.listdir(d)],
                                   key=lambda p: os.path.getmtime(p), reverse=True)
                    for p in files[10:]:
                        os.remove(p)
                    cutoff = time.time() - 7 * 86400
                    for p in files:
                        if os.path.getmtime(p) < cutoff and os.path.exists(p):
                            os.remove(p)
            except Exception:
                pass
            time.sleep(6 * 3600)

    def _backup_loop(self):
        while True:
            try:
                self._backup_once()
            except Exception:
                pass
            time.sleep(6 * 3600)

    def _backup_once(self):
        src = os.path.join(DATA_DIR, "bot.db")
        if not os.path.exists(src):
            return
        bdir = os.path.join(DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        dst = os.path.join(bdir, "bot-%s.db" % time.strftime("%Y%m%d-%H%M"))
        if os.path.exists(dst):
            return
        a = sqlite3.connect(src)
        b = sqlite3.connect(dst)
        with b:
            a.backup(b)
        a.close()
        b.close()
        files = sorted(f for f in os.listdir(bdir) if f.startswith("bot-") and f.endswith(".db"))
        for old in files[:-7]:
            try:
                os.remove(os.path.join(bdir, old))
            except Exception:
                pass

    def _auto_start(self):
        last_qr = 0
        while True:
            try:
                if self.user_stopped:
                    time.sleep(8)
                    continue
                info = self.login_info()
                if info:
                    if not self.brain_running():
                        self.start_brain()
                else:
                    if self.napcat_running():
                        uin = str(self.cfg.get("login_uin") or "").strip()
                        if uin and time.time() - getattr(self, "_quick_tried", 0) > 60:
                            self._quick_tried = time.time()
                            if self.quick_login():
                                log_line = "已用快速登录拉起小号 %s" % uin
                                try:
                                    with open(os.path.join(DATA_DIR, "console.log"), "a", encoding="utf-8") as f:
                                        f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), log_line))
                                except Exception:
                                    pass
                                time.sleep(8)
                                continue
                        qr = os.path.join(NAPCAT_DIR, "cache", "qrcode.png")
                        age = (time.time() - os.path.getmtime(qr)) if os.path.exists(qr) else 999
                        if age > 60 and time.time() - last_qr > 60:
                            self.refresh_qr()
                            last_qr = time.time()
            except Exception:
                pass
            time.sleep(8)

    # ---------- 进程 ----------
    def _log_console(self, msg):
        try:
            with open(os.path.join(DATA_DIR, "console.log"), "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    def _own_display(self):
        """读自己 QQ 进程的 DISPLAY（停服时用来只清自己的虚拟屏）。"""
        for pid in self.qq_pids():
            try:
                with open("/proc/%d/environ" % pid, "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith(b"DISPLAY="):
                            return kv.decode("utf-8", "replace").strip()
            except Exception:
                continue
        return ""

    def _kill_own_xvfb(self, disp):
        """只杀自己这个 DISPLAY 的 Xvfb；别的实例的虚拟屏一个都不碰。"""
        if not disp or not disp.startswith(":"):
            return
        try:
            subprocess.run(["pkill", "-f", "Xvfb " + disp], capture_output=True, timeout=5)
        except Exception:
            pass

    def qq_pids(self):
        """QQ 副本的进程：服务器版是 xvfb-run 拉起的 /home/dafeiyu/DafeiyuBot/qq/qq。"""
        return metrics.pids_under([os.path.join(BOT_HOME, "qq")])

    def brain_pids(self):
        try:
            with open(os.path.join(DATA_DIR, "brain.pid"), "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            if metrics.pid_alive(pid):
                exe = ""
                try:
                    exe = metrics.pid_exe(pid)
                except Exception:
                    exe = ""
                if exe and "python" not in exe.lower() and "pythonw" not in exe.lower():
                    return []
                return [pid]
            # 僵尸也算“不在了”：顺手 wait 掉，避免每次自愈重启留一个僵尸
            try:
                os.waitpid(pid, os.WNOHANG)
            except Exception:
                pass
        except Exception:
            pass
        return []

    def napcat_running(self):
        return bool(self.qq_pids())

    def brain_running(self):
        return bool(self.brain_pids())

    def start_napcat(self):
        self.user_stopped = False
        with self.lock:
            if self.napcat_running():
                return True
            if not IS_WIN:
                # 服务器版：xcffb 虚拟屏 + QQ 副本 + 注入的 NapCat（无桌面环境也能跑）
                qq_bin = os.path.join(BOT_HOME, "qq", "qq")
                if not os.path.exists(qq_bin):
                    self._log_console("start_napcat failed: 没找到 QQ 副本 %s" % qq_bin)
                    return False
                env = os.environ.copy()
                # v0.66.0：多实例连 HOME 也要分开。只给 --user-data-dir 不够——QQ/NapCat 还会写
                # ~/.config/QQ（MMKV 锁、崩溃文件），两只共用会让第二只在「等待确认」之后
                # worker 退出码 15 / GPU 进程起不来（2026-10-28 真机踩到）。大肥鱼保持原 HOME。
                _real_home = os.path.expanduser("~")
                if os.path.realpath(BOT_HOME) == os.path.realpath(os.path.join(_real_home, "DafeiyuBot")):
                    env["HOME"] = _real_home
                else:
                    env["HOME"] = BOT_HOME
                    os.makedirs(os.path.join(BOT_HOME, ".config"), exist_ok=True)
                env.setdefault("LANG", "C.UTF-8")
                env["PYTHONIOENCODING"] = "utf-8"
                try:
                    os.makedirs(os.path.join(NAPCAT_DIR, "cache"), exist_ok=True)
                    # v0.66.0：每只小号一套自己的 QQ 数据目录（不加这个会共用 ~/.config/QQ，两只互踢）
                    os.makedirs(PROFILE_DIR, exist_ok=True)
                    logf = open(os.path.join(DATA_DIR, "napcat.log"), "ab", buffering=0)
                    # 记住的小号：用 -q 走快速登录，重启后不用再扫码（第一次扫码登录后写这个文件）
                    extra = []
                    try:
                        with open(os.path.join(DATA_DIR, "login_uin"), "r", encoding="utf-8") as f:
                            uin = f.read().strip()
                        if uin.isdigit():
                            extra = ["-q", uin]
                    except Exception:
                        extra = []
                    # v0.66.0：第二只小号在虚拟屏里 GPU 进程起不来（error_code=1002，
                    # GPU process isn't usable → worker 退出码 15），关掉 GPU 更稳；反正无界面。
                    subprocess.Popen(["xvfb-run", "-a", qq_bin, "--no-sandbox", "--disable-gpu",
                                      "--user-data-dir=" + PROFILE_DIR] + extra,
                                     cwd=NAPCAT_DIR, env=env, stdin=subprocess.DEVNULL,
                                     stdout=logf, stderr=logf, start_new_session=True, close_fds=True)
                except Exception as e:
                    self._log_console("start_napcat failed: %r" % (e,))
                    return False
                for _ in range(20):
                    time.sleep(1)
                    if self.napcat_running():
                        return True
                return self.napcat_running()
            env = os.environ.copy()
            env.update({
                "NAPCAT_PATCH_PACKAGE": os.path.join(NAPCAT_DIR, "qqnt.json"),
                "NAPCAT_LOAD_PATH": os.path.join(NAPCAT_DIR, "loadNapCat.js"),
                "NAPCAT_INJECT_PATH": os.path.join(NAPCAT_DIR, "NapCatWinBootHook.dll"),
                "NAPCAT_LAUNCHER_PATH": os.path.join(NAPCAT_DIR, "NapCatWinBootMain.exe"),
                "NAPCAT_MAIN_PATH": os.path.join(NAPCAT_DIR, "napcat.mjs"),
            })
            os.makedirs(PROFILE_DIR, exist_ok=True)
            try:
                subprocess.Popen(
                    [os.path.join(NAPCAT_DIR, "NapCatWinBootMain.exe"), QQ_EXE,
                     os.path.join(NAPCAT_DIR, "NapCatWinBootHook.dll"), "--user-data-dir=" + PROFILE_DIR],
                    cwd=NAPCAT_DIR, env=env, creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                return True
            except Exception as e:
                self._log_console("start_napcat failed: %r" % (e,))
                return False

    def stop_napcat(self):
        self.user_stopped = True
        for pid in self.qq_pids():
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        if not IS_WIN:
            # v0.66.0：先记住自己这套的屏幕号，再杀 QQ；最后只清自己的虚拟屏。
            # 老代码 pkill -f "Xvfb :" 会把另一只小号的虚拟屏一起杀掉（多实例直接互相踢）。
            own_display = self._own_display()
            for _ in range(20):
                time.sleep(0.3)
                if not self.napcat_running():
                    break
            for pid in self.qq_pids():
                try:
                    import signal
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            self._kill_own_xvfb(own_display)
        # 2026-10-25：返回真实结果，stop_all/面板才知道有没有真的停掉
        return not self.napcat_running()

    def start_brain(self):
        self.user_stopped = False
        with self.lock:
            if self.brain_running():
                return True
            if time.time() - getattr(self, "_brain_starting", 0) < 15:
                return True
            self._brain_starting = time.time()
            env = os.environ.copy()
            env["DAFEIYU_HOME"] = BOT_HOME
            env["PYTHONIOENCODING"] = "utf-8"
            kw = {}
            if IS_WIN:
                kw["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
            else:
                kw["start_new_session"] = True
            try:
                subprocess.Popen([_pythonw(), BRAIN], cwd=BOT_DIR, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            except Exception as e:
                self._brain_starting = 0
                self._log_console("start_brain failed: %r" % (e,))
                return False
            for _ in range(12):
                time.sleep(0.5)
                if self.brain_running():
                    self._brain_starting = 0
                    return True
            self._brain_starting = 0
            return self.brain_running()

    def stop_brain(self):
        self.user_stopped = True
        alive = []
        for pid in self.brain_pids():
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    # 2026-10-25：从 3 秒加到 15 秒。大脑收到 SIGTERM 后会停止接新事件、
                    # drain 队列（最多 12 秒）再关库；等太短会 SIGKILL 掉正在处理的回复，
                    # 还容易留下没 checkpoint 的 WAL。
                    for _ in range(150):
                        time.sleep(0.1)
                        if not metrics.pid_alive(pid):
                            break
                    if metrics.pid_alive(pid):
                        os.kill(pid, signal.SIGKILL)
                        # SIGKILL 发出去到进程消失有一小段窗口，别立刻判它“还活着”
                        for _ in range(20):
                            time.sleep(0.1)
                            if not metrics.pid_alive(pid):
                                break
            except Exception:
                pass
            if metrics.pid_alive(pid):
                alive.append(pid)
        try:
            os.remove(os.path.join(DATA_DIR, "brain.pid"))
        except Exception:
            pass
        if alive:
            self._log_console("stop_brain failed: pids still alive %s" % alive)
            return False
        return True

    # ---------- NapCat WebUI（刷新二维码 / 快速登录）----------
    def _webui_cred(self):
        try:
            import hashlib
            import urllib.request
            w = json.load(open(os.path.join(NAPCAT_DIR, "config", "webui.json"), encoding="utf-8"))
            token = str(w.get("token") or "")
            if not token:
                return None
            h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
            body = json.dumps({"hash": h}).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/api/auth/login" % self.webui_port, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=6) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            return ((obj.get("data") or {}).get("Credential") or obj.get("Credential"))
        except Exception:
            return None

    def _webui_post(self, path, body=None):
        try:
            import urllib.request
            cred = self._webui_cred()
            if not cred:
                return None
            req = urllib.request.Request(("http://127.0.0.1:%d" % self.webui_port) + path,
                                         data=json.dumps(body or {}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + cred}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def quick_login(self):
        """用已保存的小号快速登录（不会碰主号）。"""
        uin = str(self.cfg.get("login_uin") or "").strip()
        owner = str(self.cfg.get("owner_qq") or "").strip()
        if not uin:
            return False
        # 安全闸：绝不用主号自动登录
        if owner and uin == owner:
            return False
        res = self._webui_post("/api/QQLogin/SetQuickLogin", {"uin": uin})
        return bool(res and res.get("code") == 0)

    def login_info(self):
        try:
            import urllib.request
            body = json.dumps({"echo": "console"}).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/get_login_info" % self.onebot_port, data=body,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + str(self.cfg.get("onebot_token"))},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            if obj.get("status") == "ok" and obj.get("data"):
                # v0.66.0：登录成功就把账号记住（以前 login_uin 只能装的时候写一次，
                # 第二只小号扫码登录后没人写，重启又要扫）。
                self._remember_uin(obj["data"].get("user_id"))
                return obj["data"]
        except Exception:
            pass
        return None

    def _remember_uin(self, uin):
        """把已登录的小号账号写进 config 和 data/login_uin（只写一次，变了才写）。"""
        try:
            uin = int(uin)
        except Exception:
            return
        if uin <= 0:
            return
        if int(self.cfg.get("login_uin") or 0) == uin:
            return
        try:
            self.cfg.set("login_uin", uin)
        except Exception:
            return
        try:
            path = os.path.join(DATA_DIR, "login_uin")
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(uin))
            os.chmod(path, 0o600)
            self._log_console("已记住登录账号 login_uin=%d" % uin)
        except Exception:
            pass

    def refresh_qr(self):
        """优先让 NapCat 自己换码（不重启、不丢登录）；不行才重启。"""
        res = self._webui_post("/api/QQLogin/RefreshQRcode", {})
        if res and res.get("code") == 0:
            time.sleep(1)
            return True
        self.stop_napcat()
        time.sleep(2)
        try:
            os.remove(os.path.join(NAPCAT_DIR, "cache", "qrcode.png"))
        except Exception:
            pass
        self.start_napcat()
        for _ in range(15):
            time.sleep(2)
            if os.path.exists(os.path.join(NAPCAT_DIR, "cache", "qrcode.png")):
                return True
        return False

    # ---------- 状态 ----------
    def _sample(self):
        while True:
            try:
                procs = metrics.process_list([BOT_HOME], self._extra_pids())
                total = sum(p["ws"] for p in procs)
                with self.lock:
                    self.history.append({"t": int(time.time()), "mb": round(total / 1048576.0, 1)})
                    if len(self.history) > 180:
                        self.history = self.history[-180:]
            except Exception:
                pass
            time.sleep(10)

    def _extra_pids(self):
        try:
            return [os.getpid()] + self.brain_pids()
        except Exception:
            return [os.getpid()]

    def db_conn(self):
        path = os.path.join(DATA_DIR, "bot.db")
        if not os.path.exists(path):
            return None
        try:
            con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            return con
        except Exception:
            return None

    def decayed_memories(self, limit=200):
        """v0.63.0：面板「已经淡忘的」列表；只读，捞回来走 /api/memory_restore。"""
        con = self.db_conn()
        if not con:
            return []
        try:
            people = {}
            for r in con.execute("SELECT qq,name FROM people").fetchall():
                people[int(r["qq"])] = r["name"] or str(r["qq"])
            out = []
            for r in con.execute(
                    "SELECT id,qq,text,kind,importance,created,decayed_at,use_count FROM person_memories "
                    "WHERE status='decayed' ORDER BY decayed_at DESC LIMIT ?", (int(limit),)).fetchall():
                nm = people.get(int(r["qq"])) or str(r["qq"])
                out.append({"id": "p%d" % r["id"], "table": "person_memories", "scope": "person",
                            "name": "%s（QQ%d）" % (nm, r["qq"]), "text": DEC(r["text"]) or "",
                            "kind": r["kind"], "importance": r["importance"],
                            "created": r["created"], "decayed_at": r["decayed_at"], "use_count": r["use_count"]})
            for r in con.execute(
                    "SELECT id,gid,text,kind,importance,created,decayed_at,use_count FROM group_memories "
                    "WHERE status='decayed' ORDER BY decayed_at DESC LIMIT ?", (int(limit),)).fetchall():
                out.append({"id": "g%d" % r["id"], "table": "group_memories", "scope": "group",
                            "name": "群%d" % r["gid"], "text": DEC(r["text"]) or "",
                            "kind": r["kind"], "importance": r["importance"],
                            "created": r["created"], "decayed_at": r["decayed_at"], "use_count": r["use_count"]})
            # v0.63.3：共同记忆也要列出来——memory_stats 里的 decayed 数是三张表相加的，
            # 这里少一张就会出现「统计说 12 条、列表只有 9 条」的对不上。
            for r in con.execute(
                    "SELECT id,text,kind,importance,created,decayed_at,use_count FROM shared_memories "
                    "WHERE status='decayed' ORDER BY decayed_at DESC LIMIT ?", (int(limit),)).fetchall():
                out.append({"id": "s%d" % r["id"], "table": "shared_memories", "scope": "shared",
                            "name": "共同记忆", "text": DEC(r["text"]) or "",
                            "kind": r["kind"], "importance": r["importance"],
                            "created": r["created"], "decayed_at": r["decayed_at"], "use_count": r["use_count"]})
            out.sort(key=lambda x: int(x.get("decayed_at") or 0), reverse=True)
            return out[:int(limit)]
        except Exception:
            return []
        finally:
            con.close()

    def memory_stats(self):
        """v0.55.1：面板概览显示记忆条数（人物/群/原话库），只读。"""
        con = self.db_conn()
        if not con:
            return {}
        try:
            def one(sql):
                try:
                    return int(con.execute(sql).fetchone()[0])
                except Exception:
                    return 0
            return {
                "person": one("SELECT COUNT(*) FROM person_memories WHERE status='active'"),
                "group": one("SELECT COUNT(*) FROM group_memories WHERE status='active'"),
                "quotes": one("SELECT COUNT(*) FROM memory_quotes"),
                "events": one("SELECT COUNT(*) FROM person_memories WHERE status='active' AND kind='event'"),
                # v0.63.0：已经淡忘的（还能捞回来）
                "decayed": (one("SELECT COUNT(*) FROM person_memories WHERE status='decayed'")
                            + one("SELECT COUNT(*) FROM group_memories WHERE status='decayed'")
                            + one("SELECT COUNT(*) FROM shared_memories WHERE status='decayed'")),
                "commitment": one("SELECT COUNT(*) FROM person_memories WHERE status='active' AND kind='commitment'"),
                # v0.69.0：底片（淡出后原文存档，只给地址，不进上下文）
                "archive": one("SELECT COUNT(*) FROM memory_archive"),
            }
        except Exception:
            return {}
        finally:
            con.close()

    def recent_messages(self, limit=40):
        con = self.db_conn()
        if not con:
            return []
        try:
            rows = con.execute("SELECT id,ts,direction,text,sender_name,conv,attachments FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in reversed(rows):
                try:
                    att = json.loads(r["attachments"] or "[]")
                except Exception:
                    att = []
                out.append({"id": r["id"], "ts": r["ts"], "dir": r["direction"], "text": DEC(r["text"]) or "",
                            "who": r["sender_name"] or "", "conv": r["conv"], "attachments": att})
            return out
        except Exception:
            return []
        finally:
            con.close()



    def current_mood(self):
        """v0.64.0：面板心情卡只取主人的当前主导情绪；不报数字给聊天对象。"""
        try:
            owner = int(self.cfg.get("owner_qq") or 0)
            items = self.store.get_emotions(owner) or []
            if not items:
                return {}
            names = {"jealousy": "吃醋", "upset": "不满", "sad": "难过", "happy": "开心", "shy": "害羞"}
            it = items[0]
            val = float(it.get("intensity") or 0)
            level = "有一点" if val < 26 else ("明显" if val < 46 else ("挺强" if val < 66 else "很强"))
            return {"kind": it.get("kind"), "name": names.get(it.get("kind"), "心情"),
                    "intensity": round(val, 1), "level": level, "reason": it.get("reason") or ""}
        except Exception:
            return {}

    def notebook_rows(self, limit=200):
        """v0.64.0：小本本（全局一本），面板只读列出；删走 /api/notebook_delete。"""
        con = self.db_conn()
        if not con:
            return []
        try:
            rows = con.execute("SELECT * FROM notebook WHERE status='active' "
                               "ORDER BY importance DESC, created DESC LIMIT ?", (int(limit),)).fetchall()
            out = []
            for r in rows:
                out.append({"id": int(r["id"]), "text": DEC(r["text"]) or "",
                            "kind": r["kind"] or "note", "importance": int(r["importance"] or 3),
                            "private": int(r["private"] or 0), "source_conv": r["source_conv"] or "",
                            "source_uid": int(r["source_uid"] or 0), "created": int(r["created"] or 0)})
            return out
        except Exception:
            return []
        finally:
            con.close()

    def recent_topic_rows(self, limit=50):
        """只展示目前仍会用于该会话的近期话题，过期或已清空的不展示。"""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT k,v FROM settings WHERE k LIKE 'recent_topic:%' LIMIT 500").fetchall()
        names = self.conv_names()
        out = []
        for row in rows:
            conv = str(row["k"] or "")[len("recent_topic:"):]
            if not re.fullmatch(r"[pg]:\d+", conv):
                continue
            try:
                payload = json.loads(DEC(row["v"]) or "{}")
                text = recent_topic.read(self.store, conv)
                ts = int(payload.get("ts") or 0)
                if not text or not ts:
                    continue
                out.append({"conv": conv, "name": names.get(conv) or ("群 " if conv.startswith("g:") else "私聊 ") + conv[2:],
                            "text": text, "updated_at": ts, "expires_at": ts + recent_topic.WINDOW})
            except (ValueError, TypeError, AttributeError):
                continue
        return sorted(out, key=lambda x: x["updated_at"], reverse=True)[:max(1, min(100, int(limit)))]

    def memory_rows(self, limit=200):
        """记忆 v2：人物（按 QQ，所有群共享）+ 群。id 带前缀，前端原样回传。"""
        con = self.db_conn()
        if not con:
            return []
        try:
            people = {}
            for r in con.execute("SELECT qq,name FROM people").fetchall():
                people[int(r["qq"])] = r["name"] or str(r["qq"])
            out = []
            for r in con.execute("SELECT id,qq,text,kind,importance,created,content_updated_at,key_facts,protected,last_mentioned_at,retrieved_at FROM person_memories "
                                 "WHERE status='active' ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall():
                nm = people.get(int(r["qq"])) or str(r["qq"])
                label = "%s（QQ%d）" % (nm, r["qq"])
                out.append({"id": "p%d" % r["id"], "scope": "person", "conv": "q:%d" % r["qq"],
                            "name": label, "person_name": label, "text": DEC(r["text"]) or "",
                            "kind": r["kind"], "importance": r["importance"], "world": "person", "ts": r["created"],
                            "content_updated_at": int(r["content_updated_at"] or 0), "key_facts": str(r["key_facts"] or ""), "protected": bool(r["protected"]), "last_mentioned_at": int(r["last_mentioned_at"] or 0), "retrieved_at": int(r["retrieved_at"] or 0)})
            for r in con.execute("SELECT id,gid,text,kind,importance,created,content_updated_at,key_facts,protected,last_mentioned_at,retrieved_at FROM group_memories "
                                 "WHERE status='active' ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall():
                label = "群%d" % r["gid"]
                out.append({"id": "g%d" % r["id"], "scope": "group", "conv": "g:%d" % r["gid"],
                            "name": label, "group_name": label, "text": DEC(r["text"]) or "",
                            "kind": r["kind"], "importance": r["importance"], "world": "group", "ts": r["created"],
                            "content_updated_at": int(r["content_updated_at"] or 0), "key_facts": str(r["key_facts"] or ""), "protected": bool(r["protected"]), "last_mentioned_at": int(r["last_mentioned_at"] or 0), "retrieved_at": int(r["retrieved_at"] or 0)})
            out.sort(key=lambda x: x["ts"] or 0, reverse=True)
            return out
        except Exception:
            return []
        finally:
            con.close()

    def today_counts(self):
        con = self.db_conn()
        if not con:
            return {"in": 0, "out": 0, "total": 0}
        try:
            start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
            rows = con.execute("SELECT direction, COUNT(*) c FROM messages WHERE ts >= ? GROUP BY direction", (start,)).fetchall()
            got = {r["direction"]: r["c"] for r in rows}
            return {"in": got.get("in", 0), "out": got.get("out", 0), "total": sum(got.values())}
        except Exception:
            return {"in": 0, "out": 0, "total": 0}
        finally:
            con.close()

    def recent_skips(self, limit=100):
        con = self.db_conn()
        if not con:
            return []
        try:
            rows = con.execute(
                "SELECT ts,tag,detail FROM logs WHERE tag LIKE 'skip_%' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"t": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), "tag": r["tag"], "detail": r["detail"]}
                    for r in reversed(rows)]
        except Exception:
            return []
        finally:
            con.close()

    def model_labels(self):
        """模型 id -> 看得懂的名字（厂商 · 模型）；用量页/概览都用它，不再只丢一个 ark-flash。"""
        out = {}
        try:
            for m in (self.cfg.public_dict().get("models") or []):
                mid = str(m.get("id") or "").strip()
                if not mid:
                    continue
                label = str(m.get("name") or "").strip()
                if not label:
                    model = str(m.get("model") or "").strip()
                    label = (("%s · %s" % (_vendor_of(m.get("base_url")), model)).strip(" ·")
                             if model else mid)
                if m.get("enabled") is False:
                    label += "（已停用）"
                out[mid] = label
        except Exception:
            out = {}
        return out

    def usage_dashboard(self, rng="today", frm=0, to=0):
        """用量看板（v0.65.2）：一次给齐画图要的聚合，前端只管画。只读，不改库。"""
        con = self.db_conn()
        if not con:
            return {"ok": False, "error": "读不到 bot.db"}
        try:
            return usage_board.collect(con, self.cfg.get("models") or [], rng, frm, to)
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}
        finally:
            try:
                con.close()
            except Exception:
                pass

    def usage_stats(self):
        con = self.db_conn()
        if not con:
            return {}
        try:
            import time as _t
            now = _t.time()
            def start_of(offset_days=0):
                x = _t.localtime(now - offset_days * 86400)
                return int(_t.mktime((x.tm_year, x.tm_mon, x.tm_mday, 0, 0, 0, 0, 0, -1)))
            lt = _t.localtime(now)
            week = start_of(lt.tm_wday)
            month = int(_t.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
            out = {}
            labels = self.model_labels()
            for name, start, end in (("today", start_of(0), None), ("yesterday", start_of(1), start_of(0)),
                                     ("week", week, None), ("month", month, None)):
                sql = "SELECT model_id, SUM(prompt_tokens) p, SUM(completion_tokens) c FROM usage_records WHERE ts >= ?"
                args = [start]
                if end:
                    sql += " AND ts < ?"; args.append(end)
                rows = None
                try:
                    rows = con.execute(sql + " GROUP BY model_id", tuple(args)).fetchall()
                except Exception:
                    rows = []
                out[name] = [{"model": (r["model_id"] or "-").strip() or "-",
                              "label": labels.get((r["model_id"] or "").strip())
                                       or (((r["model_id"] or "").strip() + "（配置里已没有）")
                                           if (r["model_id"] or "").strip() else "-"),
                              "prompt": r["p"] or 0,
                              "completion": r["c"] or 0, "total": (r["p"] or 0) + (r["c"] or 0)} for r in rows]
            return out
        except Exception:
            return {}
        finally:
            con.close()

    def conversations(self):
        con = self.db_conn()
        if not con:
            return []
        try:
            names = self.conv_names()
            rows = con.execute("SELECT conv, COUNT(*) c, MAX(ts) last_ts FROM messages GROUP BY conv ORDER BY last_ts DESC LIMIT 100").fetchall()
            return [{"conv": r["conv"], "name": names.get(r["conv"], r["conv"]), "count": r["c"], "last_ts": r["last_ts"]}
                    for r in rows]
        except Exception:
            return []
        finally:
            con.close()

    def sticker_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "stickers")

    def sticker_thumb(self, name, recycle=False):
        """生成/复用 96px 缩略图，避免面板一次解码 300 张原图。"""
        name = os.path.basename(name or "")
        base = os.path.join(self.sticker_dir(), "recycle" if recycle else "images")
        src = os.path.join(base, name)
        if not name or not os.path.isfile(src):
            return None
        tdir = os.path.join(self.sticker_dir(), "thumbs")
        os.makedirs(tdir, exist_ok=True)
        dst = os.path.join(tdir, ("r-" if recycle else "") + name + ".jpg")
        try:
            if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
                return dst
            from PIL import Image
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((96, 96))
                im.save(dst, "JPEG", quality=80)
            return dst
        except Exception:
            return None

    def sticker_list(self):
        path = os.path.join(self.sticker_dir(), "stickers.jsonl")
        try:
            mt = os.path.getmtime(path) if os.path.exists(path) else 0
        except Exception:
            mt = 0
        if mt != self._sticker_mtime:
            items = []
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                items.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                items = []
            self._sticker_items = items
            self._sticker_mtime = mt
            self._sticker_added = None
        items = list(self._sticker_items)
        # 添加时间：老数据没有字段，就用图片文件本身的修改时间当“添加时间”（面板排序用）
        if getattr(self, "_sticker_added", None) is None:
            img_dir = os.path.join(self.sticker_dir(), "images")
            added = {}
            for it in items:
                key = it.get("file")
                name = os.path.basename(str(key or "").replace("\\", "/"))
                try:
                    # 必须保留小数秒：一次性导入的批次（450 张）在同一秒内落盘，
                    # 截成整数秒会让它们全挤成同一个时间，排序就退化成随机。
                    added[key] = round(os.path.getmtime(os.path.join(img_dir, name)), 3)
                except Exception:
                    added[key] = 0
            self._sticker_added = added
        out = []
        for it in items:
            row = dict(it)
            row["added"] = self._sticker_added.get(it.get("file"), 0)
            out.append(row)
        return out

    def sticker_count(self):
        return len(self.sticker_list())

    def invalidate_stickers(self):
        self._sticker_mtime = 0
        self._sticker_items = []

    def sticker_hashes(self):
        names = {os.path.basename(str(x.get("file") or "").replace("\\", "/")) for x in self.sticker_list()}
        # Include images from the current multi-file upload before its metadata commit.
        for name in (getattr(self, "_sticker_upload_names", None) or set()):
            if os.path.isfile(os.path.join(self.sticker_dir(), "images", name)):
                names.add(name)
        self._sticker_hash = sticker_duplicates.load_hashes(self.sticker_dir(), names, getattr(self, "_sticker_hash", None))
        return self._sticker_hash

    def remember_hash(self, name, file_path):
        pending = getattr(self, "_sticker_upload_names", None) or set()
        pending.add(name)
        self._sticker_upload_names = pending
        self.sticker_hashes()

    def find_duplicate(self, sha1, dhash, skip="", file_path=None):
        candidate = sticker_duplicates.fingerprint(file_path) if file_path else {"sha1": sha1}
        for name, value in self.sticker_hashes().items():
            if name == skip:
                continue
            why = sticker_duplicates.same(candidate, value, file_path, os.path.join(self.sticker_dir(), "images", name))
            if why:
                return name, why
        return "", ""

    def conv_names(self):
        con = self.db_conn()
        if not con:
            return {}
        try:
            rows = con.execute("SELECT conv,name FROM convs").fetchall()
            return {r["conv"]: r["name"] for r in rows}
        except Exception:
            return {}
        finally:
            con.close()

    def query(self, sql, limit=None):
        con = self.db_conn()
        if not con:
            return []
        try:
            rows = con.execute(sql).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ("text", "card", "grudge"):
                    if k in d and d[k]:
                        d[k] = DEC(d[k])
                out.append(d)
            return out
        except Exception:
            return []
        finally:
            con.close()

    def storage_info(self):
        def size(p):
            try:
                return os.path.getsize(p)
            except Exception:
                return 0
        db = os.path.join(DATA_DIR, "bot.db")
        nap_logs = os.path.join(NAPCAT_DIR, "logs")
        nap_size = 0
        try:
            for f in os.listdir(nap_logs):
                nap_size += size(os.path.join(nap_logs, f))
        except Exception:
            pass
        # v0.55.8：NapCat 的标准输出被我们转存到 data/napcat.log（qq/logs 常常是空的），
        # 只算 qq/logs 会一直显示「协议端日志 0」。
        nap_size += size(os.path.join(DATA_DIR, "napcat.log"))
        return {
            "db": size(db), "bot_log": size(os.path.join(DATA_DIR, "bot.log")),
            "media": sum(size(os.path.join(DATA_DIR, "media", f)) for f in os.listdir(os.path.join(DATA_DIR, "media")))
            if os.path.isdir(os.path.join(DATA_DIR, "media")) else 0,
            "napcat_logs": nap_size,
            "limits": {"messages_days": getattr(Store, "MESSAGE_DAYS", 60),
                       "messages_max": getattr(Store, "MESSAGE_MAX", 50000),
                       "logs_days": getattr(Store, "LOG_DAYS", 14),
                       "logs_max": getattr(Store, "LOG_MAX", 20000),
                       "file_log_mb": 2, "file_log_keep": 3, "napcat_days": 7, "napcat_files": 10},
        }

    def recent_logs(self, limit=60):
        con = self.db_conn()
        if not con:
            return []
        try:
            # v0.55.8：「非白名单群跳过」一天几十上百条，逐条刷屏会把真正的日志淹掉，改成折叠汇总。
            noise = ("skip_group", "skip_group_noat", "skip_group_nonoone")
            rows = con.execute(
                "SELECT ts,tag,detail FROM logs WHERE tag NOT IN (?,?,?) ORDER BY id DESC LIMIT ?",
                noise + (limit,)).fetchall()
            out = ["%s [%s] %s" % (time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), r["tag"], r["detail"])
                   for r in reversed(rows)]
            try:
                n = con.execute("SELECT COUNT(*) FROM logs WHERE tag IN (?,?,?) AND ts>?",
                                noise + (int(time.time()) - 86400,)).fetchone()[0]
                if n:
                    out.append("（另外 24 小时内有 %d 条「非白名单群/没叫它」的跳过记录，已折叠不显示）" % n)
            except Exception:
                pass
            return out
        except Exception:
            return []
        finally:
            con.close()

    def _live_status(self):
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _processes(self):
        now = time.time()
        if self._proc_cache is not None and now - self._proc_cache_at < 5:
            return self._proc_cache
        procs = metrics.process_list([BOT_HOME], self._extra_pids())
        self._proc_cache = procs
        self._proc_cache_at = now
        return procs

    def sysmem(self):
        """系统真实内存（/proc/meminfo）。面板上的「进程内存」是 RSS 相加，会重复算共享页，
        单看它容易以为 2G 快满了；把 MemAvailable 一起显示，才看得到真实压力。"""
        try:
            info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    parts = v.split()
                    if parts:
                        info[k] = int(parts[0]) * 1024
            total = int(info.get("MemTotal") or 0)
            avail = int(info.get("MemAvailable") or 0)
            swap_total = int(info.get("SwapTotal") or 0)
            swap_free = int(info.get("SwapFree") or 0)
            return {"total": total, "available": avail, "used": max(0, total - avail),
                    "swap_total": swap_total, "swap_used": max(0, swap_total - swap_free)}
        except Exception:
            return {}

    def _share_state(self):
        """分享账本数据；store 的方法/表还没上线时静默给空，别把整个控制台拖成 500。"""
        def call(name, default, *args):
            try:
                fn = getattr(self.store, name, None)
                val = fn(*args) if fn else None
                return default if val is None else val
            except Exception:
                return default
        return (call("list_shares", [], 200),
                call("list_pending_shares", [], 30),
                call("share_stats", {}))

    def _qzone_state(self):
        """QQ空间只读账本：今日已发 X/Y + 最近 10 条；表/字段还没上线时静默给空，别拖垮控制台。

        今日已发只数真发出去的（草稿/演练/失败/跳过/被删都不算），和「每天条数」上限一起给前端。
        """
        out = {"today_sent": 0, "daily_max": int(self.cfg.get("qzone_daily_max") or 0),
               "posts": [], "comment_total": 0}
        con = self.db_conn()
        if not con:
            return out
        try:
            start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
            bad = ("draft", "drafted", "dry", "dry_run", "failed", "error",
                   "skipped", "rejected", "deleted", "blocked")

            def _fmt_ts(v):
                try:
                    return time.strftime("%m-%d %H:%M", time.localtime(int(v)))
                except Exception:
                    return str(v or "")

            today = {}
            try:
                for r in con.execute("SELECT status FROM qzone_posts WHERE created_at >= ?",
                                     (start,)).fetchall():
                    s = str(r["status"] or "").strip().lower()
                    today[s] = today.get(s, 0) + 1
                out["today_sent"] = sum(n for s, n in today.items() if s not in bad)
            except Exception:
                out["today_sent"] = 0
            cm = {}
            try:
                for r in con.execute("SELECT tid, COUNT(*) c FROM qzone_comments "
                                     "WHERE tid<>'' GROUP BY tid").fetchall():
                    cm[str(r["tid"])] = int(r["c"])
            except Exception:
                cm = {}
            posts = []
            try:
                rows = con.execute("SELECT created_at,status,text,tid,review FROM qzone_posts "
                                   "ORDER BY id DESC LIMIT 10").fetchall()
            except Exception:
                rows = []
            for r in rows:
                try:
                    text = DEC(r["text"]) or ""
                except Exception:
                    text = r["text"] or ""
                posts.append({"t": _fmt_ts(r["created_at"]),
                              "status": str(r["status"] or ""),
                              "text": str(text)[:40],
                              "comments": cm.get(str(r["tid"] or ""), 0),
                              "review": str(r["review"] or "")[:40]})
            out["posts"] = posts
            out["comment_total"] = sum(cm.values())
            return out
        except Exception:
            return out
        finally:
            con.close()

    def _proactive_state(self):
        """主动聊天的检查结果：大脑每分钟写一次 settings.proactive:plan，这里原样转给面板。

        只读；没有这个键（老版本/刚启动）就返回空，面板显示「还没检查」。
        """
        try:
            raw = self.store.get_setting("proactive:plan") or ""
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        items = data.get("items")
        return {"ts": int(data.get("ts") or 0),
                "items": items if isinstance(items, list) else []}

    def state(self):
        info = self.login_info()
        shares, share_pending, share_stats = self._share_state()
        procs = self._processes()
        total = sum(p["ws"] for p in procs)
        qq = sum(p["ws"] for p in procs if os.path.realpath(p["exe"]).lower().startswith(os.path.realpath(BOT_HOME).lower()))
        bot = total - qq
        with self.lock:
            hist = list(self.history)
        if not hist:
            hist = [{"t": int(time.time()), "mb": round(total / 1048576.0, 1)}]
        return {
            "version": VERSION,
            "components": {
                "napcat": self.napcat_running(),
                "brain": self.brain_running(),
                "login": bool(info),
            },
            "bot": {**self._live_status(),
                    "self_id": (info or {}).get("user_id"), "nickname": (info or {}).get("nickname"),
                    "uptime": int(time.time()) - self.started},
            "config": self.cfg.public_dict(),
            "models": [{**{k: v for k, v in m.items() if k != "api_key"},
                        "api_key_set": bool(m.get("api_key"))}
                       for m in (self.cfg.get("models") or [])],
            "today": self.today_counts(),
            "recent": self.recent_messages(200),
            "skips": self.recent_skips(100),
            "memories": self.memory_rows(),
            "recent_topics": self.recent_topic_rows(),
            "interaction_events": self.store.interaction_events(limit=50),
            "memory_usage": self.store.memory_usage_rows(50),
            "people": self.store.people_overview(
                _id_list(self.cfg.get("allow_users")), _id_list(self.cfg.get("allow_groups")),
                int(self.cfg.get("owner_qq") or 0)),
            "groups": self.store.groups_overview(_id_list(self.cfg.get("allow_groups")) or None),
            "diaries": self.query("SELECT conv,day,text,ts FROM diaries ORDER BY day DESC LIMIT 30"),
            "reminders": self.query("SELECT id,conv,text,at,done FROM reminders ORDER BY done ASC, at ASC LIMIT 30"),
            "convs": self.conv_names(),
            "usage": self.usage_stats(),
            "model_labels": self.model_labels(),
            "conversations": self.conversations(),
            "sticker_total": self.sticker_count(),
            "memory_stats": self.memory_stats(),
            "mood": self.current_mood(),
            "reply_autonomy": reply_autonomy.snapshot(self.store, self.cfg),
            "notebook": self.notebook_rows(),
            "decayed": self.decayed_memories(200),
            "sticker_index": dict(self.idx_state, files=self._sticker_index_files() or self.idx_state.get("files", 0)),
            "sticker_job": dict(self.st_job),
            "cards": self.query("SELECT conv,name,card,updated_at FROM person_cards ORDER BY updated_at DESC LIMIT 30"),
            "relations": self.query("SELECT conv,name,favor,grudge,updated_at FROM relations ORDER BY updated_at DESC LIMIT 30"),
            "shares": shares,
            "share_pending": share_pending,
            "share_stats": share_stats,
            "qzone": self._qzone_state(),
            "proactive": self._proactive_state(),
            "storage": self.storage_info(),
            "local": localllm.status(self.cfg),
            "logs": self.recent_logs(60),
            "memory": {"total": total, "qq": qq, "bot": bot, "procs": len(procs), "list": procs},
            "sysmem": self.sysmem(),
            "history": hist,
            "qr_time": int(os.path.getmtime(os.path.join(NAPCAT_DIR, "cache", "qrcode.png"))) if os.path.exists(os.path.join(NAPCAT_DIR, "cache", "qrcode.png")) else 0,
        }

    def action(self, cmd):
        if cmd == "napcat_start":
            return self.start_napcat()
        if cmd == "napcat_stop":
            return self.stop_napcat()
        if cmd == "napcat_restart":
            self.stop_napcat(); time.sleep(2); return self.start_napcat()
        if cmd == "brain_start":
            return self.start_brain()
        if cmd == "brain_stop":
            return self.stop_brain()
        if cmd == "brain_restart":
            self.stop_brain(); time.sleep(1); return self.start_brain()
        if cmd == "refresh_qr":
            return self.refresh_qr()
        if cmd == "quick_login":
            return self.quick_login()
        if cmd == "start_all":
            # 2026-10-25 修「假成功」：以前无条件 True。现在按进程真起来没有返回，
            # 协议端都没起来就不硬拉大脑（拉了也登录不上，只会在日志里刷重试）。
            nap = self.start_napcat()
            if not nap:
                self._log_console("start_all failed: 协议端没起来")
                return False
            time.sleep(6)  # 给 NapCat 一点时间把 OneBot 3000 端口拉起来
            brain = self.start_brain()
            ok = bool(nap and brain and self.napcat_running() and self.brain_running())
            if not ok:
                self._log_console("start_all failed: napcat=%s brain=%s" % (nap, brain))
            return ok
        if cmd == "stop_all":
            brain = self.stop_brain()
            nap = self.stop_napcat()
            time.sleep(1)
            ok = bool(brain and nap and (not self.brain_running()) and (not self.napcat_running()))
            if not ok:
                self._log_console("stop_all failed: brain_alive=%s napcat_alive=%s"
                                  % (self.brain_running(), self.napcat_running()))
            return ok
        if cmd == "new_session":
            return self.new_session()
        if cmd == "sticker_rebuild":
            return self.rebuild_sticker_index()
        if cmd == "sticker_dedup":
            return self.dedupe_stickers()
        if cmd == "sticker_autotag":
            return self.autotag_stickers()
        if cmd == "sticker_cards_rebuild":
            return self.rebuild_sticker_cards()
        if cmd == "local_start":
            return localllm.start(self.cfg)[0]
        if cmd == "local_stop":
            return localllm.stop(self.cfg)
        if cmd == "local_restart":
            localllm.stop(self.cfg)
            time.sleep(1)
            return localllm.start(self.cfg)[0]
        if cmd == "local_download":
            return localllm.download_all()
        if cmd == "pause_bot":
            self.cfg.set("bot_paused", True)
            self._log_console("pause_bot")
            return True
        if cmd == "resume_bot":
            self.cfg.set("bot_paused", False)
            self._log_console("resume_bot")
            return True
        if cmd == "send_test":
            owner = int(self.cfg.get("owner_qq") or 0)
            if not owner:
                return False
            return self._send_text(owner, "主人，我在。这是控制台发的测试消息。")
        return False

    def _st_job_state(self, **kw):
        self.st_job.update(kw)
        return dict(self.st_job)

    def dedupe_stickers(self):
        """Scan broadly, but recycle only pixel-verified duplicates."""
        with self._sticker_lock:
            if self.st_job.get("running"):
                return {"ok": False, "error": "已有表情包任务在跑"}
            self.st_job = {"running": True, "kind": "dedupe", "done": 0, "total": 0,
                           "msg": "扫描重复图片（含缩放、压缩、透明边框）", "ok": None, "removed": 0}

        def run():
            try:
                meta = os.path.join(self.sticker_dir(), "stickers.jsonl")
                with self._sticker_lock:
                    snapshot = open(meta, 'rb').read() if os.path.exists(meta) else b''
                    items = self.sticker_list()
                img_dir = os.path.join(self.sticker_dir(), "images")
                hashes = sticker_duplicates.load_hashes(self.sticker_dir(), [x.get('file') for x in items], getattr(self, '_sticker_hash', None))
                dup = sticker_duplicates.scan(items, hashes, img_dir,
                    lambda done,total: self._st_job_state(done=done,total=total,msg="核对画面 %d/%d（动图逐帧核对）" % (done,total)))
                with self._sticker_lock:
                    current = open(meta, 'rb').read() if os.path.exists(meta) else b''
                    if current != snapshot:
                        raise RuntimeError("扫描期间表情库有修改，已保留全部图片，请重新扫描")
                    rec = os.path.join(self.sticker_dir(), 'recycle')
                    os.makedirs(rec, exist_ok=True)
                    moved, failed, keep, seen = [], [], [], set()
                    try:
                        for item in items:
                            name = os.path.basename(str(item.get('file') or '').replace('\\', '/'))
                            if name in seen:
                                continue
                            seen.add(name)
                            if name in dup:
                                src, dst = os.path.join(img_dir, name), os.path.join(rec, name)
                                try:
                                    if os.path.exists(dst):
                                        raise OSError('回收站已有同名文件')
                                    # The fingerprint must still describe this exact file.
                                    st = os.stat(src)
                                    if hashes[name].get('stat') != [st.st_size, st.st_mtime_ns]:
                                        raise OSError('扫描后图片有修改')
                                    keeper = os.path.join(img_dir, dup[name]['keep'])
                                    ks = os.stat(keeper)
                                    if hashes[dup[name]['keep']].get('stat') != [ks.st_size, ks.st_mtime_ns]:
                                        raise OSError('保留的原图已变化')
                                    os.replace(src, dst); moved.append((src,dst))
                                    continue
                                except OSError:
                                    failed.append(name)
                            keep.append(item)
                        if moved or len(keep) != len(items):
                            _write_jsonl(meta, keep)
                    except Exception:
                        for src,dst in reversed(moved):
                            os.replace(dst, src)
                        raise
                    self.invalidate_stickers()
                    self._sticker_hash = {x.get('file'): hashes[x.get('file')] for x in keep if x.get('file') in hashes}
                removed = len(moved)
                self._st_job_state(running=False,ok=not failed,done=len(items),total=len(items),removed=removed,
                    msg="已将 %d 张确认重复移到回收站，剩 %d 张%s" % (removed,len(keep),"；%d 张移动失败，已保留"%len(failed) if failed else "；不确定的相似图保留"))
            except Exception as e:
                self._st_job_state(running=False,ok=False,msg="去重未完成：%s" % e)
        threading.Thread(target=run,name='sticker-dedupe',daemon=True).start()
        return {"ok": True, "cmd": "sticker_dedup"}

    def autotag_stickers(self, limit=0):
        """后台给「还没有标签」的表情包逐个打标签（会调看图模型，一张一次）。"""
        if self.st_job.get("running"):
            return {"ok": False, "error": "已有表情包任务在跑"}
        self.st_job = {"running": True, "kind": "autotag", "done": 0, "total": 0,
                       "msg": "准备中", "ok": None, "tagged": 0}

        def run():
            try:
                items = self.sticker_list()
                img_dir = os.path.join(self.sticker_dir(), "images")
                todo = [it for it in items if not (it.get("tags") or []) and not (it.get("note") or "")]
                if limit and limit > 0:
                    todo = todo[:limit]
                if not todo:
                    self._st_job_state(running=False, ok=True, done=0, total=0,
                                       msg="没有需要打标签的表情包")
                    return
                self._st_job_state(total=len(todo), msg="开始打标签")
                done = 0
                for it in todo:
                    name = os.path.basename(str(it.get("file") or "").replace("\\", "/"))
                    fp = os.path.join(img_dir, name)
                    tags, note = _auto_tag_image(self.cfg, fp, name)
                    if tags:
                        it["tags"] = tags
                        it["note"] = note or it.get("note") or ""
                        done += 1
                    self._st_job_state(done=done, msg="已打标签 %d / %d" % (done, len(todo)))
                _write_jsonl(os.path.join(self.sticker_dir(), "stickers.jsonl"), items)
                self.invalidate_stickers()
                self._st_job_state(running=False, ok=True, total=len(todo),
                                   msg="打标签完成：新增 %d 张有标签，正在补向量…" % done, tagged=done)
                try:
                    sticker_index.build_text_index(DATA_DIR, self.cfg)
                except Exception:
                    pass
                self._st_job_state(msg="打标签完成：%d 张已补标签并参与语义索引" % done)
            except Exception as e:
                self._st_job_state(running=False, ok=False, msg="打标签失败：%r" % (e,))

        threading.Thread(target=run, name="sticker-autotag", daemon=True).start()
        return {"ok": True, "cmd": "sticker_autotag"}

    def rebuild_sticker_cards(self, force=True):
        """v0.43.0：给每张表情包重建详细卡片（画面/细节/图上字/适合场合/检索词），
        建完顺手重建卡片索引。会调看图模型，一张一次；force=False 只补缺的。"""
        if self.st_job.get("running"):
            return {"ok": False, "error": "已有表情包任务在跑"}
        self.st_job = {"running": True, "kind": "cards", "done": 0, "total": 0,
                       "msg": "准备中", "ok": None, "removed": 0, "tagged": 0}

        def run():
            try:
                items = self.sticker_list()
                files = [os.path.basename(str(it.get("file") or "").replace("\\", "/")) for it in items]
                files = [f for f in files if f]
                img_dir = os.path.join(self.sticker_dir(), "images")
                pool = ModelPool(self.cfg)
                total = len(files)

                def prog(d, t, msg):
                    self._st_job_state(done=d, total=t,
                                       msg="%s %d/%d" % (msg or "建卡中", d, t))

                self._st_job_state(total=total, msg="开始建卡（会调看图模型）")
                cards, done, fail = sticker_cards.build(
                    DATA_DIR, pool, img_dir, files, workers=3, force=force, progress=prog)
                self._st_job_state(done=done, total=total,
                                   msg="建卡完成：%d 张，失败 %d；正在重建卡片索引…" % (done, fail))
                try:
                    res = sticker_index.build_cards_index(DATA_DIR, self.cfg)
                except Exception as e:
                    self._st_job_state(running=False, ok=False,
                                       msg="卡片建好了，但索引重建失败：%r" % (e,))
                    return
                if not res.get("ok"):
                    self._st_job_state(running=False, ok=False,
                                       msg="卡片建好了，但索引失败：%s" % (res.get("error") or ""))
                    return
                self._sticker_mtime = 0
                self._st_job_state(running=False, ok=True,
                                   msg="完成：%d 张卡片（失败 %d），索引 %d 张" % (
                                       len(cards), fail, res.get("indexed") or 0))
            except Exception as e:
                self._st_job_state(running=False, ok=False, msg="建卡失败：%r" % (e,))

        threading.Thread(target=run, name="sticker-cards", daemon=True).start()
        return {"ok": True, "cmd": "sticker_cards_rebuild"}

    def rebuild_sticker_index(self):
        """后台重建表情包语义索引；进度写进 idx_state，面板轮询显示。"""
        if self.idx_state.get("running"):
            return True
        self.idx_state = {"running": True, "done": 0, "total": 0, "msg": "启动中", "ok": None,
                          "files": self._sticker_index_files()}

        def progress(done, total, msg):
            self.idx_state.update({"done": int(done), "total": int(total), "msg": msg})

        def run():
            try:
                res = sticker_index.build_text_index(DATA_DIR, self.cfg, progress=progress)
                ok = bool(res.get("ok"))
                self.idx_state.update({
                    "running": False, "ok": ok,
                    "msg": ("完成：%d 张" % res.get("indexed", 0)) if ok else ("失败：" + str(res.get("error"))),
                    "files": self._sticker_index_files(),
                })
            except Exception as e:
                self.idx_state.update({"running": False, "ok": False, "msg": "失败：%r" % (e,),
                                       "files": self._sticker_index_files()})
        threading.Thread(target=run, daemon=True).start()
        return True

    def _sticker_index_files(self):
        try:
            meta = json.load(open(os.path.join(DATA_DIR, "stickers", "sticker_vectors.json"), encoding="utf-8"))
            return len(meta.get("files") or [])
        except Exception:
            return 0

    def new_session(self):
        """所有会话开新会话：清摘要，并把历史挡在上下文外（聊天记录仍可查）。"""
        try:
            con = sqlite3.connect(os.path.join(DATA_DIR, "bot.db"), timeout=5)
            rows = con.execute("SELECT conv, MAX(id) m FROM messages GROUP BY conv").fetchall()
            con.close()
            for conv, maxid in rows:
                if not conv or not maxid:
                    continue
                self.store.set_setting("ctx_start:" + conv, str(int(maxid)))
                self.store.reset_session(conv)
                self.store.set_setting("summary_upto:" + conv, str(int(maxid)))
            return True
        except Exception:
            return False

    def _send_text(self, uid, text):
        try:
            import urllib.request
            body = json.dumps({"user_id": uid, "message": [{"type": "text", "data": {"text": text}}]}).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/send_private_msg" % self.onebot_port, data=body,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + str(self.cfg.get("onebot_token"))},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except Exception:
            return False


def main():
    mutex = acquire_single_instance(CONSOLE_MUTEX)
    if not mutex:
        print("console already running")
        return
    sup = Supervisor()

    class ConsoleServer(ThreadingHTTPServer):
        # 重启时旧的监听还处在 TIME_WAIT，不开复用会 bind 失败（表现为控制台一起就退）
        allow_reuse_address = True
        allow_reuse_port = True
        daemon_threads = True

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            extra = extra or {}
            if "Cache-Control" not in extra:
                self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        def _token_from_request(self):
            q = parse_qs(urlsplit(self.path).query)
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

        def _ok(self):
            tok = str(sup.cfg.get("admin_token") or "")
            return bool(tok) and hmac.compare_digest(self._token_from_request(), tok)

        def _origin_ok(self):
            """防跨站：Origin 必须和本机访问地址一致。

            服务器版面板是走 SSH 隧道访问的（本机端口不是 8090），
            所以除了写死的 127.0.0.1:8090，还要认请求自己的 Host 头。
            """
            if not self._host_ok():
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = (self.headers.get("Host") or "").strip()
            allowed = {"http://" + host, "https://" + host} if host else set()
            allowed.add("http://127.0.0.1:%d" % sup.port)
            allowed.add("http://localhost:%d" % sup.port)
            return origin in allowed

        def _host_ok(self):
            # SSH forwarding changes the port, never the trusted loopback host.
            host = str(self.headers.get("Host") or "").strip()
            if not host:  # Legacy local HTTP/1.0/CLI clients, still require auth.
                return True
            try:
                value = urlsplit("http://" + host)
                return (value.hostname in ("127.0.0.1", "localhost") and value.username is None
                        and value.password is None and not value.path and not value.query
                        and not value.fragment and (value.port is None or 1 <= value.port <= 65535))
            except ValueError:
                return False

        def do_GET(self):
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "host"})
                return
            path = urlsplit(self.path).path
            q = parse_qs(urlsplit(self.path).query)
            if path == "/health":
                self._json(200, {"ok": True})
                return
            if path == "/favicon.ico":
                # 浏览器标签页图标：大肥鱼 QQ 头像（不是口令页面，放在鉴权前）
                try:
                    with open(ICON, "rb") as f:
                        data = f.read()
                    self._send(200, data, "image/x-icon", {"Cache-Control": "public, max-age=86400"})
                except Exception:
                    self._send(404, "not found")
                return
            token = str(sup.cfg.get("admin_token") or "")
            if path in ("/", "/index.html") and q.get("token") and token and hmac.compare_digest(q["token"][0], token):
                self.send_response(302)
                self.send_header("Set-Cookie", "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % token)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if not self._ok():
                self._send(403, "<meta charset='utf-8'><body style='background:#0f1520;color:#e7eefc;font-family:system-ui;padding:40px'><h3>需要口令</h3><p>请从大肥鱼手机版的“完整管理页”入口重新打开。</p></body>")
                return
            if path in ("/ops", "/ops.html"):
                # v0.53.0 运维中心：体检 / 告警 / 机器人清单 / 人设编辑（独立页，不塞进主面板）
                try:
                    with open(OPS_PAGE, "r", encoding="utf-8") as f:
                        ops_page = f.read()
                except Exception as e:
                    ops_page = "<meta charset='utf-8'>ops.html 读取失败：%s" % e
                self._send(200, ops_page, "text/html; charset=utf-8",
                           {"Set-Cookie": "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % str(sup.cfg.get("admin_token"))})
                return
            if path == "/api/ops/state":
                try:
                    self._json(200, sup.ops.state())
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/persona":
                try:
                    self._json(200, sup.ops.persona_list((q.get("bot") or [""])[0]))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/persona_get":
                try:
                    self._json(200, sup.ops.persona_get((q.get("bot") or [""])[0], (q.get("name") or [""])[0]))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/usage":
                # v0.65.2 用量看板：range=today|7d|30d|month|all|custom（custom 另带 from/to 秒级时间戳）
                try:
                    rng = (q.get("range") or ["today"])[0]
                    frm = int((q.get("from") or ["0"])[0] or 0)
                    to = int((q.get("to") or ["0"])[0] or 0)
                    self._json(200, sup.usage_dashboard(rng, frm, to))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:300]})
                return
            if path.startswith("/api/sticker_thumb/"):
                fp = sup.sticker_thumb(unquote(path[len("/api/sticker_thumb/"):]), False)
                if not fp:
                    self._send(404, "not found"); return
                with open(fp, "rb") as f:
                    data = f.read()
                self._send(200, data, "image/jpeg", {"Cache-Control": "public, max-age=86400"})
                return
            if path.startswith("/api/sticker_recycle_thumb/"):
                fp = sup.sticker_thumb(unquote(path[len("/api/sticker_recycle_thumb/"):]), True)
                if not fp:
                    self._send(404, "not found"); return
                with open(fp, "rb") as f:
                    data = f.read()
                self._send(200, data, "image/jpeg", {"Cache-Control": "public, max-age=86400"})
                return
            if path == "/video":
                try:
                    with open(VIDEO_PAGE, "r", encoding="utf-8") as f:
                        page = f.read()
                except Exception as e:
                    page = "<meta charset='utf-8'>video.html 读取失败：%s" % e
                self._send(200, page, "text/html; charset=utf-8",
                           {"Set-Cookie": "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % str(sup.cfg.get("admin_token"))})
                return
            if path == "/api/video_browse":
                try:
                    self._json(200, video_browse_state(sup.cfg, sup.store))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/kb":
                # v0.61.0 知识库：按分类看卡片、搜、改分类/质量、删
                try:
                    with open(KB_PAGE, "r", encoding="utf-8") as f:
                        page = f.read()
                except Exception as e:
                    page = "<meta charset='utf-8'>kb.html 读取失败：%s" % e
                self._send(200, page, "text/html; charset=utf-8",
                           {"Set-Cookie": "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % str(sup.cfg.get("admin_token"))})
                return
            if path == "/api/kb":
                try:
                    act = (q.get("action") or ["list"])[0]
                    self._json(200, kb_state(sup, act, q))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/blacklist":
                self._json(200, {"ok": True, "users": sup.store.blacklist_users()})
                return
            if path == "/api/dreams":
                try:
                    self._json(200, sup.store.dream_state())
                except Exception:
                    self._json(500, {"ok": False, "error": "梦境暂不可用"})
                return
            if path == '/api/self-state':
                try:
                    snapshot=sup.store.self_snapshot(include_versions=True)
                    owner=int(sup.cfg.get('owner_qq') or 0)
                    self._json(200,dict(snapshot,enabled=bool(sup.cfg.get('self_growth_enabled',True)),
                        family=dict(role='父亲',confirmed=bool(owner),profile_available=bool(owner and sup.store.get_profile(owner)))))
                except Exception:
                    self._json(500,{'ok':False,'error':'当前自我暂不可用'})
                return
            if path == "/api/self-growth":
                conv = str((q.get("conv") or [""])[0])
                if not re.fullmatch(r"[pg]:[1-9]\d*", conv):
                    self._json(400, {"ok": False, "error": "请选择有效会话"})
                    return
                try:
                    snapshot = sup.store.growth_snapshot(conv)
                    self._json(200, dict(snapshot, enabled=bool(sup.cfg.get("self_growth_enabled", True)), conv=conv))
                except Exception:
                    self._json(500, {"ok": False, "error": "成长记录暂不可用"})
                return
            if path == "/api/state":
                self._json(200, sup.state())
                return
            if path == "/api/bot_rounds":
                try:
                    self._json(200, bot_rounds_state(sup.cfg))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/cover":
                try:
                    self._json(200, cover_state(sup.cfg))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/voice":
                try:
                    self._json(200, voice_state(sup.cfg))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/gate_probe":
                # v0.55.1 面板「为什么没回」：把参数转给大脑的只读试算口（不调模型、不发消息、不写库）
                try:
                    import urllib.parse as _up
                    import urllib.request as _ur
                    port = int(sup.cfg.get("webhook_port") or 18080)
                    tok = str(sup.cfg.get("onebot_token") or "")
                    qs = _up.urlencode({
                        "is_group": (q.get("is_group") or ["1"])[0],
                        "gid": (q.get("gid") or ["0"])[0],
                        "uid": (q.get("uid") or ["0"])[0],
                        "name": (q.get("name") or [""])[0],
                        "text": (q.get("text") or [""])[0],
                    })
                    req = _ur.Request("http://127.0.0.1:%d/probe_gate?%s" % (port, qs),
                                      headers={"Authorization": "Bearer " + tok})
                    with _ur.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    self._json(200, data)
                except Exception as e:
                    self._json(500, {"ok": False, "error": repr(e)[:200]})
                return
            if path == "/api/person":
                try:
                    qq = int(((q.get("qq") or ["0"])[0]) or 0)
                    _ags = _id_list(sup.cfg.get("allow_groups")) or None
                    self._json(200, sup.store.person_detail(qq, _ags))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/group":
                try:
                    gid = int(((q.get("gid") or ["0"])[0]) or 0)
                    _ags = _id_list(sup.cfg.get("allow_groups"))
                    if _ags and gid not in _ags:
                        self._json(404, {"ok": False, "error": "group not allowed"})
                        return
                    self._json(200, sup.store.group_detail(gid))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path.startswith("/api/sticker_recycle_img/"):
                name = os.path.basename(unquote(path[len("/api/sticker_recycle_img/"):]))
                fp = os.path.join(sup.sticker_dir(), "recycle", name)
                if not os.path.isfile(fp):
                    self._send(404, "not found"); return
                data = open(fp, "rb").read()
                ctype = "image/png" if name.endswith(".png") else ("image/gif" if name.endswith(".gif") else ("image/webp" if name.endswith(".webp") else "image/jpeg"))
                self._send(200, data, ctype); return
            if path.startswith("/api/sticker_img/"):
                name = os.path.basename(unquote(path[len("/api/sticker_img/"):]))
                fp = os.path.join(sup.sticker_dir(), "images", name)
                if not os.path.isfile(fp):
                    self._send(404, "not found"); return
                data = open(fp, "rb").read()
                ctype = "image/png" if name.endswith(".png") else ("image/gif" if name.endswith(".gif") else ("image/webp" if name.endswith(".webp") else "image/jpeg"))
                self._send(200, data, ctype); return
            if path == "/api/stickers":
                rec_dir = os.path.join(sup.sticker_dir(), "recycle")
                recycle = sorted(os.listdir(rec_dir)) if os.path.isdir(rec_dir) else []
                self._json(200, {"total": sup.sticker_count(), "items": sup.sticker_list(), "recycle": recycle})
                return
            if path.startswith("/api/media/"):
                import re as _re
                name = path[len("/api/media/"):]
                if not _re.match(r"^[0-9a-f]{20}\.(jpg|png|gif|webp)$", name):
                    self._send(404, "bad name")
                    return
                fp = os.path.join(DATA_DIR, "media", name)
                if not os.path.exists(fp):
                    self._send(404, "not found")
                    return
                with open(fp, "rb") as f:
                    data = f.read()
                ctype = "image/gif" if name.endswith(".gif") else ("image/webp" if name.endswith(".webp") else ("image/png" if name.endswith(".png") else "image/jpeg"))
                self._send(200, data, ctype)
                return
            if path == "/api/qr":
                qr = os.path.join(NAPCAT_DIR, "cache", "qrcode.png")
                if not os.path.exists(qr):
                    self._send(404, "no qr")
                    return
                with open(qr, "rb") as f:
                    data = f.read()
                self._send(200, data, "image/png")
                return
            if path == "/api/sticker":
                try:
                    d = os.path.join(DATA_DIR, "stickers", "images")
                    names = [n for n in os.listdir(d) if not n.startswith(".")]
                    name = names[int(time.time() * 7) % len(names)]
                    with open(os.path.join(d, name), "rb") as f:
                        data = f.read()
                    ctype = "image/gif" if name.endswith(".gif") else ("image/webp" if name.endswith(".webp") else "image/png")
                    self._send(200, data, ctype)
                except Exception:
                    self._send(404, "not found")
                return
            try:
                with open(PAGE, "r", encoding="utf-8") as f:
                    page = f.read()
            except Exception as e:
                page = "<meta charset='utf-8'>dashboard.html 读取失败：%s" % e
            self._send(200, page, "text/html; charset=utf-8",
                       {"Set-Cookie": "dafeiyu_admin=%s; Path=/; HttpOnly; SameSite=Strict" % str(sup.cfg.get("admin_token"))})

        def do_POST(self):
            if urlsplit(self.path).path.startswith("/api/sticker_"):
                with sup._sticker_lock:
                    try:
                        return self._handle_post()
                    finally:
                        sup._sticker_upload_names = set()
            return self._handle_post()

        def _handle_post(self):
            path = urlsplit(self.path).path
            if not self._origin_ok():
                self._json(403, {"ok": False, "error": "origin"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if self.headers.get("Transfer-Encoding") or length < 0 or length > 16 * 1024 * 1024:
                self.close_connection = True
                self._json(413 if length > 16 * 1024 * 1024 else 400,
                           {"ok": False, "error": "invalid request size"})
                return
            if getattr(self, 'connection', None) is not None:
                self.connection.settimeout(10)
            if path == "/api/sticker_upload":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    ctype = self.headers.get("Content-Type") or ""
                    if not self._ok_form({}):
                        self._json(403, {"ok": False})
                        return
                    if n > 16 * 1024 * 1024:
                        self._json(413, {"ok": False, "error": "too large"})
                        return
                    body = self.rfile.read(n) if n else b""
                    import re as _re
                    m = _re.search(r"boundary=([^;]+)", ctype)
                    if not m:
                        self._json(403, {"ok": False})
                        return
                    boundary = ("--" + m.group(1).strip('"')).encode()
                    autotag = True
                    files = []
                    for part in body.split(boundary):
                        head, _, data = part.partition(b"\r\n\r\n")
                        if b'name="autotag"' in head:
                            val = data.strip().strip(b"-").strip().decode("utf-8", "replace").lower()
                            autotag = val not in ("0", "false", "no", "off")
                            continue
                        if b"filename=" not in head:
                            continue
                        fn = _re.search(rb'filename="([^"]+)"', head)
                        if not fn:
                            continue
                        fname = os.path.basename(fn.group(1).decode("utf-8", "replace"))
                        files.append((fname, data.rstrip(b"\r\n-")))
                    if not files:
                        self._json(400, {"ok": False, "error": "no file"})
                        return
                    st_dir = sup.sticker_dir()
                    img_dir = os.path.join(st_dir, "images")
                    os.makedirs(img_dir, exist_ok=True)
                    items = sup.sticker_list()
                    added, dup, tagged, tag_failed = [], [], [], []
                    for fname, data in files:
                        tmpf = os.path.join(img_dir, ".up-tmp-" + str(int(time.time() * 1000)))
                        with open(tmpf, "wb") as f:
                            f.write(data)
                        sha1 = _sha1_of(tmpf)
                        dhash = _dhash_of(tmpf)
                        same, why = sup.find_duplicate(sha1, dhash, file_path=tmpf)
                        if same:
                            try:
                                os.remove(tmpf)
                            except Exception:
                                pass
                            dup.append({"file": fname, "same": same, "why": why})
                            continue
                        safe = "up-" + str(int(time.time())) + "-" + str(len(items)) + "-" + fname
                        final = os.path.join(img_dir, safe)
                        os.replace(tmpf, final)
                        tags, note = [], ""
                        if autotag:
                            t2, n2 = _auto_tag_image(sup.cfg, final, safe)
                            if t2:
                                tags, note = t2, n2
                                tagged.append(safe)
                            else:
                                tag_failed.append(safe)
                        items.append({"file": safe, "tags": tags, "note": note,
                                      "scene": "", "status": "ok", "uses": 0})
                        sup.remember_hash(safe, final)
                        added.append(safe)
                    if added:
                        _write_jsonl(os.path.join(st_dir, "stickers.jsonl"), items)
                        sup.invalidate_stickers()
                    self._json(200, {"ok": True, "added": added, "duplicates": dup,
                                     "tagged": tagged, "tag_failed": tag_failed, "autotag": autotag})
                    if added and autotag and tagged:
                        try:
                            sup.rebuild_sticker_index()
                        except Exception:
                            pass
                    return
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1024 * 1024:
                    self._json(413, {"ok": False, "error": "too large"})
                    return
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            except Exception:
                raw = ""
            # v0.67.0 语音接入：/api/voice 用 JSON 提交，其余接口仍是表单，两种都认
            ctype = (self.headers.get("Content-Type") or "").lower()
            form = {}
            if "application/json" in ctype:
                try:
                    obj = json.loads(raw or "{}")
                    if isinstance(obj, dict):
                        form = obj
                except Exception:
                    form = {}
            if not form:
                form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            if not self._ok_form(form):
                self._json(403, {"ok": False, "error": "token"})
                return
            if path == "/api/ops/selftest":
                try:
                    self._json(200, sup.ops.selftest())
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/persona_save":
                try:
                    self._json(200, sup.ops.persona_save(form.get("bot") or "", form.get("name") or "",
                                                         form.get("content") or ""))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/persona_restore":
                try:
                    self._json(200, sup.ops.persona_restore(form.get("bot") or "", form.get("name") or "",
                                                            form.get("file") or ""))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/bot_add":
                try:
                    self._json(200, sup.ops.bot_add(form.get("name") or "", form.get("channel") or "",
                                                    form.get("account") or "", form.get("note") or ""))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/bot_remove":
                try:
                    self._json(200, sup.ops.bot_remove(form.get("id") or ""))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/ops/mail_test":
                try:
                    self._json(200, sup.ops.mail_test())
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/blacklist":
                try:
                    value = form.get("users", "")
                    users = value if isinstance(value, list) else [v for v in re.split(r"[,，\s]+", str(value).strip()) if v]
                    self._json(200, {"ok": True, "users": sup.store.set_blacklist(users)})
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                except Exception:
                    self._json(500, {"ok": False, "error": "黑名单保存失败，请重新读取后再试"})
                return
            if path == "/api/save":
                try:
                    apply_settings(sup.cfg, form)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/video_browse":
                try:
                    act = str(form.get("action") or "").strip()
                    if act:
                        req = {"action": act, "ts": int(time.time())}
                        if act == "start":
                            req["minutes"] = max(1, min(180, int(form.get("minutes") or 10)))
                        if act == "growth_rollback" and form.get("version"):
                            req["version"] = int(form.get("version"))
                        sup.store.set_setting("vbrowse:manual_request", json.dumps(req, ensure_ascii=False))
                        resp = {"ok": True, "queued": act}
                        if act in ("start", "stop"):
                            try:
                                cur = json.loads(sup.store.get_setting("vbrowse:status") or "{}")
                            except Exception:
                                cur = {}
                            running = bool(cur.get("running"))
                            resp["running"] = running
                            if act == "stop":
                                resp["message"] = ("已在停：当前这条视频收尾后就停（最多约 1 分钟）"
                                                   if running else "现在没在刷，不用停")
                            else:
                                resp["message"] = ("已经排上了，5 秒内开始" if not running
                                                   else "在刷着呢，已给你延到还剩 %d 分钟"
                                                   % max(0, int((int(cur.get("deadline") or 0) - time.time()) // 60)))
                        self._json(200, resp)
                        return
                    save_video_browse(sup.cfg, form)
                    self._json(200, video_browse_state(sup.cfg, sup.store))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/bot_rounds":
                try:
                    self._json(200, bot_rounds_save(sup.cfg, form))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/cover":
                try:
                    self._json(200, cover_save(sup.cfg, form))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/voice":
                try:
                    self._json(200, voice_save(sup.cfg, form))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/voice/test":
                try:
                    ok, ctype2, data, err = voice_test(sup.cfg, form)
                    if ok:
                        if "wav" not in (ctype2 or "").lower():
                            data = _voice_wav_wrap(data)
                        self._send(200, data, "audio/wav")
                    else:
                        self._json(502, {"ok": False, "error": err})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/kb":
                try:
                    self._json(200, kb_action(sup, form))
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/local_set":
                try:
                    if "thinking" in form:
                        sup.cfg.set("local_llm_thinking", form.get("thinking") in ("1", "on", "true", "True"))
                    if "budget" in form:
                        try:
                            sup.cfg.set("local_llm_thinking_budget", max(0, min(32768, int(form.get("budget") or 0))))
                        except Exception:
                            pass

                    def _reload():
                        try:
                            if localllm.status(sup.cfg)["running"]:
                                localllm.stop(sup.cfg)
                                time.sleep(1)
                                localllm.start(sup.cfg)
                        except Exception as e:
                            print("local reload failed", e)
                    threading.Thread(target=_reload, daemon=True).start()
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/models":
                try:
                    models = json.loads(form.get("models") or "[]")
                    old = {str(x.get("id") or ""): x for x in (sup.cfg.get("models") or [])}
                    clean = []
                    for m in models:
                        if not isinstance(m, dict):
                            continue
                        if not (m.get("base_url") and m.get("model")):
                            continue
                        mid = str(m.get("id") or ("m" + str(len(clean) + 1)))[:40]
                        key = str(m.get("api_key") or "").strip()
                        if key in ("", "***"):
                            key = str((old.get(mid) or {}).get("api_key") or "")
                        clean.append({
                            "id": mid,
                            "name": str(m.get("name") or m.get("model"))[:60],
                            "base_url": str(m.get("base_url")).strip(),
                            "model": str(m.get("model")).strip(),
                            "api_key": key,
                            "enabled": bool(m.get("enabled", True)),
                        })
                    sup.cfg.set("models", clean)
                    sup.cfg.set("vision_model_id", str(form.get("vision_model_id") or ""))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/model_test":
                try:
                    mid = str(form.get("id") or "")
                    conf = None
                    for m in (sup.cfg.get("models") or []):
                        if str(m.get("id") or "") == mid:
                            conf = m
                            break
                    if not conf:
                        self._json(404, {"ok": False, "error": "模型不存在，先保存再测"})
                        return
                    key = str(conf.get("api_key") or "") or str(sup.cfg.get("api_key") or "")
                    t0 = time.time()
                    r = Ark(conf, key, False, 0).chat(
                        [{"role": "user", "content": "只回两个字：在的"}],
                        max_tokens=256, temperature=0.2, timeout=30)
                    ms = int((time.time() - t0) * 1000)
                    ok = bool(r.ok and r.text.strip())
                    self._json(200, {
                        "ok": ok, "ms": ms, "model": conf.get("model") or "",
                        "text": (r.text or "")[:80],
                        "error": ("" if ok else (r.error or "空回复"))[:160],
                    })
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/shared_save":
                try:
                    text = str(form.get("text") or "").strip()
                    people = []
                    for part in str(form.get("people") or "").replace("，", ",").split(","):
                        part = part.strip()
                        if part.isdigit() and int(part) > 0 and int(part) not in people:
                            people.append(int(part))
                    gid = int(str(form.get("gid") or "0").strip() or 0)
                    res = sup.store.add_shared_memory(text, people, gid, "event", 3, "")
                    if not res:
                        self._json(400, {"ok": False, "error": "需要 2~5 个人和至少 6 个字"})
                        return
                    if res.get("id"):
                        vec = _embed_one(sup.cfg, text)
                        if vec:
                            sup.store.set_memory_vec("shared_memories", res["id"], vec,
                                                     str(sup.cfg.get("sticker_embed_model") or ""))
                    self._json(200, {"ok": True, "id": res.get("id"), "action": res.get("action")})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/shared_delete":
                try:
                    sup.store.delete_shared_memory(int(str(form.get("id") or "0").strip() or 0))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/person_save":
                try:
                    qq = int(str(form.get("qq") or "0").strip() or 0)
                    if qq <= 0:
                        self._json(400, {"ok": False, "error": "qq"})
                        return
                    old = sup.store.get_profile(qq) or {}
                    name = str(form.get("name") or old.get("name") or "")[:40]
                    card = form.get("card")
                    recent = form.get("recent")
                    if card is not None or recent is not None:
                        sup.store.upsert_card(
                            "q:%d" % qq, name,
                            card if card is not None else (old.get("card") or ""),
                            recent=recent if recent is not None else None,
                            evidence=old.get("evidence"))
                    if str(form.get("favor") or "").strip() not in ("", "None"):
                        sup.store.set_favor(qq, int(float(str(form.get("favor")))), "面板手动调整")
                    if str(form.get("trust") or "").strip() not in ("", "None"):
                        sup.store.set_trust(qq, int(float(str(form.get("trust")))), "面板手动调整")
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/grudge_clear":
                try:
                    gid = int(str(form.get("id") or "0").strip() or 0)
                    sup.store.clear_grudge(gid)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/memory_protect":
                try:
                    mid = str(form.get("id") or "")
                    table = {"p":"person_memories", "g":"group_memories", "s":"shared_memories"}.get(mid[:1])
                    if not table or not mid[1:].isdigit():
                        raise ValueError("invalid memory id")
                    ok = sup.store.set_memory_protected(table, int(mid[1:]), str(form.get("protected"))=="1")
                    self._json(200, {"ok":ok})
                except Exception as e:
                    self._json(400, {"ok":False,"error":str(e)[:120]})
                return
            if path == "/api/memory_save":
                try:
                    mid = str(form.get("id") or "")
                    text = str(form.get("text") or "")[:500]
                    table, row_id = "", 0
                    if mid.startswith("p"):
                        table, row_id = "person_memories", int(mid[1:] or 0)
                        sup.store.update_person_memory(row_id, text)
                    elif mid.startswith("g"):
                        table, row_id = "group_memories", int(mid[1:] or 0)
                        sup.store.update_group_memory(row_id, text)
                    else:
                        sup.store.update_memory(int(mid or 0), text)
                    if table and row_id and text:
                        vec = _embed_one(sup.cfg, text)
                        if vec:
                            sup.store.set_memory_vec(table, row_id, vec,
                                                     str(sup.cfg.get("sticker_embed_model") or ""))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/memory_restore":
                # v0.63.0：把一条已经淡忘的记忆捞回来（重新可被想起）
                try:
                    mid = str(form.get("id") or "")
                    table = str(form.get("table") or "").strip()
                    if table not in ("person_memories", "group_memories", "shared_memories"):
                        table = "group_memories" if mid.startswith("g") else "person_memories"
                    row_id = int(re.sub(r"[^0-9]", "", mid) or 0)
                    ok2 = bool(row_id) and sup.store.restore_memory(table, row_id)
                    if ok2:
                        try:
                            row = sup.store.conn.execute(
                                "SELECT text FROM %s WHERE id=?" % table, (row_id,)).fetchone()
                            txt2 = DEC(row["text"]) or "" if row else ""
                            vec = _embed_one(sup.cfg, txt2) if txt2 else None
                            if vec:
                                sup.store.set_memory_vec(table, row_id, vec,
                                                         str(sup.cfg.get("sticker_embed_model") or ""))
                        except Exception:
                            pass
                    self._json(200, {"ok": ok2})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/memory_archive":
                # v0.69.0：面板「底片」列表（淡出的原文存档，只读）
                try:
                    limit = max(1, min(500, int(form.get("limit") or 100)))
                    q = str(form.get("query") or "")[:40]
                    self._json(200, {"ok": True, "items": sup.store.list_memory_archive(limit, q)})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/memory_archive_dig":
                # v0.69.0：面板点开某条底片看原文
                try:
                    rec = sup.store.dig_memory(str(form.get("what") or "")[:80])
                    self._json(200, {"ok": bool(rec), "item": rec or {}})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/notebook_delete":
                try:
                    ok = bool(sup.store.delete_notebook(int(form.get("id") or 0)))
                except Exception:
                    ok = False
                self._json(200, {"ok": ok})
                return
            if path == "/api/memory_delete":
                try:
                    mid = str(form.get("id") or "")
                    if mid.startswith("p"):
                        sup.store.delete_person_memory(int(mid[1:] or 0))
                    elif mid.startswith("g"):
                        sup.store.delete_group_memory(int(mid[1:] or 0))
                    else:
                        sup.store.delete_memory(int(mid or 0))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/share_update":
                try:
                    sid = int(str(form.get("id") or "0").strip() or 0)
                    if sid <= 0:
                        self._json(400, {"ok": False, "error": "id"})
                        return
                    if str(form.get("action") or "status") == "delete":
                        sup.store.delete_share(sid)
                    else:
                        status = str(form.get("status") or "").strip()
                        if status not in ("pending", "replied", "seen_ignored", "unseen", "dumped"):
                            self._json(400, {"ok": False, "error": "status"})
                            return
                        sup.store.set_share_status(sid, status)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path == "/api/share_pending_drop":
                try:
                    pid = int(str(form.get("id") or "0").strip() or 0)
                    if pid <= 0:
                        self._json(400, {"ok": False, "error": "id"})
                        return
                    sup.store.drop_pending_share(pid)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)[:200]})
                return
            if path in ("/api/dreams/settings", "/api/dreams/delete"):
                try:
                    result = (sup.store.save_dream_settings(form) if path.endswith("/settings")
                              else sup.store.delete_dream(int(form.get("id") or 0)))
                    self._json(200, result)
                except (ValueError, TypeError) as e:
                    self._json(400, {"ok": False, "error": str(e)[:100]})
                except Exception:
                    self._json(500, {"ok": False, "error": "梦境操作失败"})
                return
            if path == "/api/diary_save":
                try:
                    sup.store.update_diary(form.get("conv") or "", form.get("day") or "",
                                           str(form.get("text") or "")[:5000])
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/diary_delete":
                try:
                    sup.store.delete_diary(form.get("conv") or "", form.get("day") or "")
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/sticker_recycle_clear":
                try:
                    rec = os.path.join(sup.sticker_dir(), "recycle")
                    n = 0
                    if os.path.isdir(rec):
                        for f in os.listdir(rec):
                            fp = os.path.join(rec, f)
                            try:
                                if os.path.isfile(fp):
                                    os.remove(fp)
                                    n += 1
                            except Exception:
                                pass
                    self._json(200, {"ok": True, "removed": n})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/sticker_restore":
                try:
                    f = os.path.basename(form.get("file") or "")
                    rec = os.path.join(sup.sticker_dir(), "recycle", f)
                    img = os.path.join(sup.sticker_dir(), "images", f)
                    if not os.path.isfile(rec):
                        self._json(404, {"ok": False}); return
                    os.replace(rec, img)
                    items = sup.sticker_list()
                    if not any(x.get("file") == f for x in items):
                        items.append({"file": f, "tags": [], "note": "", "scene": "", "status": "ok", "uses": 0})
                        _write_jsonl(os.path.join(sup.sticker_dir(), "stickers.jsonl"), items)
                    sup.invalidate_stickers()
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/sticker_save":
                try:
                    items = sup.sticker_list()
                    target = form.get("file") or ""
                    for it in items:
                        if it.get("file") == target:
                            it["tags"] = [t.strip() for t in str(form.get("tags") or "").replace("，", ",").split(",") if t.strip()]
                            it["note"] = str(form.get("note") or "")[:200]
                    path2 = os.path.join(sup.sticker_dir(), "stickers.jsonl")
                    _write_jsonl(path2, items)
                    sup.invalidate_stickers()
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/sticker_delete":
                try:
                    items = sup.sticker_list()
                    target = form.get("file") or ""
                    keep = [it for it in items if it.get("file") != target]
                    rec = os.path.join(sup.sticker_dir(), "recycle")
                    os.makedirs(rec, exist_ok=True)
                    src = os.path.join(sup.sticker_dir(), "images", os.path.basename(target))
                    if os.path.isfile(src):
                        os.replace(src, os.path.join(rec, os.path.basename(target)))
                    _write_jsonl(os.path.join(sup.sticker_dir(), "stickers.jsonl"), keep)
                    sup.invalidate_stickers()
                    self._json(200, {"ok": True, "left": len(keep)})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/kb_delete":
                try:
                    sup.store.delete_kb(int(form.get("id") or 0))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/session":
                conv = form.get("conv") or ""
                try:
                    sup.store.reset_session(conv)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"ok": False, "error": str(e)})
                return
            if path == "/api/action":
                cmd = form.get("cmd", "")
                try:
                    ok = sup.action(cmd)
                except Exception as e:
                    self._json(500, {"ok": False, "cmd": cmd, "error": str(e)})
                    return
                resp = {"ok": bool(ok), "cmd": cmd}
                if not ok and cmd in ("start_all", "stop_all"):
                    # 面板要按真实结果提示，别只说“失败”；把排查方向一起带上
                    resp["error"] = ("协议端或大脑没起来，看「日志」页/console.log"
                                     if cmd == "start_all" else "还有进程没停掉，看 console.log")
                self._json(200, resp)
                return
            self._json(404, {"ok": False})

        def _ok_form(self, form):
            tok = str(sup.cfg.get("admin_token") or "")
            supplied = form.get("token") or self._token_from_request()
            return bool(tok) and hmac.compare_digest(str(supplied or ""), tok)

        def log_message(self, *a):
            return

    port = sup.port
    try:
        srv = ConsoleServer(("127.0.0.1", port), H)
    except OSError as e:
        print("console bind failed: %r" % (e,))
        return

    if sup.cfg.get("mobile_autostart", False):
        if not sup.napcat_running():
            sup.start_napcat()
        if sup.login_info() and not sup.brain_running():
            sup.start_brain()

    print("console on http://127.0.0.1:%d" % port)
    srv.serve_forever()


def _q_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0


def _q_peers(cfg):
    raw = str((cfg.get("bot_peers_qq") if cfg is not None else "") or "")
    out = []
    for part in raw.replace("，", ",").replace("、", ",").replace(";", ",").split(","):
        q = _q_int(part)
        if q > 0 and q not in out:
            out.append(q)
    return out


def _q_recent(limit=80):
    """最近在群里说过话的人：返回 [{id,name,n,last_ts}]（QQ 号，按最近活跃排序）。"""
    import sqlite3
    path = os.path.join(DATA_DIR, "bot.db")
    out = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
        try:
            rows = con.execute(
                "SELECT sender_id, COUNT(*) n, MAX(ts) last FROM messages "
                "WHERE direction='in' AND conv LIKE 'g:%' AND sender_id>0 "
                "GROUP BY sender_id ORDER BY last DESC LIMIT ?", (int(limit),)).fetchall()
            for sid, n, last in rows:
                nm = ""
                try:
                    r = con.execute(
                        "SELECT sender_name FROM messages WHERE sender_id=? AND sender_name<>'' "
                        "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                    if r:
                        nm = str(r[0] or "")
                except Exception:
                    pass
                out.append({"id": int(sid), "name": nm, "n": int(n or 0), "last_ts": int(last or 0)})
        finally:
            con.close()
    except Exception:
        return out
    return out


def bot_rounds_state(cfg):
    peers = _q_peers(cfg)
    recent = _q_recent()
    by_id = {m["id"]: m for m in recent}
    plist = [{"id": q, "name": (by_id.get(q) or {}).get("name", ""),
              "last_ts": (by_id.get(q) or {}).get("last_ts", 0)} for q in peers]
    cands = [m for m in recent if m["id"] not in peers]
    return {"ok": True, "enabled": bool(cfg.get("bot_rounds_enabled", True)),
            "limit": _q_int(cfg.get("bot_round_limit")) or 6,
            "reset_min": _q_int(cfg.get("bot_round_reset_min")) or 10,
            "peers": plist, "candidates": cands, "db": os.path.join(DATA_DIR, "bot.db")}


def bot_rounds_save(cfg, form):
    if "enabled" in form:
        cfg.set("bot_rounds_enabled", str(form.get("enabled")).strip() in ("1", "on", "true", "True"))
    if "limit" in form:
        cfg.set("bot_round_limit", max(1, min(100, _q_int(form.get("limit")) or 6)))
    if "reset" in form:
        cfg.set("bot_round_reset_min", max(1, min(1440, _q_int(form.get("reset")) or 10)))
    if "peers" in form:
        raw = str(form.get("peers") or "[]")
        try:
            arr = json.loads(raw) if raw[:1] in "[\"" else raw.split(",")
        except Exception:
            arr = raw.split(",")
        if not isinstance(arr, (list, tuple)):
            arr = []
        qs = []
        for x in arr:
            q = _q_int(x)
            if q > 0 and q not in qs:
                qs.append(q)
        cfg.set("bot_peers_qq", ",".join(str(q) for q in qs))
    return bot_rounds_state(cfg)



# ---------------- v0.67.0 语音（Breeze TTS，跑在主人电脑上） ----------------

VOICE_DEFAULTS = {
    "voice_enabled": False,
    "voice_paused": False,
    "voice_groups": [],
    "voice_privates": [],
    "voice_user_prefs": {},
    "voice_max_chars": 120,
    "voice_timeout_sec": 60,
    "voice_max_audio_sec": 40,
    "voice_queue_max": 3,
    "voice_tts_url": "http://127.0.0.1:17861",
    "voice_instruct": "用软萌小女孩撒娇的语气说：带笑、尾音上扬、更嗲一点，像在跟喜欢的人撒娇，口型松一点。",
    "voice_ref_path": "",
    "voice_ref_text": "在呢在呢，主人这一嗓子喊得本鲸大人尾巴都甩起来了。咋啦，又想我啦？",
    "voice_seed": 10000,
    "voice_idle_unload_min": 10,
    "voice_all_bubbles": True,
    "voice_off_periods": [],
}


def _voice_cfg(cfg, key):
    v = cfg.get(key)
    return VOICE_DEFAULTS[key] if v is None else v


def _voice_url(cfg):
    return str(_voice_cfg(cfg, "voice_tts_url") or "").strip().rstrip("/")


_VOICE_NAMES = {"t": 0.0, "g": {}, "p": {}}


def _onebot_call(cfg, action, params=None, timeout=6):
    """问一次协议端（NapCat）。失败返回 None，绝不让面板因此报错。"""
    import urllib.request
    base = str(cfg.get("onebot_api") or "").strip().rstrip("/")
    if not base:
        return None
    tok = str(cfg.get("onebot_token") or "")
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/" + action, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(j, dict) or j.get("status") == "failed":
        return None
    return j.get("data")


def _voice_names(cfg, ttl=300):
    """群名 / 好友昵称（缓存 5 分钟）。拿不到就返回空表，面板按 QQ 号兜底显示。"""
    now = time.time()
    if now - _VOICE_NAMES["t"] < ttl and (_VOICE_NAMES["g"] or _VOICE_NAMES["p"]):
        return _VOICE_NAMES
    g, p = {}, {}
    try:
        for it in (_onebot_call(cfg, "get_group_list") or []):
            if isinstance(it, dict) and it.get("group_id"):
                g[int(it["group_id"])] = str(it.get("group_name") or "").strip()[:40]
    except Exception:
        pass
    try:
        data = _onebot_call(cfg, "get_friend_list")
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("user_id"):
                    p[int(it["user_id"])] = str(it.get("remark") or it.get("nickname") or "").strip()[:40]
    except Exception:
        pass
    if not g and not p:
        return _VOICE_NAMES
    _VOICE_NAMES["t"] = now
    _VOICE_NAMES["g"] = g
    _VOICE_NAMES["p"] = p
    return _VOICE_NAMES


def _voice_tts_status(cfg):
    """探电脑上的 TTS：/health 判在线，/v1/models 判模型有没有加载。3 秒超时。"""
    import urllib.request
    out = {"ok": False, "loaded": False, "version": ""}
    url = _voice_url(cfg)
    if not url:
        return out
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as r:
            raw = r.read(262144).decode("utf-8", "replace")
        out["ok"] = True
        try:
            j = json.loads(raw)
            for k in ("version", "commit", "build", "backend"):
                if isinstance(j, dict) and j.get(k):
                    out["version"] = str(j.get(k))[:40]
                    break
        except Exception:
            pass
    except Exception:
        return out
    try:
        with urllib.request.urlopen(url + "/v1/models", timeout=3) as r:
            j = json.loads(r.read(262144).decode("utf-8", "replace"))
        data = j.get("data") or []
        if data and isinstance(data[0], dict):
            out["loaded"] = bool(data[0].get("loaded"))
            if not out["version"]:
                out["version"] = str(data[0].get("backend") or "")[:40]
    except Exception:
        pass
    return out


def _cover_online(cfg):
    """电脑侧翻唱服务在线没（只探一下，不触发任务）。"""
    try:
        import urllib.request as _ur2
        with _ur2.urlopen("http://127.0.0.1:17862/health", timeout=3) as r:
            txt = r.read(500).decode("utf-8", "replace").replace(" ", "")
        return '"ok":true' in txt
    except Exception:
        return False


def _cover_health():
    try:
        import urllib.request as _ur2
        with _ur2.urlopen("http://127.0.0.1:17862/health", timeout=3) as r:
            data = json.loads(r.read(4096).decode("utf-8"))
        return {"online": data.get("ok") is True, "running": bool(data.get("running"))}
    except Exception:
        return {"online": False, "running": False}


def cover_state(cfg):
    """面板用：翻唱授权（和语音那套完全独立，互不同步）。"""
    allow_groups = _id_list(cfg.get("allow_groups"))
    allow_users = _id_list(cfg.get("allow_users"))
    owner = _q_int(cfg.get("owner_qq"))
    on_g = set(_id_list(cfg.get("cover_groups")))
    on_p = set(_id_list(cfg.get("cover_privates")))
    priv_ids = []
    for u in list(allow_users) + ([owner] if owner else []):
        if u and u not in priv_ids:
            priv_ids.append(u)
    names = _voice_names(cfg)
    owner_name = str(cfg.get("owner_name") or "").strip()
    health = _cover_health()
    mode = cfg.get("cover_output_mode", "clip")
    return {
        "groups": [{"id": g, "name": names["g"].get(g, ""), "on": g in on_g}
                   for g in allow_groups],
        "privates": [{"id": u,
                      "name": (owner_name if (owner and u == owner) else names["p"].get(u, "")),
                      "on": u in on_p} for u in priv_ids],
        # 主人自己的号永远能点，这里只列授权名单
        "owner": owner,
        "cooldown_min": max(0, min(1440, _q_int(cfg.get("cover_cooldown_min"))
                                          if cfg.get("cover_cooldown_min") is not None else 5)),
        "output_mode": mode if mode in ("clip", "full", "full_voice") else "clip",
        **health,
    }


def cover_save(cfg, form):
    """保存翻唱授权（独立字段 cover_groups / cover_privates，不碰语音那套）。"""
    if "output_mode" in form and form.get("output_mode") not in ("clip", "full", "full_voice"):
        return {"ok": False, "error": "不支持的翻唱输出方式"}
    def _ids(key, cur):
        if key not in form:
            return cur
        v = form.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = [x for x in v.replace("，", ",").split(",") if x.strip()]
        return _id_list(v) if isinstance(v, (list, tuple)) else []

    allow_g = set(_id_list(cfg.get("allow_groups")))
    allow_u = set(_id_list(cfg.get("allow_users")))
    owner = _q_int(cfg.get("owner_qq"))
    groups = [g for g in _ids("groups", _id_list(cfg.get("cover_groups"))) if g in allow_g]
    privs = [p for p in _ids("privates", _id_list(cfg.get("cover_privates")))
             if p in allow_u or (owner and p == owner)]
    cfg.set("cover_groups", groups)
    cfg.set("cover_privates", privs)
    if "cooldown_min" in form:
        try:
            mins = int(form.get("cooldown_min") or 0)
        except Exception:
            mins = 5
        cfg.set("cover_cooldown_min", max(0, min(1440, mins)))
    if "output_mode" in form:
        mode = form.get("output_mode")
        cfg.set("cover_output_mode", mode)
    return {"ok": True, "groups": len(groups), "privates": len(privs),
            "cooldown_min": max(0, min(1440, _q_int(cfg.get("cover_cooldown_min"))
                                            if cfg.get("cover_cooldown_min") is not None else 5)),
            "output_mode": cfg.get("cover_output_mode", "clip"),
            **_cover_health()}


def voice_state(cfg):
    """面板用：白名单全集（群=allow_groups；私聊=allow_users+主人）+ 语音勾选状态。"""
    allow_groups = _id_list(cfg.get("allow_groups"))
    allow_users = _id_list(cfg.get("allow_users"))
    owner = _q_int(cfg.get("owner_qq"))
    on_groups = set(_id_list(cfg.get("voice_groups")))
    on_privs = set(_id_list(cfg.get("voice_privates")))

    priv_ids = []
    for u in list(allow_users) + ([owner] if owner else []):
        if u and u not in priv_ids:
            priv_ids.append(u)

    prefs = {}
    for _pk, _slot in (("voice_user_prefs", "count"), ("voice_user_text", "text")):
        raw = cfg.get(_pk) or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                n = len(v) if isinstance(v, (list, tuple)) else 0
                if n > 0:
                    prefs.setdefault(str(k), {"count": 0, "text": 0})[_slot] = n

    names = _voice_names(cfg)
    owner_name = str(cfg.get("owner_name") or "").strip()
    _off_hit, _off_win = voice.off_now(cfg)
    return {
        # v0.70.0 静音时段（列表，可加多段）+ 现在在不在里面
        "periods": clean_time_windows(cfg.get("voice_off_periods") or []),
        "off_now": bool(_off_hit),
        "off_label": (voice.off_line(_off_win) if _off_hit else ""),
        "enabled": bool(cfg.get("voice_enabled", False)),
        "paused": bool(cfg.get("voice_paused", False)),
        "auto_choose": bool(cfg.get("voice_auto_choose", False)),
        "groups": [{"id": g, "name": names["g"].get(g, ""), "on": g in on_groups}
                   for g in allow_groups],
        "privates": [{"id": u,
                      "name": (owner_name if (owner and u == owner) else names["p"].get(u, "")),
                      "on": u in on_privs} for u in priv_ids],
        "prefs": prefs,
        "idle_unload_min": int(_voice_cfg(cfg, "voice_idle_unload_min") or 0),
        "all_bubbles": bool(cfg.get("voice_all_bubbles", True)),
        # 没表态的人按什么默认收：群聊默认语音、私聊默认文字（面板可改）
        "group_default": bool(cfg.get("voice_group_default", True)),
        "private_default": bool(cfg.get("voice_private_default", True)),
        "max_chars": _q_int(_voice_cfg(cfg, "voice_max_chars")) or 120,
        "instruct": str(_voice_cfg(cfg, "voice_instruct") or ""),
        "ref_path": str(_voice_cfg(cfg, "voice_ref_path") or ""),
        "ref_text": str(_voice_cfg(cfg, "voice_ref_text") or ""),
        "tts": _voice_tts_status(cfg),
    }


def voice_save(cfg, form):
    """保存语音设置；顺手按消息白名单联动清理（白名单删了，语音授权跟着没）。"""
    def _bool(key, cur):
        if key not in form:
            return cur
        v = form.get(key)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "on", "true", "yes")

    def _ids(key, cur):
        if key not in form:
            return cur
        v = form.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = [x for x in v.replace("，", ",").split(",") if x.strip()]
        return _id_list(v) if isinstance(v, (list, tuple)) else []

    allow_g = set(_id_list(cfg.get("allow_groups")))
    allow_u = set(_id_list(cfg.get("allow_users")))
    owner = _q_int(cfg.get("owner_qq"))

    enabled = _bool("enabled", bool(cfg.get("voice_enabled", False)))
    paused = _bool("paused", bool(cfg.get("voice_paused", False)))
    groups = [g for g in _ids("groups", _id_list(cfg.get("voice_groups"))) if g in allow_g]
    privs = [p for p in _ids("privates", _id_list(cfg.get("voice_privates")))
             if p in allow_u or (owner and p == owner)]

    cfg.set("voice_enabled", enabled)
    cfg.set("voice_paused", paused)
    if "auto_choose" in form:
        cfg.set("voice_auto_choose", _bool("auto_choose", bool(cfg.get("voice_auto_choose", False))))
    cfg.set("voice_groups", groups)
    cfg.set("voice_privates", privs)

    if "max_chars" in form:
        cfg.set("voice_max_chars", max(10, min(2000, _q_int(form.get("max_chars")) or 120)))
    if "instruct" in form:
        cfg.set("voice_instruct", str(form.get("instruct") or "").strip()[:300])
    if "ref_path" in form:
        cfg.set("voice_ref_path", str(form.get("ref_path") or "").strip()[:400])
    if "ref_text" in form:
        cfg.set("voice_ref_text", str(form.get("ref_text") or "").strip()[:500])
    if "idle_unload_min" in form:
        cfg.set("voice_idle_unload_min", max(0, min(1440, _q_int(form.get("idle_unload_min")))))
    if "all_bubbles" in form:
        cfg.set("voice_all_bubbles", _bool("all_bubbles", bool(cfg.get("voice_all_bubbles", True))))
    if "group_default" in form:
        cfg.set("voice_group_default", _bool("group_default", bool(cfg.get("voice_group_default", True))))
    if "private_default" in form:
        cfg.set("voice_private_default", _bool("private_default", bool(cfg.get("voice_private_default", True))))

    # v0.70.0 静音时段：整份列表一起交上来（不是单个表单字段），坏段直接丢
    periods_out = clean_time_windows(cfg.get("voice_off_periods") or [])
    if "periods" in form:
        raw_p = form.get("periods")
        if isinstance(raw_p, str):
            try:
                raw_p = json.loads(raw_p)
            except Exception:
                raw_p = []
        periods_out = clean_time_windows(raw_p)
        cfg.set("voice_off_periods", periods_out)

    # 个人表态联动清理：g:x 只留 x 在群白名单；p:x 只留 x 在私聊白名单或主人
    # （voice_user_prefs = 明确要语音，voice_user_text = 明确要文字，两边都要清）
    for _pk in ("voice_user_prefs", "voice_user_text"):
        new_prefs = {}
        raw_prefs = cfg.get(_pk) or {}
        if isinstance(raw_prefs, dict):
            for k, v in raw_prefs.items():
                key = str(k)
                if key.startswith("g:"):
                    cid = _q_int(key[2:])
                    if cid in allow_g:
                        new_prefs[key] = _id_list(v if isinstance(v, (list, tuple)) else [])
                elif key.startswith("p:"):
                    cid = _q_int(key[2:])
                    if cid in allow_u or (owner and cid == owner):
                        new_prefs[key] = _id_list(v if isinstance(v, (list, tuple)) else [])
                else:
                    continue
                if not new_prefs.get(key):
                    new_prefs.pop(key, None)
        if new_prefs != raw_prefs:
            cfg.set(_pk, new_prefs)

    return {"ok": True, "groups": len(groups), "privates": len(privs),
            "periods": len(periods_out)}


def _voice_wav_wrap(pcm, rate=24000):
    """电脑那边回的是 24k 单声道 PCM 时，包一层 WAV 头，浏览器才播得出来。"""
    import struct
    data = bytes(pcm or b"")
    if data[:4] == b"RIFF":
        return data
    n = len(data)
    hdr = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
           + b"data" + struct.pack("<I", n))
    return hdr + data


def _voice_pad(cfg, data):
    """跟大脑那边一样：C4 后处理（升调/削气声）+ 头尾补静音，试听才和真发出去一致。"""
    try:
        import voice as _v
        return _v.tune_and_pad(data, cfg)
    except Exception:
        return data


def voice_test(cfg, form):
    """合成一句试听。返回 (ok, content_type, body, error)。"""
    import urllib.request, urllib.error, uuid
    text = str((form or {}).get("text") or "").strip() or "主人，试听一句，听得清吗？"
    text = text[:200]
    ref_path = str(_voice_cfg(cfg, "voice_ref_path") or "")
    ref_text = str(_voice_cfg(cfg, "voice_ref_text") or "")
    instruct = str(form.get("instruct") if "instruct" in (form or {}) else _voice_cfg(cfg, "voice_instruct") or "")
    seed = _q_int(form.get("seed")) if "seed" in (form or {}) else _q_int(_voice_cfg(cfg, "voice_seed"))
    timeout = max(10, min(300, _q_int(_voice_cfg(cfg, "voice_timeout_sec")) * 2 or 120))
    url = _voice_url(cfg)
    if not url:
        return (False, "", b"", "语音服务地址没配")
    if not os.path.isfile(ref_path):
        return (False, "", b"", "参考音文件不存在：%s" % ref_path)

    err = ""

    # 1) 先按约定发 JSON
    payload = {"model": "breeze", "input": text, "voice_ref": ref_path,
               "reference_text": ref_text, "instruct": instruct, "seed": seed,
               "response_format": "wav"}
    try:
        req = urllib.request.Request(url + "/v1/audio/speech",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (True, r.headers.get("Content-Type") or "", _voice_pad(cfg, r.read()), "")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        err = "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        err = repr(e)[:160]

    # 2) 退 multipart（audio.cpp 官方写法：ref_audio 传文件）
    try:
        bd = "----dafeiyuvoice" + uuid.uuid4().hex
        chunks = []

        def field(name, value):
            chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                           % (bd, name, value)).encode("utf-8"))

        field("text", text)
        field("input", text)
        field("ref_text", ref_text)
        field("reference_text", ref_text)
        field("instruct", instruct)
        field("instruction", instruct)
        field("seed", str(seed))
        field("model", "breeze")
        field("response_format", "wav")
        with open(ref_path, "rb") as fh:
            ref_bytes = fh.read()
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"ref.wav\"\r\n"
                       "Content-Type: audio/wav\r\n\r\n" % bd).encode("utf-8") + ref_bytes + b"\r\n")
        chunks.append(("--%s--\r\n" % bd).encode("utf-8"))
        req = urllib.request.Request(url + "/v1/audio/speech", data=b"".join(chunks),
                                     headers={"Content-Type": "multipart/form-data; boundary=" + bd},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (True, r.headers.get("Content-Type") or "", _voice_pad(cfg, r.read()), "")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return (False, "", b"", "%s；multipart 也失败：HTTP %s %s" % (err, e.code, detail))
    except Exception as e:
        return (False, "", b"", "%s；multipart 也失败：%r" % (err, e))


def _clamp(form, key, cast, lo, hi, default):
    if key not in form:
        return None
    try:
        v = cast(str(form.get(key) or "").strip())
    except Exception:
        return default
    return max(lo, min(hi, v))


VIDEO_BOOL_KEYS = ("video_browse_enabled", "video_browse_notify", "video_browse_like",
                  "video_browse_fav", "video_browse_reply_owner", "video_browse_reply_strangers",
                  "video_browse_growth_enabled", "video_browse_kb_enabled")
VIDEO_INT_KEYS = {
    "video_browse_search_ratio": (0, 100),
    "video_browse_start_jitter_min": (0, 240),
    "video_browse_duration_min": (1, 180),
    "video_browse_duration_max": (1, 180),
    "video_browse_sessions_per_day": (0, 12),
    "video_browse_max_minutes_per_day": (0, 300),
    "video_browse_manual_max_min": (1, 180),
    "video_browse_daily_token_limit": (0, 10 ** 9),
    "video_browse_share_max_per_day": (0, 6),
    "video_browse_share_score_min": (1, 5),
    "video_browse_comment_max_per_day": (0, 10),
    "video_browse_reply_owner_max_per_day": (0, 50),
    "video_browse_reply_stranger_max_per_day": (0, 50),
    "video_browse_reply_poll_min": (1, 1440),
    "video_browse_like_max_per_day": (0, 50),
    "video_browse_fav_max_per_day": (0, 50),
    "video_browse_bili_owner_uid": (0, 2 ** 63 - 1),
    "video_browse_bili_self_mid": (0, 2 ** 63 - 1),
    "video_browse_growth_learn_max": (0, 5),
    "video_browse_growth_max_active": (1, 200),
    "video_browse_kb_min_worth": (0, 5),
    "video_browse_kb_keep": (20, 2000),
}
VIDEO_STR_KEYS = ("video_browse_windows", "video_browse_keywords", "video_browse_growth_goals",
                  "video_browse_model_ids", "video_browse_model_priority")


def _vb_num(store, key, default=0):
    try:
        return int(store.get_setting(key) or default)
    except Exception:
        return default


_BILI_STATUS_CACHE = {"ts": 0, "data": {}}


def bili_account_state(cfg=None, force=False):
    now = time.time()
    if not force and _BILI_STATUS_CACHE.get("data") and now - float(_BILI_STATUS_CACHE.get("ts") or 0) < 120:
        return dict(_BILI_STATUS_CACHE["data"])
    out = {"available": False, "logged_in": False, "user": {}, "owner_uid": 0}
    try:
        import subprocess
        binp = os.environ.get("DAFEIYU_BILI_BIN") or os.path.join(BOT_HOME, "tools", "bin", "bili")
        home = os.environ.get("DAFEIYU_BILI_HOME") or os.path.join(BOT_HOME, "bili")
        if not os.path.exists(binp):
            return out
        env = dict(os.environ)
        env["HOME"] = BOT_HOME
        env["BILI_CLI_HOME"] = home
        r = subprocess.run([binp, "auth", "status"], capture_output=True, timeout=12, env=env)
        raw = (r.stdout or b"").decode("utf-8", "replace").strip()
        data = {}
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        out["available"] = True
        if data.get("ok"):
            d = data.get("data") or {}
            out["logged_in"] = bool(d.get("logged_in"))
            out["user"] = d.get("user") or {}
        if cfg is not None:
            out["owner_uid"] = int(cfg.get("video_browse_bili_owner_uid") or 0)
    except Exception as e:
        out["error"] = str(e)[:160]
    _BILI_STATUS_CACHE["ts"] = now
    _BILI_STATUS_CACHE["data"] = dict(out)
    return out


def _kb_scope(sup):
    """知识库按主人私聊域存：p:<owner_qq>。"""
    try:
        owner = int(sup.cfg.get("owner_qq") or 0)
    except Exception:
        owner = 0
    return ("p:%d" % owner) if owner else ""


def _kb_doc_public(d):
    return {"id": int(d.get("id") or 0), "name": d.get("name") or "",
            "topic": d.get("topic") or "其他", "kind": d.get("kind") or "doc",
            "summary": d.get("summary") or "", "points": d.get("points") or [],
            "tags": d.get("tags") or "", "source": d.get("source") or "",
            "quality": int(d.get("quality") or 0),
            "created_at": int(d.get("created_at") or 0),
            "updated_at": int(d.get("updated_at") or 0),
            "chunks": int(d.get("chunks") or 0), "hits": int(d.get("hits") or 0)}


def kb_state(sup, action="list", q=None):
    """面板读知识库：state 只回统计；list 回卡片（可按分类/关键词过滤）。"""
    q = q or {}
    scope = _kb_scope(sup)
    if not scope:
        return {"ok": False, "error": "没设置主人 QQ（owner_qq），知识库没法定位。"}
    store = sup.store
    if action == "state":
        return {"ok": True, "scope": scope, "stats": store.kb_stats(scope)}
    topic = (q.get("topic") or [""])[0]
    kw = (q.get("q") or [""])[0]
    docs = store.kb_list(scope, topic=(topic or None), keyword=kw, limit=500)
    return {"ok": True, "scope": scope, "stats": store.kb_stats(scope),
            "docs": [_kb_doc_public(d) for d in docs]}


def kb_action(sup, form):
    """面板写知识库：delete 删一份；set 改分类/质量。"""
    scope = _kb_scope(sup)
    if not scope:
        return {"ok": False, "error": "没设置主人 QQ"}
    act = str(form.get("action") or "").strip()
    try:
        did = int(form.get("id") or 0)
    except Exception:
        did = 0
    if act == "delete" and did:
        sup.store.delete_kb(did)
        return {"ok": True, "stats": sup.store.kb_stats(scope)}
    if act == "set" and did:
        kw = {}
        if "topic" in form:
            kw["topic"] = (form.get("topic") or "其他").strip()[:20]
        if "quality" in form:
            try:
                kw["quality"] = max(0, min(5, int(form.get("quality") or 0)))
            except Exception:
                kw["quality"] = 0
        ok = sup.store.kb_update(did, **kw)
        return {"ok": bool(ok), "stats": sup.store.kb_stats(scope)}
    return {"ok": False, "error": "unknown action"}


def _vb_history_today(limit=200):
    """今天刷过的视频（读大脑写的 jsonl，不带转写正文）。"""
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "video_browse_history.jsonl")
    out = []
    try:
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                ts = int(item.get("ts") or 0)
                if not ts or time.strftime("%Y-%m-%d", time.localtime(ts)) != day:
                    continue
                out.append({"ts": ts, "bvid": str(item.get("bvid") or ""),
                            "title": str(item.get("title") or "")[:80],
                            "duration": int(item.get("duration") or 0),
                            "frames": int(item.get("frames") or 0),
                            "source": str(item.get("source") or ""),
                            "summary": str(item.get("summary") or "").replace("\n", " ")[:120]})
    except Exception:
        return []
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:max(1, int(limit))]


def video_browse_state(cfg, store):
    out = {"ok": True, "config": {}, "status": {}, "profile": {}, "report": {}, "bili": bili_account_state(cfg)}
    try:
        _rows = _vb_history_today(200)
    except Exception:
        _rows = []
    out["history"] = _rows[:30]
    out["watched_today"] = len(_rows)
    for k in VIDEO_BOOL_KEYS:
        out["config"][k] = bool(cfg.get(k))
    for k in VIDEO_INT_KEYS:
        out["config"][k] = int(cfg.get(k) or 0)
    for k in VIDEO_STR_KEYS:
        out["config"][k] = str(cfg.get(k) or "")
    try:
        raw = store.get_setting("vbrowse:status") or "{}"
        out["status"] = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        out["status"] = {}
    try:
        p = os.path.join(DATA_DIR, "video_growth_profile.json")
        if os.path.exists(p):
            out["profile"] = json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        out["profile"] = {}
    try:
        p = os.path.join(DATA_DIR, "video_growth_report.json")
        if os.path.exists(p):
            out["report"] = json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        out["report"] = {}
    if store.get_setting('self_continuity_initialized'):
        out['unified_self']=True
        out['legacy_profile']=out['profile']
        out['profile']=store.self_snapshot()
        out['report']={}
    day = time.strftime("%Y-%m-%d")
    out["models"] = []
    try:
        for m in (cfg.models() or []):
            mid = str(m.get("id") or "").strip()
            if mid:
                out["models"].append({"id": mid, "label": str(m.get("name") or m.get("model") or mid)[:80],
                                      "model": str(m.get("model") or "")[:80]})
    except Exception:
        out["models"] = []
    out["today"] = {
        "used_minutes": _vb_num(store, "vbrowse:minutes:" + day),
        "tokens": _vb_num(store, "vbrowse:tokens:" + day),
        "token_limit": int(cfg.get("video_browse_daily_token_limit") or 0),
        "shares": _vb_num(store, "vbrowse:shares:" + day),
        "comments": _vb_num(store, "vbrowse:comments:" + day),
        "likes": _vb_num(store, "vbrowse:likes:" + day),
        "favs": _vb_num(store, "vbrowse:favs:" + day),
        "reply_owner": _vb_num(store, "vbrowse:reply:owner:" + day),
        "reply_stranger": _vb_num(store, "vbrowse:reply:stranger:" + day),
    }
    return out


def save_video_browse(cfg, form):
    raw_win = str(form.get("video_browse_windows") or "")
    import re as _re
    for part in _re.split(r"[,，;；\n]+", raw_win):
        part = part.strip()
        m = _re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", part)
        if not m:
            continue
        sh, sm, eh, em = [int(x) for x in m.groups()]
        if eh * 60 + em <= sh * 60 + sm:
            raise ValueError("跨午夜时段请拆成两段，例如 23:00-23:59 + 00:00-01:00")
    for k in VIDEO_BOOL_KEYS:
        if k in form:
            cfg.set(k, str(form.get(k) or "").lower() in ("1", "on", "true", "yes"))
    for k, (lo, hi) in VIDEO_INT_KEYS.items():
        if k not in form:
            continue
        try:
            v = int(str(form.get(k, "")).strip() or 0)
            cfg.set(k, max(lo, min(hi, v)))
        except Exception:
            pass
    for k in VIDEO_STR_KEYS:
        if k in form:
            cfg.set(k, str(form.get(k) or "").strip()[:400])


def apply_settings(cfg, form):
    if "allow_all_users" in form:
        cfg.set("allow_all_users", str(form.get("allow_all_users") or "").strip().lower() in ("1", "on", "true", "yes"))
    if form.get("api_key"):
        cfg.set("api_key", form["api_key"].strip())
    if "owner_qq" in form:
        try:
            cfg.set("owner_qq", max(0, int(str(form["owner_qq"]).strip() or 0)))
        except Exception:
            cfg.set("owner_qq", 0)
    if "owner_name" in form:
        cfg.set("owner_name", str(form.get("owner_name") or "").strip()[:40])
    for key in ("allow_users", "allow_groups"):
        if key in form:
            out = []
            for part in str(form.get(key) or "").replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit() and int(part) > 0 and int(part) not in out:
                    out.append(int(part))
            cfg.set(key, out)
    if "group_companion_groups" in form:
        allowed = set(_id_list(cfg.get("allow_groups")))
        selected = _id_list(str(form.get("group_companion_groups") or "").replace("，", ",").split(","))
        cfg.set("group_companion_groups", [g for g in selected if g in allowed])
    if "group_need_at" in form:
        cfg.set("group_need_at", form.get("group_need_at") in ("1", "on", "true", "True"))
    for key in ("group_trigger_name", "group_trigger_quote", "reply_autonomy_enabled", "self_growth_enabled"):
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
    if "model_thinking" in form:
        cfg.set("model_thinking", form.get("model_thinking") in ("1", "on", "true", "True"))
    # v0.57.0 思考强度（reasoning_effort）与原图直传开关；写错/没见过的值保留原样
    if "model_reasoning_effort" in form:
        _eff = str(form.get("model_reasoning_effort") or "").strip().lower()
        if _eff in ("low", "medium", "high", "auto"):
            cfg.set("model_reasoning_effort", _eff)
    if "image_direct_feed" in form:
        cfg.set("image_direct_feed", form.get("image_direct_feed") in ("1", "on", "true", "True"))
    # v0.58.0 私聊攒批（只对私聊生效）：总开关 + 私聊首气泡不再随机等
    for key in ("private_batch", "reply_delay_skip_first_private"):
        if key in form:
            cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
    if "encrypt_content" in form:
        cfg.set("encrypt_content", form.get("encrypt_content") in ("1", "on", "true", "True"))
    if "proactive_enabled" in form:
        cfg.set("proactive_enabled", form.get("proactive_enabled") in ("1", "on", "true", "True"))
    if "share_enabled" in form:
        cfg.set("share_enabled", form.get("share_enabled") in ("1", "on", "true", "True"))
    if "share_private_chats" in form:
        cfg.set("share_private_chats", form.get("share_private_chats") in ("1", "on", "true", "True"))
    if "share_judge_enabled" in form:
        cfg.set("share_judge_enabled", form.get("share_judge_enabled") in ("1", "on", "true", "True"))
    if "music_source" in form:
        _ms = str(form.get("music_source") or "").strip()
        if _ms in ("netease", "qq"):
            cfg.set("music_source", _ms)
    if "web_search_enabled" in form:
        cfg.set("web_search_enabled", form.get("web_search_enabled") in ("1", "on", "true", "True"))
    if "passive_group_store" in form:
        cfg.set("passive_group_store", form.get("passive_group_store") in ("1", "on", "true", "True"))
    # v0.63.0：记忆分层遗忘总开关
    if "memory_decay_enabled" in form:
        cfg.set("memory_decay_enabled", form.get("memory_decay_enabled") in ("1", "on", "true", "True"))
    if "image_watch" in form:
        cfg.set("image_watch", form.get("image_watch") in ("1", "on", "true", "True"))
    # v0.40.0 表情包判定开关（在「表情包」页保存）
    for key in ("sticker_judge_enabled", "sticker_wait_ready", "sticker_text_only", "sticker_fit_enabled"):
        if key in form:
            cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
    for key, allowed in (("sticker_recheck", ("high", "always", "off")),):
        if key in form:
            v = str(form.get(key) or "").strip().lower()
            if v in allowed:
                cfg.set(key, v)
    for key in ("bot_paused", "group_speak_enabled", "group_speak_idle"):
        if key in form:
            cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
    for key in ("relay_enabled", "relay_quiet_enabled", "lookup_enabled", "errand_enabled"):
        if key in form:
            cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
    # QQ空间自主发说说 + 邮件告警：开关类
    for key in ("qzone_enabled", "qzone_comment_reply", "qzone_memory_inject", "alert_email_enabled"):
        if key in form:
            cfg.set(key, form.get(key) in ("1", "on", "true", "True"))
    if "qzone_mode" in form:
        _qmode = str(form.get("qzone_mode") or "").strip().lower()
        if _qmode in ("dry", "live"):
            cfg.set("qzone_mode", _qmode)
    if "qzone_image_policy" in form:
        _qpol = str(form.get("qzone_image_policy") or "").strip().lower()
        if _qpol in ("sticker", "none", "random"):
            cfg.set("qzone_image_policy", _qpol)
    if "qzone_active_hours" in form:
        # 时段写成 "9-23"，表示 9:00~23:00；格式不对就保持原值，不让坏值进配置
        _qhour = str(form.get("qzone_active_hours") or "").strip().replace("：", "-").replace("~", "-")
        _qparts = [x.strip() for x in _qhour.split("-")]
        if len(_qparts) == 2 and all(x.isdigit() and 0 <= int(x) <= 23 for x in _qparts):
            cfg.set("qzone_active_hours", "%d-%d" % (int(_qparts[0]), int(_qparts[1])))
    for key in ("alert_smtp_host", "alert_smtp_user", "alert_mail_to"):
        if key in form:
            cfg.set(key, str(form.get(key) or "").strip()[:120])
    if form.get("alert_smtp_pass"):
        # 留空=不改，和 api_key 一样
        cfg.set("alert_smtp_pass", str(form.get("alert_smtp_pass")).strip())
    for key, cast, lo, hi in (
        ("reply_delay_min", float, 0.0, 10.0),
        ("reply_delay_max", float, 0.0, 10.0),
        # v0.58.0 私聊攒批：安静窗口 0.5~30 秒、时间兜底 0~120 秒（0=不限）、条数兜底 0~200（0=不限）
        ("private_batch_sec", float, 0.5, 30.0),
        ("private_batch_max_msgs", int, 0, 200),
        ("private_batch_max_wait_sec", float, 0.0, 120.0),
        ("sticker_recall_count", int, 1, 20),
        ("sticker_send_gap_sec", float, 0.0, 10.0),
        ("sticker_brief_history", int, 5, 50),
        ("sticker_fit_min", int, 0, 10),
        ("sticker_fit_rounds", int, 1, 3),
        ("sticker_fit_tries", int, 1, 4),
        ("sticker_panel_size", int, 4, 8),
        ("sticker_conf_min", float, 0.0, 1.0),
        ("sticker_text_match_min", float, 0.0, 1.0),
        ("sticker_rel_min", float, 0.0, 1.0),
        ("context_limit", int, 4000, 1000000),
        ("keep_recent", int, 5, 200),
        ("idle_reset_sec", int, 60, 604800),
        ("vision_max_bytes", int, 1048576, 33554432),
        ("model_timeout", int, 5, 300),
        ("reply_workers", int, 1, 12),
        ("event_queue_size", int, 50, 5000),
        ("model_max_concurrent", int, 1, 16),
        ("memory_workers", int, 1, 8),
        ("group_store_max_per_day", int, 0, 50000),
        ("proactive_idle_sec", int, 60, 604800),
        ("proactive_min_idle_sec", int, 60, 21600),
        ("proactive_max_idle_sec", int, 60, 21600),
        ("proactive_max_per_day", int, 0, 50),
        ("proactive_quiet_start", int, 0, 23),
        ("proactive_quiet_end", int, 0, 23),
        ("proactive_quiet_jitter_min", int, 0, 240),
        ("group_speak_quiet_min", int, 5, 1440),
        ("group_speak_gap_min", int, 5, 1440),
        ("group_speak_max_per_day", int, 0, 20),
        ("group_speak_quiet_start", int, 0, 23),
        ("group_speak_quiet_end", int, 0, 23),
        ("memory_extract_every", int, 2, 50),
        ("memory_person_max", int, 10, 500),
        ("memory_group_max", int, 10, 500),
        # v0.55.6：记忆注入预算与召回条数（以前只在代码里，面板调不了）
        ("memory_recall_person", int, 1, 60),
        ("memory_recall_group", int, 1, 60),
        ("memory_recall_shared", int, 0, 30),
        ("memory_inject_tokens", int, 500, 20000),
        ("memory_event_days", int, 1, 365),
        # v0.63.0：记忆分层遗忘
        ("memory_decay_low_never_days", int, 1, 365),
        ("memory_decay_low_days", int, 1, 365),
        ("memory_decay_mid_days", int, 1, 3650),
        ("memory_decay_purge_days", int, 7, 3650),
        ("memory_high_tier_max", int, 5, 200),
        ("favor_decay_days", int, 7, 90),
        ("grudge_days", int, 7, 365),
        ("image_watch_per_hour", int, 0, 300),
        ("image_watch_recent", int, 0, 5),
        ("image_watch_window_sec", int, 60, 3600),
        ("share_max_per_day", int, 0, 50),
        ("share_min_gap_min", int, 0, 1440),
        ("share_min_juice", int, 1, 5),
        ("share_pending_max", int, 1, 50),
        ("share_pending_ttl_h", int, 1, 720),
        ("tattle_max_per_day", int, 0, 50),
        ("share_seen_window_h", int, 1, 168),
        ("share_dump_max", int, 1, 5),
        ("web_search_count", int, 1, 20),
        ("relay_max_per_day", int, 0, 50),
        ("relay_quiet_start", int, 0, 23),
        ("relay_quiet_end", int, 0, 23),
        ("qzone_ugc_right", int, 1, 128),
        ("qzone_daily_max", int, 0, 50),
        ("qzone_min_interval_sec", int, 300, 604800),
        ("qzone_max_len", int, 60, 1000),
        ("qzone_min_score", int, 0, 10),
        ("qzone_comment_poll_sec", int, 60, 86400),
        ("qzone_comment_reply_daily_max", int, 0, 50),
        ("alert_smtp_port", int, 1, 65535),
    ):
        v = _clamp(form, key, cast, lo, hi, None)
        if v is not None:
            cfg.set(key, v)
    try:
        if float(cfg.get("reply_delay_min") or 0) > float(cfg.get("reply_delay_max") or 0):
            cfg.set("reply_delay_max", cfg.get("reply_delay_min"))
    except Exception:
        pass



if __name__ == "__main__":
    main()
