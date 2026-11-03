"""Grounded overnight learning and scoped dream recall. Fiction is never evidence."""
import datetime as dt
import functools
import hashlib
import inspect
import json
import re
import threading
import time

local = threading.local()
IMAGINED = re.compile(r"梦见|梦到|梦里|梦中|梦境|做.{0,3}梦|假如|假设|小说|角色扮演|设定|引用上下文")
ASK_DREAM = re.compile(r"(?:梦见|梦到|做梦|梦里|梦境).{0,8}(?:什么|啥|谁|怎么样)|(?:什么|啥).{0,4}梦|(?:讲|说|聊).{0,5}(?:你的梦|那个梦)")
NO_DREAM = re.compile(r"(?:别|不要|不用|不想).{0,6}(?:梦|提起|旧事)")
LESSONS = {
    "concise": (r"(?:回复|回答|说话|说得|讲).{0,8}(?:短点|短一点|简短|简洁)|(?:别|不要).{0,5}(?:长篇|啰嗦)", "对方明确喜欢简短自然的回答，先回答要点。"),
    "no_followup": (r"(?:别|不要|不用|不需要).{0,6}(?:追问|反复问|一直问)", "对方明确不喜欢连续追问；信息足够就接话，不额外索取确认。"),
    "no_lists": (r"(?:别|不要|不用).{0,5}(?:列表|列点|分点)", "对方明确偏好自然段落；普通接话不机械列点。"),
}


def imagined(text, attachments=()):
    if IMAGINED.search(str(text or "")):
        return True
    try:
        atts = json.loads(attachments) if isinstance(attachments, str) else attachments
        return any(isinstance(a, dict) and a.get("type") in ("dream", "dream_context") for a in (atts or []))
    except (ValueError, TypeError):
        return True


def words(text):
    from util import tokenize
    return tokenize(str(text or ""))


def progress_state(text):
    if re.search(r"不是|并非|不对|纠正|说错|改成|更正", text):
        return "corrected"
    if re.search(r"取消|不去了|不考了|作废", text):
        return "cancelled"
    if re.search(r"没(?:有)?(?:通过|完成|搞定)|还没|未通过|没考过|不及格|失败", text):
        return "ongoing"
    if re.search(r"通过|考过了|完成了|搞定了|录取了|拿到了|解决了", text):
        return "completed"
    return "ongoing"


def reverses_lesson(lesson, text):
    if not re.search(r"以后|每次|一直|平时|记住", text):
        return False
    patterns = {"concise":r"(?:回复|回答|解释|说话).{0,10}(?:详细|展开|多说|长一点)|不喜欢.{0,5}(?:简短|简洁)",
                "no_followup":r"(?:可以|希望|请).{0,5}(?:多问我|追问)",
                "no_lists":r"(?:可以|希望|请).{0,5}(?:分点|列点|列表)"}
    return bool(lesson in patterns and re.search(patterns[lesson],text))


