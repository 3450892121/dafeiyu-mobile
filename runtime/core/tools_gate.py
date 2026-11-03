# -*- coding: utf-8 -*-
"""工具权限 / 次数 / 待确认动作的统一闸门（v0.59.0）。

三只机器人共用同一套规则，各自写自己的库。
- 次数：窗口内每人每会话上限；主人不卡窗口，只按天提醒。
- 待确认：发消息 / 删知识库这类不可逆动作，先记在这里，主人回「确认」才执行。
"""
import json, time

# scope：owner=只有主人；all=白名单群里的人 + 私聊好友
# window=(秒, 次数)；daily=每人每天上限；owner_daily=主人每天提醒阈值（不拦）
LIMITS = {
    "web_search":       {"scope": "all",   "owner_daily": 200},
    "make_document":    {"scope": "all",   "window": (600, 5), "owner_daily": 50},
    "make_spreadsheet": {"scope": "all",   "window": (600, 5), "owner_daily": 50},
    "make_slides":      {"scope": "all",   "window": (600, 5), "owner_daily": 50},
    "read_history":     {"scope": "all"},
    "note_to_owner":    {"scope": "all",   "daily": 3},
    "read_link":        {"scope": "all",   "window": (600, 5), "daily": 30, "owner_daily": 100},
    "watch_video":      {"scope": "all",   "window": (600, 1), "daily": 5, "owner_daily": 20},
    "message_person":   {"scope": "owner"},
    "kb_search":        {"scope": "owner"},
    "kb_manage":        {"scope": "owner"},
    "memory_search":    {"scope": "all",   "window": (600, 20), "owner_daily": 200},
    "memory_dig":       {"scope": "all",   "window": (600, 8), "daily": 40, "owner_daily": 200},
    "read_diary":       {"scope": "all",   "window": (600, 8)},
    "notebook_write":   {"scope": "all",   "window": (600, 10), "daily": 30, "owner_daily": 200},
    "notebook_list":    {"scope": "all"},
    "notebook_delete":  {"scope": "all",   "window": (600, 10)},
    "draw_image":       {"scope": "owner", "owner_daily": 20},
}

_READY = {"v": False}


def ensure(conn):
    if _READY["v"]:
        return
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS tool_usage ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                     "conv TEXT NOT NULL, uid INTEGER NOT NULL, tool TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_usage ON tool_usage(tool, ts)")
        conn.execute("CREATE TABLE IF NOT EXISTS pending_actions ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, conv TEXT NOT NULL, "
                     "uid INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT DEFAULT '', "
                     "expires INTEGER NOT NULL, status TEXT DEFAULT 'pending')")
        conn.execute("CREATE TABLE IF NOT EXISTS owner_notes ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, conv TEXT NOT NULL, "
                     "name TEXT DEFAULT '', text TEXT DEFAULT '')")
        conn.commit()
        _READY["v"] = True
    except Exception:
        pass


def _count(conn, sql, args):
    try:
        r = conn.execute(sql, args).fetchone()
        return int(r[0] or 0) if r else 0
    except Exception:
        return 0


def check(store, tool, conv, uid, is_owner):
    """返回 (ok, 给模型看的一句话)。不通过时调用方把这句话交给模型自己润色。"""
    rule = LIMITS.get(tool) or {}
    if rule.get("scope") == "owner" and not is_owner:
        return False, "这个功能需要已确认的管理账号授权，当前请求没有这项权限。"
    ensure(store.conn)
    now = int(time.time())
    if not is_owner and rule.get("window"):
        win, cap = rule["window"]
        n = _count(store.conn, "select count(*) from tool_usage where tool=? and conv=? and uid=? and ts>?",
                   (tool, conv, int(uid), now - int(win)))
        if n >= int(cap):
            return False, "这会儿用得有点频繁，先停一停，过几分钟再来。"
    if rule.get("daily") and (not is_owner or rule.get("scope") == "owner"):
        n = _count(store.conn, "select count(*) from tool_usage where tool=? and uid=? and ts>?",
                   (tool, int(uid), now - 86400))
        if n >= int(rule["daily"]):
            return False, "今天的次数用完了，明天再来。"
    return True, ""


def note(store, tool, conv, uid):
    ensure(store.conn)
    try:
        store.conn.execute("insert into tool_usage(ts, conv, uid, tool) values(?,?,?,?)",
                           (int(time.time()), str(conv), int(uid), tool))
        store.conn.commit()
    except Exception:
        pass


def owner_daily(store, tool, uid):
    ensure(store.conn)
    return _count(store.conn, "select count(*) from tool_usage where tool=? and uid=? and ts>?",
                  (tool, int(uid), int(time.time()) - 86400))


def remind_needed(store, tool, uid):
    """主人当天超过阈值：返回 (要不要提醒, 已用次数, 阈值)，一天只提醒一次。"""
    rule = LIMITS.get(tool) or {}
    cap = int(rule.get("owner_daily") or 0)
    if not cap:
        return False, 0, 0
    n = owner_daily(store, tool, uid)
    if n < cap:
        return False, n, cap
    key = "tool_remind:%s:%s" % (tool, time.strftime("%Y%m%d"))
    try:
        if store.get_setting(key):
            return False, n, cap
        store.set_setting(key, "1")
    except Exception:
        return False, n, cap
    return True, n, cap


def save_owner_note(store, conv, name, text):
    ensure(store.conn)
    try:
        store.conn.execute("insert into owner_notes(ts, conv, name, text) values(?,?,?,?)",
                           (int(time.time()), str(conv), str(name or "")[:40], str(text or "")[:300]))
        store.conn.commit()
    except Exception:
        pass


def pending_add(store, conv, uid, kind, payload, ttl=60):
    ensure(store.conn)
    now = int(time.time())
    try:
        cur = store.conn.execute(
            "insert into pending_actions(ts, conv, uid, kind, payload, expires) values(?,?,?,?,?,?)",
            (now, str(conv), int(uid), str(kind), json.dumps(payload or {}, ensure_ascii=False), now + int(ttl)))
        store.conn.commit()
        return cur.lastrowid
    except Exception:
        return 0


def pending_take(store, conv, uid):
    """取最近一条还没过期的待确认动作；没有返回 None。"""
    ensure(store.conn)
    try:
        row = store.conn.execute(
            "select id,kind,payload,expires from pending_actions "
            "where conv=? and uid=? and status='pending' and expires>? order by id desc limit 1",
            (str(conv), int(uid), int(time.time()))).fetchone()
        if not row:
            return None
        return {"id": row[0], "kind": row[1], "payload": json.loads(row[2] or "{}"), "expires": row[3]}
    except Exception:
        return None


def pending_done(store, pid, status="done"):
    try:
        store.conn.execute("update pending_actions set status=? where id=?", (str(status), int(pid)))
        store.conn.commit()
    except Exception:
        pass
