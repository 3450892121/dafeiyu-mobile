# -*- coding: utf-8 -*-
"""公共工具（服务器版 / Linux）。

和 Windows 版的区别只有两处，其余行为完全一致：
1. 密钥不再用 Windows DPAPI（换机器解不开），改成「本机密钥文件 + Fernet」，
   前缀 enc2:；遇到从 Windows 拷来的 enc1: 一律当成空字符串，让面板提示重填。
2. 单实例锁：Linux 用 flock /tmp/<name>.lock，Windows 逻辑保留（照抄原实现）。
"""
import base64
import hashlib
import hmac
import os
import re
import threading
import time
import traceback

IS_WIN = os.name == "nt"

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_HOME = os.environ.get("DAFEIYU_HOME") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DafeiyuBot")
DATA_DIR = os.path.join(BOT_HOME, "data")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_KEEP = 3
_lock = threading.Lock()
PREFIX = "enc1:"
PREFIX_V2 = "enc2:"
KEY_PATH = os.path.join(DATA_DIR, ".secret_key")
_instance_locks = []


def _read_key():
    with open(KEY_PATH, "rb") as file:
        raw = file.read(32)
    if len(raw) != 32:
        raise ValueError("本实例加密材料损坏，未覆盖原文件")
    return raw


def _load_key(create):
    try:
        return _read_key()
    except FileNotFoundError:
        if not create:
            raise ValueError("解密所需的本实例密钥缺失")
    os.makedirs(DATA_DIR, exist_ok=True)
    # Fresh control/brain processes must publish one complete key, even if a writer is killed.
    with open(KEY_PATH + ".lock", "a+b") as lock:
        if IS_WIN:
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _read_key()
        except FileNotFoundError:
            if any(os.path.isfile(os.path.join(DATA_DIR, name)) for name in ("runtime-ready.json", "bot.db")):
                raise ValueError("已有实例的密钥缺失，已停止生成新密钥")
        raw = os.urandom(32)
        temporary = KEY_PATH + ".tmp"
        with open(temporary, "wb") as file:
            os.chmod(temporary, 0o600)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, KEY_PATH)
        return raw


def _fernet(create):
    """缺少 cryptography 时由写入方明确失败，绝不降级成明文。"""
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    key = base64.urlsafe_b64encode(_load_key(create))
    return Fernet(key)


def dpapi_encrypt(text, strict=False):
    if text is None:
        return None
    raw = str(text).encode("utf-8")
    if not raw:
        return ""
    try:
        f = _fernet(create=True)
        if f is None:
            raise ValueError("手机版加密组件不可用，拒绝明文保存")
        return PREFIX_V2 + f.encrypt(raw).decode("ascii")
    except Exception as e:
        raise ValueError("encrypt failed: %r" % (e,)) from e


def dpapi_decrypt(text, strict=False):
    if text is None:
        return text
    s = str(text)
    if s.startswith(PREFIX_V2):
        try:
            f = _fernet(create=False)
            if f is None:
                return ""
            return f.decrypt(s[len(PREFIX_V2):].encode("ascii")).decode("utf-8", "replace")
        except Exception as e:
            if strict:
                raise ValueError("decrypt failed: %r" % (e,)) from e
            return ""
    if s.startswith(PREFIX):
        # Windows 机器的 DPAPI 密文，Linux 解不开：当空，让用户重填
        return ""
    return s


def acquire_single_instance(name):
    """同名进程只允许一个。Windows 用命名互斥体，Linux 用 flock 锁文件。"""
    if IS_WIN:
        return _win_mutex(name)
    try:
        import fcntl
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))
        lock_path = os.path.join("/tmp", safe + ".lock")
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            fh.close()
            return None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except Exception:
            pass
        _instance_locks.append(fh)
        return fh
    except Exception:
        return None


# ---------- 以下只在 Windows 生效（保留原实现，方便同一份代码两边都能跑）----------
def _win_mutex(name):
    try:
        import ctypes
        import ctypes.wintypes as wt
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        kernel32.CreateMutexW.restype = wt.HANDLE
        handle = kernel32.CreateMutexW(None, False, str(name))
        err = ctypes.get_last_error()
        if not handle:
            return None
        if err == 183:
            kernel32.CloseHandle(handle)
            return None
        _instance_locks.append(handle)
        return handle
    except Exception:
        return None


def _win_encrypt(text, strict=False):
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        raw = str(text).encode("utf-8")
        buf = ctypes.create_string_buffer(raw, len(raw))
        bin_blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out = DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(bin_blob), None, None, None, None, 0, ctypes.byref(out)):
            raise ValueError("CryptProtectData failed")
        try:
            return PREFIX + base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
        finally:
            kernel32.LocalFree(out.pbData)
    except Exception as e:
        if strict:
            raise ValueError("DPAPI encrypt failed: %r" % (e,)) from e
        return str(text)