def validate_plan(parsed, sources):
    plans = parsed.get("consolidations", []) if isinstance(parsed, dict) else []
    if not isinstance(plans, list) or len(plans) > 6:
        raise ValueError("invalid sleep plan")
    allowed = {s["id"]: s for s in sources}
    result, seen = [], set()
    for plan in plans:
        if not isinstance(plan, dict):
            raise ValueError("invalid consolidation")
        ids, topic = plan.get("source_ids"), plan.get("topic")
        kind, lesson = plan.get("kind"), plan.get("lesson", "none")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8 or any(type(i) is not int or i not in allowed for i in ids)
                or len(set(ids)) != len(ids) or not isinstance(topic, str) or not 2 <= len(topic) <= 16
                or not re.fullmatch(r"[\w\u4e00-\u9fff ]+", topic) or topic in {"今天", "昨天", "主人", "你好", "谢谢", "事情", "聊天"}
                or kind not in ("progress", "preference", "commitment", "fact") or lesson not in ("none", *LESSONS)):
            raise ValueError("ungrounded consolidation")
        selected = sorted([allowed[i] for i in ids], key=lambda s: (s["ts"], s["id"]))
        if len({(s["conv"], s["sender_id"]) for s in selected}) != 1:
            raise ValueError("mixed privacy or speaker")
        if any(imagined(s["text"]) for s in selected) or not all(topic.lower() in (s["text"]+s.get("event_topic","")).lower() for s in selected):
            raise ValueError("unsupported topic")
        # A one-off formatting request is not a permanent preference.
        if lesson != "none":
            pattern = LESSONS[lesson][0]
            evidence = [s for s in selected if re.search(pattern, s["text"])]
            if (not evidence or (lesson=="concise" and re.search(r"(?:不喜欢|别|不要).{0,8}(?:简短|简洁|短点)", selected[-1]["text"]))
                    or not (re.search(r"以后|每次|一直|记住|长期|平时|都这样", selected[-1]["text"])
                            or len({dt.datetime.fromtimestamp(s["ts"], dt.timezone(dt.timedelta(hours=8))).date() for s in evidence}) >= 2)):
                lesson = "none"
        key = (selected[0]["conv"], selected[0]["sender_id"], topic.lower())
        if key in seen:
            raise ValueError("duplicate topic")
        seen.add(key)
        result.append({"scope": key[0], "subject": key[1], "topic": topic, "kind": kind,
                       "lesson": lesson, "sources": selected})
        if lesson != "none":
            result[-1]["kind"] = "preference"
    return result


