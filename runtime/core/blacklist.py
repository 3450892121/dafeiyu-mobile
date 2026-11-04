"""Per-bot durable ignore policy. Raw history is preserved; model reads use views."""
import json
import time
import functools
import threading
import re

local = threading.local()


def policy_scope(fn):
    @functools.wraps(fn)
    def run(bot, *args, **kwargs):
        old = getattr(local, "policy", None)
        store = getattr(bot, "store", None)
        local.policy = old if old and old[0] is store else ((store, store.blacklist_generation()) if hasattr(store, "blacklist_generation") else None)
        try:
            return fn(bot, *args, **kwargs)
        finally:
            local.policy = old
    return run


def policy_changed(bot):
    policy = getattr(local, "policy", None)
    return bool(policy and policy[0] is bot.store and policy[1] != bot.store.blacklist_generation())


def write_allowed(store):
    policy = getattr(local, "policy", None)
    return not (policy and policy[0] is store and policy[1] != store.blacklist_generation())


def blocked(bot, uid):
    fn = getattr(getattr(bot, "store", None), "is_blocked", None)
    return bool(fn and fn(uid))


def generation(bot):
    policy = getattr(local, "policy", None)
    if policy and policy[0] is bot.store:
        return policy[1]
    fn = getattr(getattr(bot, "store", None), "blacklist_generation", None)
    return fn() if fn else "0"


def attachments_allowed(raw, ids):
    if not ids:
        return True
    denied = {int(x) for x in ids.split(",")}
    try:
        for item in json.loads(raw or "[]"):
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("quote", "reply"):
                if int(item.get("who_qq") or item.get("uid") or 0) in denied:
                    return False
            # Historical forward notes do not carry reliable author provenance.
            if item.get("type") == "forward":
                if not denied.issubset(set(item.get("ignored_users") or [])):
                    return False
        return True
    except (ValueError, TypeError):
        return False


def handle_command(bot, ev, uid, gid, is_group):
    """Owner controls are consumed before normal parsing, media work or memory."""
    segs = ev.get('message') or []
    text = ''.join(str((s.get('data') or {}).get('text') or '') for s in segs
                   if isinstance(s, dict) and s.get('type') == 'text').strip()
    match = re.fullmatch(r'(取消拉黑|解除拉黑|拉黑)\s*(.*?)\s*', text)
    if not match:
        return False
    mentions = []
    for seg in segs:
        if isinstance(seg, dict) and seg.get('type') == 'at':
            value = str((seg.get('data') or {}).get('qq') or '')
            if value.isascii() and value.isdigit():
                mentions.append(int(value))
    if is_group:
        if gid not in (bot.cfg.get('allow_groups') or []) or bot.self_id not in mentions:
            return False
    if uid != int(bot.cfg.get('owner_qq') or 0):
        return True
    from onebot import text_seg
    def answer(value):
        previous = getattr(local, 'policy', None)
        local.policy = (bot.store, bot.store.blacklist_generation())
        try:
            bot._send(is_group, gid, uid, [text_seg(value)])
        finally:
            local.policy = previous
        return True
    targets = set(mentions) - {int(bot.self_id or 0)}
    raw = match.group(2).strip()
    if not targets and raw:
        numeric = re.fullmatch(r'(?:QQ|qq)?\s*(?:号)?\s*([0-9]+)', raw)
        if numeric:
            targets.add(int(numeric.group(1)))
        else:
            # Management lookup intentionally sees ignored names, so nickname
            # based unblocking works without exposing those records to models.
            with bot.store._lock:
                rows = bot.store.conn.execute('SELECT qq,name,aliases FROM people').fetchall()
            for row in rows:
                try:
                    aliases = json.loads(row['aliases'] or '[]')
                except (ValueError, TypeError):
                    aliases = []
                if raw == row['name'] or raw in aliases:
                    targets.add(int(row['qq']))
    if len(targets) != 1:
        return answer('请 @一个人或写他的 QQ 号；昵称重名时请用 @ 或 QQ 号。')
    target = targets.pop()
    if not 0 < target < 2**63:
        return answer('这个 QQ 号无效，请核对后再说。')
    add = match.group(1) == '拉黑'
    if add and target in (uid, bot.self_id):
        return answer('聊天里不能把你自己或当前机器人拉黑。')
    try:
        bot.store.change_blacklist(target, add)
    except ValueError as error:
        return answer(str(error))
    return answer(('已拉黑 QQ%d，之后会忽略他的消息。' if add else '已取消拉黑 QQ%d，之后按正常权限接收他的消息。') % target)


