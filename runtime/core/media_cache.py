# -*- coding: utf-8 -*-
r"""跨机器人共享的媒体缓存（2026-10-25 v0.47.2）

只存文字，不存图片/音频/向量：
- vision_cache: 图片 SHA256 + 提示词版本 -> 客观画面描述
- asr_cache: 音频 SHA256 -> 转写文字
- 一个 SQLite（WAL）供大肥鱼/缇宝/哦鲸鲸共用；任何失败都静默退回正常调用，不阻塞回复。

路径：优先环境变量 DFY_MEDIA_CACHE_DB；否则 Linux 用 /home/dafeiyu/DafeiyuBot/data/media_cache.db，
Windows 用 %LOCALAPPDATA%\DafeiyuBot\data\media_cache.db。
"""
import os, sqlite3, threading, time, hashlib, contextlib

_LOCK = threading.Lock()
_CONN = None
_DB = None
_INIT = False
_PRUNE_EVERY = 50
_PUTS = 0


def _db_path():
    env = os.environ.get("DFY_MEDIA_CACHE_DB")
    if env:
        return env
    base = os.path.join(os.environ['DAFEIYU_HOME'], 'data')
    return os.path.join(base, "media_cache.db")


def _conn():
    global _CONN, _DB, _INIT
    path = _db_path()
    with _LOCK:
        if _CONN is not None and _DB == path:
            return _CONN
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con = sqlite3.connect(path, timeout=5, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("""CREATE TABLE IF NOT EXISTS vision_cache(
                key TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'done', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS asr_cache(
                key TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_vision_created ON vision_cache(created_at)")
            con.commit()
            if os.name != "nt":
                try: os.chmod(path, 0o600)
                except Exception: pass
            _CONN, _DB = con, path
            return con
        except Exception:
            _CONN, _DB = None, path
            return None


def available():
    return _conn() is not None


def _prune(con):
    global _PUTS
    _PUTS += 1
    if _PUTS % _PRUNE_EVERY:
        return
    now = int(time.time())
    try:
        con.execute("DELETE FROM vision_cache WHERE created_at < ?", (now - 30 * 86400,))
        con.execute("DELETE FROM vision_cache WHERE key IN (SELECT key FROM vision_cache ORDER BY created_at DESC LIMIT -1 OFFSET 50000)")
        con.execute("DELETE FROM asr_cache WHERE created_at < ?", (now - 30 * 86400,))
        # 200MB 上限：超大时删最旧 10%
        try:
            pc = con.execute("PRAGMA page_count").fetchone()[0]
            ps = con.execute("PRAGMA page_size").fetchone()[0]
            if pc * ps > 200 * 1024 * 1024:
                con.execute("DELETE FROM vision_cache WHERE key IN (SELECT key FROM vision_cache ORDER BY created_at ASC LIMIT 5000)")
                con.execute("DELETE FROM asr_cache WHERE key IN (SELECT key FROM asr_cache ORDER BY created_at ASC LIMIT 5000)")
        except Exception:
            pass
        con.commit()
    except Exception:
        pass


def vision_key(data, version="vision-v1"):
    return hashlib.sha256((version + ":").encode() + (data or b"")).hexdigest()


def vision_get(key):
    if not key:
        return None
    try:
        con = _conn()
        if con is None:
            return None
        with _LOCK:
            row = con.execute("SELECT text, status, created_at FROM vision_cache WHERE key=?", (key,)).fetchone()
        if row and row[1] == "done" and row[0] and int(row[2]) >= int(time.time()) - 30 * 86400:
            return row[0]
    except Exception:
        pass
    return None


def vision_claim(key, stale=300):
    """抢到 processing 返回 True；已完成/别人刚抢到返回 False。"""
    if not key:
        return False
    try:
        con = _conn()
        if con is None:
            return False
        now = int(time.time())
        with _LOCK:
            cur = con.execute(
                "INSERT INTO vision_cache(key,text,model,status,created_at,updated_at) VALUES(?,'','', 'processing',?,?) "
                "ON CONFLICT(key) DO UPDATE SET status='processing', updated_at=excluded.updated_at "
                "WHERE vision_cache.status!='done' AND vision_cache.updated_at < ?",
                (key, now, now, now - stale))
            con.commit()
            return cur.rowcount > 0
    except Exception:
        return False


def vision_put(key, text, model=""):
    if not key or not text:
        return
    try:
        con = _conn()
        if con is None:
            return
        now = int(time.time())
        with _LOCK:
            con.execute("INSERT INTO vision_cache(key,text,model,status,created_at,updated_at) VALUES(?,?,?, 'done',?,?) "
                        "ON CONFLICT(key) DO UPDATE SET text=excluded.text, model=excluded.model, status='done', updated_at=excluded.updated_at",
                        (key, str(text)[:2000], str(model or "")[:80], now, now))
            _prune(con)
            con.commit()
    except Exception:
        pass


def asr_get(key):
    if not key:
        return None
    try:
        con = _conn()
        if con is None:
            return None
        with _LOCK:
            row = con.execute("SELECT text, created_at FROM asr_cache WHERE key=?", (key,)).fetchone()
        if row and row[0] and int(row[1]) >= int(time.time()) - 30 * 86400:
            return row[0]
    except Exception:
        pass
    return None


def asr_put(key, text, model=""):
    if not key or not text:
        return
    try:
        con = _conn()
        if con is None:
            return
        now = int(time.time())
        with _LOCK:
            con.execute("INSERT INTO asr_cache(key,text,model,created_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET text=excluded.text, model=excluded.model, created_at=excluded.created_at",
                        (key, str(text)[:1000], str(model or "")[:80], now))
            _prune(con)
            con.commit()
    except Exception:
        pass


def mem_available_mb():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


@contextlib.contextmanager
def asr_lock(timeout=30):
    """跨进程全局串行：同一时间只允许一个本地 ASR。拿不到锁就跳过，不排队到把回复拖死。"""
    if os.name == "nt":
        yield True
        return
    path = os.path.join(os.path.dirname(_db_path()), "asr.lock")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a+")
    except Exception:
        yield False
        return
    got = False
    try:
        import fcntl
        end = time.time() + max(1, int(timeout))
        while time.time() < end:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                time.sleep(0.2)
            except Exception:
                break
        yield got
    finally:
        try:
            if got:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