class SleepMemoryStore:
    def _sleep_event(self, source):
        from store import _dec
        mid = source.get("message_id")
        if not mid:
            return {}
        row = self.conn.execute("SELECT id,topic,state,prior_id FROM interaction_events WHERE conv=? AND qq=? AND source_mid=?", (source["conv"],int(source["sender_id"]),str(mid))).fetchone()
        return {"event_id":row["id"],"event_topic":str(_dec(row["topic"]) or ""),"event_state":row["state"],"prior_id":row["prior_id"]} if row else {}

    def migrate_sleep_memory(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS sleep_memories (
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, subject INTEGER NOT NULL,
          topic TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
          body TEXT NOT NULL, sources TEXT NOT NULL, lesson TEXT DEFAULT 'none',
          version INTEGER NOT NULL, status TEXT DEFAULT 'active', created INTEGER NOT NULL,
          updated INTEGER NOT NULL, last_run_id INTEGER NOT NULL, UNIQUE(scope,subject,topic));
        CREATE TABLE IF NOT EXISTS sleep_memory_versions (
          memory_id INTEGER NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
          sources TEXT NOT NULL, run_id INTEGER NOT NULL, created INTEGER NOT NULL,
          reason TEXT NOT NULL, PRIMARY KEY(memory_id,version));
        CREATE TABLE IF NOT EXISTS sleep_commits (
          run_id INTEGER PRIMARY KEY, result TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sleep_watermarks (scope TEXT PRIMARY KEY, upto INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sleep_usage (
          id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL DEFAULT 0, run_id INTEGER NOT NULL DEFAULT 0,
          conv TEXT NOT NULL, source_mid TEXT NOT NULL, kind TEXT NOT NULL, ts INTEGER NOT NULL,
          UNIQUE(memory_id,run_id,conv,source_mid,kind));
        CREATE TABLE IF NOT EXISTS sleep_outbound (
          conv TEXT NOT NULL, message_id TEXT NOT NULL, run_id INTEGER NOT NULL,
          created INTEGER NOT NULL, PRIMARY KEY(conv,message_id));
        CREATE TABLE IF NOT EXISTS sleep_tainted (message_row INTEGER PRIMARY KEY, reason TEXT NOT NULL);
        """)
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(dream_entries)")}
        for key, declaration in (("wake_until", "INTEGER DEFAULT 0"), ("offered_at", "INTEGER DEFAULT 0"),
                                 ("mentioned_at", "INTEGER DEFAULT 0"), ("wake_hint", "TEXT DEFAULT ''")):
            if key not in existing:
                self.conn.execute("ALTER TABLE dream_entries ADD COLUMN " + key + " " + declaration)
        self.conn.commit()

    def sleep_tainted(self, row_id=0, conv="", source_mid="", text="", attachments=()):
        if imagined(text, attachments):
            return True
        with self._lock:
            if row_id and self.conn.execute("SELECT 1 FROM sleep_tainted WHERE message_row=?", (int(row_id),)).fetchone():
                return True
            if conv and source_mid:
                return bool(self.conn.execute("SELECT 1 FROM messages m JOIN sleep_tainted t ON t.message_row=m.id WHERE m.conv=? AND CAST(m.message_id AS TEXT)=?", (conv, str(source_mid))).fetchone())
        return False

    def sleep_attachments(self, conv, direction, text, raw, attachments):
        atts = list(attachments or [])
        state = getattr(local, "reply", None)
        tainted = imagined(text, atts)
        if direction == "out" and state and state.get("run_id"):
            atts.append({"type": "dream", "run_id": state["run_id"]})
            tainted = True
        for a in atts:
            if not isinstance(a, dict) or a.get("type") not in ("quote", "reply", "forward"):
                continue
            mid = str(a.get("id") or a.get("message_id") or "")
            if imagined(a.get("text") or a.get("note") or "") or (mid and self.conn.execute("SELECT 1 FROM sleep_outbound WHERE conv=? AND message_id=?", (conv, mid)).fetchone()):
                tainted = True
        if direction == "in" and tainted and not any(a.get("type") == "dream_context" for a in atts if isinstance(a, dict)):
            atts.append({"type": "dream_context"})
        return atts, tainted

    def sleep_material(self, now):
        from store import _dec
        from dreaming import fingerprint
        with self._lock:
            rows = self.conn.execute("SELECT id,ts,conv,sender_id,sender_name,message_id,text,attachments FROM visible_messages WHERE direction='in' AND ts>=? AND ts<=? ORDER BY id DESC LIMIT 600", (int(now)-7*86400, int(now))).fetchall()
            marks = {r[0]: r[1] for r in self.conn.execute("SELECT scope,upto FROM sleep_watermarks")}
            groups, seen = {}, set()
            for raw in rows:
                row = dict(raw)
                row["text"] = str(_dec(row["text"]) or "").strip()
                attachments = row.pop("attachments")
                if (not re.fullmatch(r"[pg]:[1-9]\d*", row["conv"] or "") or not int(row["sender_id"] or 0)
                        or (row["conv"].startswith("p:") and int(row["conv"][2:])!=int(row["sender_id"]))
                        or not 6 <= len(row["text"]) <= 600
                        or self.sleep_tainted(row["id"], text=row["text"], attachments=attachments)):
                    continue
                try:
                    if any(not isinstance(a, dict) or a.get("type") in ("quote", "reply", "forward") for a in json.loads(attachments or "[]")):
                        continue
                except (ValueError, TypeError):
                    continue
                key = (row["conv"], row["text"])
                if key in seen:
                    continue
                seen.add(key)
                row.update(self._sleep_event(row))
                row["hash"] = fingerprint(row)
                groups.setdefault(row["conv"], []).append(row)
        ranked = []
        for scope, group in groups.items():
            fresh = [r for r in group if r["id"] > marks.get(scope, 0) and r["ts"] >= now-86400]
            salience = lambda r: bool(re.search(r"考试|通过|结果|录取|约好|答应|说好|纠正|以后|每次|记住|不喜欢|别.*追问|喜欢", r["text"]))
            if not fresh or (len(fresh)<2 and not any(salience(r) for r in fresh)):
                continue
            fresh_first = sorted(fresh,key=lambda r:(r["ts"],r["id"]),reverse=True)[:4]
            used = {r["id"] for r in fresh_first}
            selected = (fresh_first + sorted([r for r in group if r["id"] not in used], key=lambda r: (salience(r),r["ts"]),reverse=True))[:10]
            # Keep an earlier phase as well as the newest result when possible.
            if len(group)>10:
                selected[-1] = group[-1]
            ranked.append((sum(salience(r) for r in fresh), max(r["ts"] for r in fresh), selected))
        selected = []
        for _, _, rows in sorted(ranked, key=lambda x: x[:2], reverse=True)[:4]:
            selected.extend(sorted(rows, key=lambda r: (r["ts"], r["id"])))
        return selected[:40]

    @staticmethod
    def _sleep_body(sources):
        lines = []
        for source in sorted(sources, key=lambda s: (s["ts"], s["id"])):
            day = dt.datetime.fromtimestamp(source["ts"], dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M")
            lines.append("%s %s(%s)原话：%s" % (day, source["sender_name"], source["sender_id"], source["text"]))
        return "\n".join(lines)

    def commit_sleep_review(self, run_id, plans, sources, generation, now):
        from store import _enc, _dec
        with self._lock, self.conn:
            run = self.conn.execute("SELECT status FROM dream_runs WHERE id=?", (run_id,)).fetchone()
            settings = self.dream_settings()
            if (not run or run[0] != "review" or not settings["enabled"] or settings["generation"] != generation
                    or not self.dream_sources_valid(sources)):
                return None
            prior = self.conn.execute("SELECT result FROM sleep_commits WHERE run_id=?", (run_id,)).fetchone()
            if prior:
                return json.loads(prior[0])
            result = {"added": 0, "updated": 0, "unchanged": 0, "memory_ids": []}
            for plan in plans:
                topic = hashlib.sha256(plan["topic"].lower().encode()).hexdigest()
                old = self.conn.execute("SELECT * FROM sleep_memories WHERE scope=? AND subject=? AND topic=?", (plan["scope"], plan["subject"], topic)).fetchone()
                combined = {s["id"]: s for s in (json.loads(_dec(old["sources"])) if old and old["status"]=="active" else [])}
                combined.update({s["id"]: s for s in plan["sources"]})
                ordered = sorted(combined.values(), key=lambda s: (s["ts"], s["id"]))
                # Refuse silent information loss; split/retention can be revisited explicitly.
                if len(ordered)>512 or not self.dream_sources_valid(ordered):
                    result["unchanged"] += 1
                    continue
                body = self._sleep_body(ordered)
                refs = json.dumps(ordered, ensure_ascii=False)
                lesson = plan["lesson"]
                if old and old["status"]=="active" and _dec(old["body"])==body and old["lesson"]==lesson:
                    result["unchanged"] += 1
                    continue
                version = int(old["version"])+1 if old else 1
                state = progress_state(ordered[-1]["text"]) if plan["kind"]=="progress" else "stable"
                if old:
                    mid = old["id"]
                    self.conn.execute("UPDATE sleep_memories SET kind=?,state=?,body=?,sources=?,lesson=?,version=?,status='active',updated=?,last_run_id=? WHERE id=?", (plan["kind"], state, _enc(body), _enc(refs), lesson, version, int(now), run_id, mid))
                    result["updated"] += 1
                else:
                    cur = self.conn.execute("INSERT INTO sleep_memories(scope,subject,topic,label,kind,state,body,sources,lesson,version,created,updated,last_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan["scope"],plan["subject"],topic,_enc(plan["topic"]),plan["kind"],state,_enc(body),_enc(refs),lesson,version,int(now),int(now),run_id))
                    mid = cur.lastrowid
                    result["added"] += 1
                self.conn.execute("INSERT INTO sleep_memory_versions VALUES(?,?,?,?,?,?,?)", (mid,version,_enc(body),_enc(refs),run_id,int(now),"add" if not old else "update"))
                result["memory_ids"].append(mid)
            for scope in {s["conv"] for s in sources}:
                upto = max(s["id"] for s in sources if s["conv"]==scope)
                self.conn.execute("INSERT INTO sleep_watermarks VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET upto=MAX(upto,excluded.upto)", (scope,upto))
            self.conn.execute("INSERT INTO sleep_commits VALUES(?,?,?)", (run_id,json.dumps(result),int(now)))
            return result

    def _withdraw_sleep_memory(self, mid):
        self.conn.execute("UPDATE sleep_memories SET status='withdrawn',body='',sources='[]',label='',lesson='none' WHERE id=?", (mid,))
        self.conn.execute("UPDATE sleep_memory_versions SET body='',sources='[]',reason='source_withdrawn' WHERE memory_id=?", (mid,))

    def sleep_maintenance(self):
        from store import _dec
        with self._lock, self.conn:
            for row in self.conn.execute("SELECT id,sources FROM sleep_memories WHERE status='active'").fetchall():
                if not self.dream_sources_valid(json.loads(_dec(row["sources"]))):
                    self._withdraw_sleep_memory(row["id"])
            self.conn.execute("DELETE FROM sleep_tainted WHERE message_row NOT IN (SELECT id FROM messages)")
            self.conn.execute("DELETE FROM sleep_outbound WHERE created<?", (int(time.time())-30*86400,))

    def sleep_context(self, conv, uid, text, now=None, all_for_query=False):
        from store import _dec
        now = int(now or time.time())
        if imagined(text):
            return "", []
        tokens, selected = words(text), []
        with self._lock, self.conn:
            rows = self.conn.execute("SELECT * FROM sleep_memories WHERE scope=? AND subject=? AND status='active' ORDER BY updated DESC LIMIT 60", (conv,int(uid))).fetchall()
            def priority(row):
                label = str(_dec(row["label"]) or "").lower()
                relevance = 3 if label in text.lower() else (2 if len(tokens & words(label))>=2 else (1 if row["lesson"]!="none" else 0))
                return relevance,row["updated"],row["id"]
            rows = sorted(rows,key=priority,reverse=True)
            for raw in rows:
                row = dict(raw)
                row["topic"] = str(_dec(row["label"]) or "")
                refs = json.loads(_dec(row["sources"]))
                if any(self.is_blocked(s.get("sender_id")) for s in refs):
                    continue
                related = row["topic"] in text.lower() or len(tokens & words(row["topic"])) >= 2
                if not self.dream_sources_valid(refs):
                    self._withdraw_sleep_memory(row["id"])
                    continue
                # Include newer direct evidence before the next nightly pass; never keep asserting an old result.
                newest = self.conn.execute("SELECT id,ts,sender_id,sender_name,message_id,text FROM messages WHERE conv=? AND sender_id=? AND direction='in' AND id>? ORDER BY id DESC LIMIT 16", (conv,int(uid),max(s["id"] for s in refs))).fetchall()
                for candidate in reversed(newest):
                    source = dict(candidate);source["conv"] = conv;source["text"] = str(_dec(source["text"]) or "")
                    source.update(self._sleep_event(source))
                    linked = source.get("prior_id") in {r.get("event_id") for r in refs if r.get("event_id")}
                    reversal = reverses_lesson(row["lesson"],source["text"])
                    if (row["topic"] in (source["text"]+source.get("event_topic","")).lower() or linked or reversal) and not self.sleep_tainted(source["id"], text=source["text"]):
                        refs.append(source)
                        if reversal:
                            row["lesson"] = "none"
                if not (related or row["lesson"] != "none" or all_for_query):
                    continue
                lesson = LESSONS.get(row["lesson"], ("", ""))[1]
                # Full source remains stored; context takes recent phases plus exact key facts from all phases.
                from store import guess_key_facts
                facts = list(dict.fromkeys(f for s in refs for f in guess_key_facts(s["text"])))
                excerpt = self._sleep_body(refs[-3:])
                if len(excerpt)>2000:
                    continue
                selected.append((row["id"], "主题：%s\n%s%s%s" % (row["topic"], excerpt, "\n关键事实："+"；".join(facts) if facts else "", "\n沟通偏好："+lesson if lesson else "")))
                if len(selected)>=2:
                    break
        if not selected:
            return "", []
        block = ("\n\n# 夜间回顾形成的有据记忆\n" + "\n".join(x[1] for x in selected)
                 + "\n这些来自真人原话，按时间理解，最后阶段优先；不是梦中发生的事。当前发言的纠正优先。已完成/取消的事不再当待办追问；不要编造原话没有的结论。沟通偏好只是本会话的倾向，当前要求优先。")
        return block, [x[0] for x in selected]

    def dream_recall(self, conv, uid, text, now=None):
        from store import _dec
        now = int(now or time.time())
        explicit = bool(ASK_DREAM.search(text))
        if NO_DREAM.search(text) or not self.dream_settings()["enabled"]:
            return "", 0
        with self._lock, self.conn:
            rows = self.conn.execute("SELECT e.*,r.sources FROM dream_entries e JOIN dream_runs r ON r.id=e.run_id WHERE e.scope=? AND r.status='complete' AND e.created>=? ORDER BY e.created DESC LIMIT 3", (conv,now-(7*86400 if explicit else 12*3600))).fetchall()
            for row in rows:
                sources = json.loads(_dec(row["sources"]))
                if any(self.is_blocked(s.get("sender_id")) for s in sources):
                    continue
                if not self.dream_sources_valid(sources):
                    continue
                if conv.startswith('p:') and any(int(s['sender_id'])!=int(uid) for s in sources):
                    continue
                body = _dec(row["text"])
                if not explicit:
                    overlap = words(text) & words(" ".join(s["text"] for s in sources))
                    if len(overlap)<2 or row["mentioned_at"] or (row["offered_at"] and now-row["offered_at"]<1800):
                        continue
                    if row["wake_until"] and row["wake_until"]<now:
                        continue
                self.conn.execute("UPDATE dream_entries SET offered_at=? WHERE id=?", (now,row["id"]))
                block = ("\n\n# 自己记得的一段梦（虚构，不能当现实证据）\n%s\n" % body
                         + ("对方正在问梦，可以用自己的口气讲这段已保存的梦，不另外编造真人经历。" if explicit else "这是醒后短暂残留的联想；只在贴合当下话题时顺口提一句，可以完全不提。")
                         + "提到情节时明确说是梦，别把梦里的失约/伤害当现实，不索取安慰，不据此生气或改关系；不要输出本段标题或程序说明。")
                if row["wake_hint"]:
                    block += "\n" + str(row["wake_hint"])
                return block, row["run_id"]
        return ("\n\n这次对方问梦，但此会话没有可用的梦境记录；自然说没记住，不能现编一场当成昨晚做过的。", 0) if explicit else ("", 0)

    def sleep_effects(self, run_id):
        from store import _dec
        row = self.conn.execute("SELECT result FROM sleep_commits WHERE run_id=?", (run_id,)).fetchone()
        result = json.loads(row[0]) if row else {"added":0,"updated":0,"unchanged":0,"memory_ids":[]}
        for key,kind in (("provided","provided"),("mentioned","mentioned")):
            result[key] = self.conn.execute("SELECT COUNT(*) FROM (SELECT conv,source_mid FROM sleep_usage WHERE run_id=? AND kind=? GROUP BY conv,source_mid)", (run_id,kind)).fetchone()[0]
        result["records"] = []
        for row in self.conn.execute("SELECT m.id,m.scope,m.label,m.state,m.status,v.body,v.version FROM sleep_memory_versions v JOIN sleep_memories m ON m.id=v.memory_id WHERE v.run_id=?", (run_id,)):
            result["records"].append({"id":row["id"],"scope":row["scope"],"topic":_dec(row["label"]),"state":row["state"],"status":row["status"],"body":_dec(row["body"]),"version":row["version"]})
        return result


def reply_scope(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        bound = signature.bind_partial(self,*args,**kwargs).arguments
        previous = getattr(local,"reply",None)
        local.reply = {"conv":bound.get("conv",""),"source_mid":str((bound.get("ev") or {}).get("message_id") or ""),"run_id":0,"memory_ids":[]}
        try:
            return fn(self,*args,**kwargs)
        finally:
            local.reply = previous
    return wrapped


def reply_context(bot, conv, uid, text, include_memory=True):
    block, mids = bot.store.sleep_context(conv,uid,text) if include_memory else ("", [])
    dream, run_id = bot.store.dream_recall(conv,uid,text)
    state = getattr(local,"reply",None)
    if state is not None:
        state.update(run_id=run_id,memory_ids=list(dict.fromkeys(state.get("memory_ids",[])+mids)))
        if run_id:
            from store import _dec
            with bot.store._lock:
                row = bot.store.conn.execute("SELECT text FROM dream_entries WHERE run_id=?",(run_id,)).fetchone()
            state["dream_body"] = _dec(row[0]) if row else ""
    return (block + dream + "\n\n你偶尔会记得自己的梦；只有提供了具体梦境记录时才讲细节，不凭空编昨晚的梦，也不主动介绍这套机制。")


def with_sleep_memory(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(self,*args,**kwargs):
        original = fn(self,*args,**kwargs)
        if not hasattr(self.store,"sleep_context"):
            return original
        bound = signature.bind_partial(self,*args,**kwargs).arguments
        uid, gid = int(bound.get("uid") or 0), int(bound.get("gid") or 0)
        conv = "g:%d"%gid if bound.get("is_group") else "p:%d"%uid
        block, mids = self.store.sleep_context(conv,uid,str(bound.get("text") or ""))
        state = getattr(local,"reply",None)
        if state is not None:
            state["memory_ids"] = list(dict.fromkeys(state.get("memory_ids",[])+mids))
        return (original or "") + block
    return wrapped


def guard_dream_reply(text):
    state = getattr(local,"reply",None)
    body = state.get("dream_body", "") if state else ""
    overlap = any(body[i:i+8] in str(text) for i in range(max(0,len(body)-7)))
    if state and state.get("run_id") and overlap and not re.search(r"梦|醒", str(text)):
        # If dream context was offered, prevent unlabelled fictional details from escaping.
        return "梦里倒是有这么一段：" + str(text)
    return text


def note_delivery(store, data):
    state = getattr(local,"reply",None)
    if not state or not state.get("run_id") or not isinstance(data,dict):
        return
    mid = str(data.get("message_id") or data.get("id") or "")
    if mid:
        with store._lock, store.conn:
            store.conn.execute("INSERT OR REPLACE INTO sleep_outbound VALUES(?,?,?,?)", (state["conv"],mid,state["run_id"],int(time.time())))


def finish_reply(store, text):
    from store import _dec
    state = getattr(local,"reply",None)
    if not state or not str(text or "").strip():
        return
    now = int(time.time())
    with store._lock, store.conn:
        for mid in state.get("memory_ids",[]):
            row = store.conn.execute("SELECT last_run_id,body FROM sleep_memories WHERE id=? AND status='active'", (mid,)).fetchone()
            if not row:
                continue
            for kind in ("provided",):
                store.conn.execute("INSERT OR IGNORE INTO sleep_usage(memory_id,run_id,conv,source_mid,kind,ts) VALUES(?,?,?,?,?,?)", (mid,row[0],state["conv"],state["source_mid"],kind,now))
            body = _dec(row[1])
            if any(body[i:i+8] in text for i in range(max(0,len(body)-7))):
                store.conn.execute("INSERT OR IGNORE INTO sleep_usage(memory_id,run_id,conv,source_mid,kind,ts) VALUES(?,?,?,?,?,?)", (mid,row[0],state["conv"],state["source_mid"],"mentioned",now))
        run_id = state.get("run_id")
        if run_id:
            store.conn.execute("INSERT OR IGNORE INTO sleep_usage(run_id,conv,source_mid,kind,ts) VALUES(?,?,?,?,?)", (run_id,state["conv"],state["source_mid"],"provided",now))
            body = state.get("dream_body", "")
            overlap = any(body[i:i+8] in text for i in range(max(0,len(body)-7)))
            if re.search(r"梦|醒",text) and overlap:
                store.conn.execute("UPDATE dream_entries SET mentioned_at=? WHERE run_id=?", (now,run_id))
                store.conn.execute("INSERT OR IGNORE INTO sleep_usage(run_id,conv,source_mid,kind,ts) VALUES(?,?,?,?,?)", (run_id,state["conv"],state["source_mid"],"mentioned",now))