class BlacklistStore:
    def init_blacklist(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS blocked_users (qq INTEGER PRIMARY KEY, added_at INTEGER NOT NULL)")
        self.conn.create_function("blacklist_attachments_allowed", 2, attachments_allowed, deterministic=True)
        self.conn.execute("CREATE TEMP VIEW blacklist_affected_convs AS SELECT DISTINCT conv FROM main.messages WHERE sender_id IN (SELECT qq FROM blocked_users) UNION SELECT 'p:'||qq FROM blocked_users")
        predicates = {
            "messages": "sender_id NOT IN (SELECT qq FROM blocked_users) AND conv NOT IN (SELECT 'p:'||qq FROM blocked_users) AND blacklist_attachments_allowed(attachments,(SELECT group_concat(qq) FROM blocked_users)) AND (direction<>'out' OR conv NOT IN (SELECT conv FROM blacklist_affected_convs) OR NOT EXISTS (SELECT 1 FROM blocked_users WHERE ts<=added_at))",
            "people": "qq NOT IN (SELECT qq FROM blocked_users)",
            "person_memories": "qq NOT IN (SELECT qq FROM blocked_users)",
            "person_cards": "conv NOT IN (SELECT 'q:'||qq FROM blocked_users UNION SELECT 'p:'||qq FROM blocked_users)",
            "group_memories": "('g:'||gid NOT IN (SELECT conv FROM blacklist_affected_convs) OR NOT EXISTS (SELECT 1 FROM blocked_users WHERE created<=added_at))",
            "shared_memories": "NOT EXISTS (SELECT 1 FROM json_each(people) j JOIN blocked_users b ON CAST(j.value AS INTEGER)=b.qq)",
            "notebook": "source_uid NOT IN (SELECT qq FROM blocked_users) AND (source_conv NOT IN (SELECT conv FROM blacklist_affected_convs) OR NOT EXISTS (SELECT 1 FROM blocked_users WHERE created<=added_at))",
            "memory_archive": "(src_table<>'person_memories' OR CAST(owner AS INTEGER) NOT IN (SELECT qq FROM blocked_users)) AND (src_table<>'group_memories' OR 'g:'||owner NOT IN (SELECT conv FROM blacklist_affected_convs) OR NOT EXISTS (SELECT 1 FROM blocked_users WHERE created<=added_at)) AND (src_table<>'shared_memories' OR NOT EXISTS (SELECT 1 FROM json_each(owner) j JOIN blocked_users b ON CAST(j.value AS INTEGER)=b.qq))",
            "memories": "person_id NOT IN (SELECT qq FROM blocked_users) AND (conv NOT IN (SELECT conv FROM blacklist_affected_convs) OR NOT EXISTS (SELECT 1 FROM blocked_users WHERE ts<=added_at))",
            "reminders": "target_uid NOT IN (SELECT qq FROM blocked_users) AND conv NOT IN (SELECT 'p:'||qq FROM blocked_users)",
        }
        for table, predicate in predicates.items():
            self.conn.execute("CREATE TEMP VIEW visible_%s AS SELECT * FROM main.%s WHERE %s" % (table, table, predicate))
        self.conn.commit()

    def is_blocked(self, uid):
        try:
            uid = int(uid or 0)
        except (TypeError, ValueError):
            return False
        if not 0 < uid < 2**63:
            return False
        with self._lock:
            return bool(self.conn.execute("SELECT 1 FROM blocked_users WHERE qq=?", (uid,)).fetchone())

    def blacklist_users(self):
        with self._lock:
            return [int(r[0]) for r in self.conn.execute("SELECT qq FROM blocked_users ORDER BY qq")]

    def blacklist_generation(self):
        return str(self.get_setting("blacklist_generation") or "0")

    def change_blacklist(self, uid, add):
        with self._lock, self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            users = set(self.blacklist_users())
            users.add(int(uid)) if add else users.discard(int(uid))
            return self.set_blacklist(sorted(users), _in_transaction=True)

    def set_blacklist(self, users, _in_transaction=False):
        if not isinstance(users, (list, tuple)) or len(users) > 1000:
            raise ValueError("黑名单最多 1000 人")
        clean = set()
        for value in users:
            text = str(value).strip()
            if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
                raise ValueError("请填写有效的 QQ 号，多个号码用逗号分隔")
            clean.add(int(text))
        with self._lock, self.conn:
            if not _in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            old = set(self.blacklist_users())
            if old == clean:
                return sorted(clean)
            self.conn.executemany("DELETE FROM blocked_users WHERE qq=?", [(u,) for u in old-clean])
            self.conn.executemany("INSERT INTO blocked_users VALUES (?,?)", [(u, int(time.time())) for u in clean-old])
            # Summaries and transient hints may contain untraceable quotations.
            self.conn.execute("UPDATE sessions SET summary=''")
            self.conn.execute("DELETE FROM settings WHERE k LIKE 'summary_upto:%' OR k LIKE 'recent_topic:%'")
            for row in self.conn.execute("SELECT conv FROM messages UNION SELECT conv FROM sessions").fetchall():
                key = 'recent_topic_generation:' + row[0]
                value = int(self.get_setting(key) or 0) + 1
                self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('blacklist_generation',?)", (str(int(self.blacklist_generation())+1),))
        return sorted(clean)