def tokenize(text, key="idx"):
    """中文 2-gram + 英文数字词，做 HMAC 不可逆索引。"""
    import re
    s = str(text or "")
    toks = set()
    for w in re.findall(r"[A-Za-z0-9_]{2,}", s.lower()):
        toks.add(w)
    for seg in re.findall(r"[\u4e00-\u9fa5]+", s):
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return {hmac.new(key.encode(), t.encode(), hashlib.sha256).hexdigest()[:24] for t in toks}


def log(tag, msg):
    line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _lock:
            _rotate_if_needed()
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def log_exc(tag, e):
    log(tag, "EXC %r\n%s" % (e, traceback.format_exc(limit=6)))


def _rotate_if_needed():
    try:
        if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) < LOG_MAX_BYTES:
            return
        for i in range(LOG_KEEP - 1, -1, -1):
            src = LOG_PATH + ("" if i == 0 else ".%d" % i)
            dst = LOG_PATH + ".%d" % (i + 1)
            if os.path.exists(dst):
                os.remove(dst)
            if os.path.exists(src):
                os.replace(src, dst)
    except Exception:
        pass


# ---------- v0.70.0 时间窗（语音静音时段用；纯数据工具，不依赖任何业务模块）----------
# 一段就是一个 dict：
#   {"start": "23:30", "end": "07:00", "days": [1..7], "on": True, "note": "睡觉"}
# days 用 ISO 星期（周一=1 … 周日=7）；空/没有当「每天」。
ALL_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
WEEKDAY_NAMES = ("", "周一", "周二", "周三", "周四", "周五", "周六", "周日")


def parse_hm(text):
    """「23:30」「9:5」「23：30」「2330」→ 当天第几分钟；认不出来返回 None。"""
    s = str(text or "").strip().replace("：", ":").replace(".", ":").replace("．", ":")
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
    if not m:
        m2 = re.match(r"^(\d{2})(\d{2})$", s)
        if not m2:
            return None
        m = m2
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def fmt_hm(minutes):
    m = int(minutes) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def clean_time_windows(raw, limit=24):
    """校验时间窗列表：认不出来的整段丢掉，剩下的归一化（不改顺序、不自动排序）。"""
    out = []
    if not isinstance(raw, (list, tuple)):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        a, b = parse_hm(it.get("start")), parse_hm(it.get("end"))
        if a is None or b is None or a == b:
            continue
        days = []
        raw_days = it.get("days")
        if isinstance(raw_days, (list, tuple)):
            for d in raw_days:
                try:
                    d = int(str(d).strip())
                except Exception:
                    continue
                if 1 <= d <= 7 and d not in days:
                    days.append(d)
        if not days:
            days = list(ALL_WEEKDAYS)
        days.sort()
        out.append({"start": fmt_hm(a), "end": fmt_hm(b), "days": days,
                    "on": bool(it.get("on", True)),
                    "note": str(it.get("note") or "").strip()[:20]})
        if len(out) >= max(1, int(limit)):
            break
    return out


def window_hit(w, when=None):
    """现在（或 when 那一刻）在不在这段时间窗里。跨零点的段按「开始那天」算星期。"""
    try:
        a, b = parse_hm(w.get("start")), parse_hm(w.get("end"))
        if a is None or b is None or a == b:
            return False
        days = w.get("days") or ALL_WEEKDAYS
        days = [int(x) for x in days]
        lt = time.localtime(float(when) if when is not None else time.time())
    except Exception:
        return False
    minute = lt.tm_hour * 60 + lt.tm_min
    iso = lt.tm_wday + 1
    if a < b:
        return (iso in days) and (a <= minute < b)
    # 跨零点：23:30~24:00 归「开始那天」，00:00~07:00 归「前一天」
    if minute >= a:
        return iso in days
    if minute < b:
        prev_iso = iso - 1 if iso > 1 else 7
        return prev_iso in days
    return False


def window_label(w):
    """「23:30-07:00（每天）」这种人话，日志和面板共用。"""
    try:
        days = w.get("days") or list(ALL_WEEKDAYS)
        days = sorted(int(x) for x in days)
        if days == list(ALL_WEEKDAYS):
            dtxt = "每天"
        elif days == [1, 2, 3, 4, 5]:
            dtxt = "工作日"
        elif days == [6, 7]:
            dtxt = "周末"
        else:
            dtxt = "、".join(WEEKDAY_NAMES[d] for d in days if 0 < d < 8)
        note = str(w.get("note") or "").strip()
        base = "%s-%s" % (w.get("start"), w.get("end"))
        return base + ("（%s）" % ((dtxt + " " + note).strip() if note else dtxt))
    except Exception:
        return "%s-%s" % (w.get("start", "?"), w.get("end", "?"))
