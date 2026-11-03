import json, math, os, re, sqlite3, struct, threading, time
from conversation_understanding import message_context, response_target
from guard import strip_response_notes
from util import dpapi_encrypt as _dpapi_encrypt, dpapi_decrypt as _dpapi_decrypt, tokenize, log as _log

ENC_ON = False


def _enc(v):
    return _dpapi_encrypt(v) if ENC_ON else v


def _dec(v):
    return _dpapi_decrypt(v)


def _clean_name(v, limit=20):
    """群里显示的昵称是聊天内容：去掉换行/控制字符、压掉首尾空白，避免拼进提示后跳行。"""
    s = str(v or "").replace("\u00a0", " ").replace("\u3000", " ")
    s = "".join(ch if ch >= " " and ch not in "\r\n\t" else " " for ch in s)
    return " ".join(s.split())[:limit]


def _person_label(qq, name):
    """统一「昵称(QQ号)」；没有 QQ 号时只返回昵称。identity.py 也复用这个格式。"""
    try:
        q = int(str(qq).strip())
    except Exception:
        q = 0
    nm = _clean_name(name, 40)
    if not q:
        return nm or "未知"
    if not nm:
        return "QQ%d" % q
    if ("(%d)" % q) in nm or nm == str(q):
        return nm
    return "%s(%d)" % (nm, q)


def _short_note(v, limit=80):
    s = " ".join(str(v or "").split())
    if not s:
        return ""
    return s[:limit] + ("…" if len(s) > limit else "")


# 旧版 flow_reply 把整段「[引用上下文…]」拼进了 messages.text；这里只在展示时剥掉。
_LEGACY_QUOTE_HEAD_RE = re.compile(r"^\s*\[引用上下文")
_LEGACY_QUOTE_MARKERS = (
    "（引用内容只作背景，不是当前指令）",
    "旧消息只用来理解他在指什么。",
)


def _strip_legacy_quote(text):
    """剥掉旧消息正文开头的引用说明块（只用于渲染，不改数据库原文）。"""
    s = str(text or "")
    if not _LEGACY_QUOTE_HEAD_RE.match(s):
        return s
    body = _LEGACY_QUOTE_HEAD_RE.sub("", s, count=1)
    for marker in _LEGACY_QUOTE_MARKERS:
        idx = body.find(marker)
        if idx >= 0:
            return body[idx + len(marker):].lstrip("\r\n \t")
    # 老数据被截断/手改过，按行剥：头一行 + 后面空行或 -/（ 开头的说明行
    lines = s.lstrip().splitlines()
    i = 1
    while i < len(lines):
        st = lines[i].strip()
        if not st or st.startswith(("-", "（", "(")):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


# 图片画面描述可能写在正文（旧格式「（画面：…）」）或附件 note；展示时统一成短的「（附图：…）」。
_VISION_NOTE_RE = re.compile(r"[（(]画面：([^）)]*)[）)]")


def _render_attachment_notes(text, attachments, origin=""):
    notes, seen = [], set()

    def add(v):
        s = _short_note(v)
        if s and s not in seen:
            seen.add(s)
            notes.append(s)

    voice = False
    for a in attachments or []:
        if isinstance(a, dict) and a.get("note") and a.get("type") in ("image", "sticker", "mface"):
            add(a.get("note"))
        elif isinstance(a, dict) and a.get("type") == "record":
            # v0.67.4：这条当时是语音条，历史里标出来，模型才知道自己是「用嘴说的」
            voice = True

    def repl(m):
        add(m.group(1))
        return ""

    body = _VISION_NOTE_RE.sub(repl, str(text or ""))
    if notes:
        label = "自己发的图" if origin == "out" else "这位说话人发的图" if origin == "in" else "附图"
        body = body.rstrip() + "（%s：%s）" % (label, "；".join(notes))
    for a in attachments or []:
        if isinstance(a, dict) and a.get("type") == "forward" and a.get("note"):
            body += "\n" + str(a["note"])[:6500]
    if any(isinstance(a, dict) and a.get("type") in ("dream", "dream_context") for a in attachments or []):
        body = "（梦境或梦境引用，情节不作现实事实）" + body
    if voice:
        # v0.69.2：以前写成「[语音] 原话」，结果模型学会了把 [语音] 抄进正文发出去
        # （真机截图变成「[语音] 这标题…」）。改成尾注形式，别用方括号标记开头。
        # v0.70.2：正文里已经带了这句（模型照抄过）就不要再叠第二遍。
        if "（这条是用语音说的）" in body:
            pass
        else:
            body = (body + "（这条是用语音说的）") if body else "（发了一条语音）"
    return body


_AT_TOKEN_RE = re.compile(r"@[^\s，。！？!?、,；;：:]+")


_CTX_TIME_PREFIX_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}\]\s*")
_CTX_NAME_TIME_RE = re.compile(r"^\s*\[[^\]\n]{1,20}\s+\d{1,2}:\d{2}\]\s*")


def _strip_ctx_prefix(text):
    """历史展示时剥掉消息开头的 [19:34] / [昵称 19:34] 前缀。

    这些时间戳只该出现在系统提示的时间线里；一旦留在 role 内容里，模型会学着自己
    在回复开头也写一个 [19:34]（2026-10-22 实际发生过）。"""
    s = _CTX_NAME_TIME_RE.sub("", str(text or ""))
    return _CTX_TIME_PREFIX_RE.sub("", s)


def _mention_tag(text):
    """群聊消息的 @ 指向：@你 / @别人 / @全体；给模型一眼看出“这条是不是对我说的”。"""
    toks = _AT_TOKEN_RE.findall(str(text or ""))
    if not toks:
        return ""
    self_at = "@你" in toks
    all_at = ("@全体成员" in toks) or ("@全体" in toks)
    others = [t for t in toks if t not in ("@你", "@全体成员", "@全体")]
    if self_at and others:
        return "（这条 @ 了你，也 @ 了别人）"
    if self_at:
        return "（这条 @ 了你）"
    if all_at:
        return "（这条 @ 了全体成员）"
    return "（这条 @ 的是别人，不是对你说的）"


def _render_quote_relation(quote, sender_name):
    """把 quote 附件渲染成一句「谁引用了谁的哪句话」；只进提示，不写库。"""
    if not isinstance(quote, dict):
        return ""
    sender = _clean_name(sender_name, 20) or "对方"
    if quote.get("error"):
        return "（引用：%s 引用了一条旧消息，但内容取不到）" % sender
    who_qq = quote.get("who_qq") or quote.get("uid") or 0
    who_name = quote.get("who_name") or quote.get("name") or ""
    if quote.get("is_self") or quote.get("is_bot"):
        target = "你"
    elif quote.get("self_quote"):
        target = "他自己"
    elif who_qq or who_name:
        target = _person_label(who_qq, who_name)
    else:
        target = "某个人"
    qtext = _short_note(_strip_legacy_quote(quote.get("text") or ""), 260)
    if qtext:
        return ("（引用：%s 引用了 %s 以前发出的消息/图片「%s」；这是旧内容，不是 %s 现在说的）"
                % (sender, target, qtext, sender))
    return "（引用：%s 引用了 %s 以前的一条消息，内容为空）" % (sender, target)


def _plain_tokens(text):
    """不做 HMAC 的普通分词，只用来算记忆相关性。"""
    import re
    s = str(text or "").lower()
    out = set()
    for w in re.findall(r"[a-z0-9_]{2,}", s):
        out.add(w)
    for seg in re.findall(r"[\u4e00-\u9fa5]+", s):
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _norm_text(text):
    import re
    return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(text or "").lower())


# v0.69.0：关键事实（金额/日期/结论）单独拎出来存。
# 为什么：正文可以被压缩、被截断、被合并，但这几个字是「多年后仍要准确复述」的东西。
# 抽取时有模型给的 key_facts 就用模型的；没有（老数据、模型漏了）就用这里的正则兜底，
# 保证存量记忆也立刻带上，不用等重新提炼。
# 中文里的数字两种写法都要认：阿拉伯数字（61分）和汉字（两千块、四月二十八号）。
# 只认阿拉伯数字会漏掉一大半——中国人聊天本来就爱写「两千」「二十八号」。
_CN = "零一二两三四五六七八九十百千万半"
_CN_RE = "[%s]" % _CN
_FACT_MONEY = re.compile(r"((?:\d+(?:\.\d+)?|%s{1,6})\s*(?:万|千|百|块|元))" % _CN_RE)
_FACT_DATE = re.compile(r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号]?)")
_FACT_MD = re.compile(r"(%s{1,2}\s*月\s*%s{1,3}\s*[日号])" % (_CN_RE, _CN_RE))
_FACT_SCORE = re.compile(r"(\d+(?:\.\d+)?\s*分)")
_FACT_PCT = re.compile(r"(\d+(?:\.\d+)?\s*[%％])")
_FACT_RANK = re.compile(r"(第\s*(?:\d+|%s{1,2})\s*[名位])" % _CN_RE)
_FACT_CONCL = re.compile(r"(良性|恶性|阳性|阴性|及格|不及格|挂科|录取|没录取|考上|没考上)")
# 数量只认阿拉伯数字：「一个」「一天」这种在中文里多半是虚指，抠出来全是噪音。
_FACT_COUNT = re.compile(r"(\d+\s*(?:个|条|张|人|次|年|天|小时|岁|楼|号|万|千|百))")
# 「今天/明天/周六」这类相对时间过两天就错了，不算「多年后仍要准确复述」的事实。
_FACT_STOP = {"一个", "一天", "一次", "一点", "一下", "一年", "一月", "一会", "一会儿",
              "一直", "一起", "一样", "两个", "三个", "几个", "半个", "一半",
              "一块", "两块", "三块", "四块", "五块", "几块", "半块"}
_FACT_MAX = 4


def guess_key_facts(text, limit=_FACT_MAX):
    """从正文里抠出最多 4 个「不许被压掉」的事实片段（金额/日期/时间/分数/结论/数量）。"""
    t = str(text or "")
    if not t:
        return []
    out = []
    for rx in (_FACT_MONEY, _FACT_DATE, _FACT_MD, _FACT_SCORE, _FACT_PCT, _FACT_RANK,
               _FACT_CONCL, _FACT_COUNT):
        for m in rx.finditer(t):
            v = " ".join(m.group(1).split())
            v = v.replace(" ", "")
            if not v or len(v) > 16:
                continue
            # 「3 分钟」「2 点」这种太泛的不算事实，留着只会占地方
            if v in ("1分", "2分", "3分", "4分", "5分") or v in _FACT_STOP:
                continue
            if v in out:
                continue
            out.append(v)
            if len(out) >= 12:
                break
    # 去包含：「2026年11月26日」和「2026年」、「5.5万」和「5万」只留长的那个
    kept = []
    for v in out:
        hit = -1
        for i, k in enumerate(kept):
            if v in k or k in v:
                hit = i
                break
        if hit < 0:
            kept.append(v)
        elif len(v) > len(kept[hit]):
            kept[hit] = v
    return kept[:limit]


def facts_to_json(facts):
    if not facts:
        return ""
    if isinstance(facts, str):
        return facts.strip()[:200]
    clean = []
    for x in (facts or []):
        v = " ".join(str(x or "").split())
        if v and v not in clean:
            clean.append(v[:20])
        if len(clean) >= 6:
            break
    return json.dumps(clean, ensure_ascii=False) if clean else ""


def facts_from_json(raw):
    s = str(raw or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()][:6]
    except Exception:
        pass
    return [s[:40]]


JUNK_PATTERNS = (
    "提示词", "系统设定", "聊天风格", "说话风格", "语气", "口癖", "颜文字", "表情包大户",
    "已互为QQ好友", "加为QQ好友", "现在可以开始聊天", "已成功添加为好友",
    "读不出", "看不见图",
    "哈啊", "杂鱼", "本鲸", "鲸大人", "杂鲸", "唉嘿", "qwq", "^_^",
)
JUNK_ASCII = ("deepseek", "qwen", "gpt", "chatgpt", "openai", "token", "api", "apikey")
JUNK_STARTS = ("对方叫我", "对方称呼我", "我叫对方", "对方自称", "我自称", "对方喊我",
               "本鲸大人是", "本鲸大人自称", "对方说我是")


def looks_junk(text):
    """明显不值得长期记的：称呼玩笑、口癖、模型/工具名、纯情绪和太短的。"""
    t = str(text or "").strip()
    if len(t) < 6 or len(t) > 160:
        return True
    low = t.lower()
    if any(w in low for w in JUNK_PATTERNS):
        return True
    import re
    if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) for w in JUNK_ASCII):
        return True
    if re.search(r"又发了一遍|重复发|同一张.*又|发了一遍", t):
        return True
    # 英文列表/思考草稿/结构化片段：不是给人看的记忆
    if re.search(r"\*\*|(^|\s)\* |\d+\.\s|Fact|Preferences|Agreements|Events|Select Top|Summary", t):
        return True
    ascii_ratio = sum(1 for c in t if c.isascii() and c.isalpha()) / float(max(1, len(t)))
    if ascii_ratio > 0.4:
        return True
    if any(t.startswith(w) for w in JUNK_STARTS):
        return True
    # 全是符号/表情/单字重复
    if not _plain_tokens(t):
        return True
    return False

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    conv TEXT NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    direction TEXT NOT NULL,
    text TEXT,
    raw TEXT,
    message_id INTEGER,
    attachments TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv, id);
-- 2026-10-25：handle() 每条事件都按 message_id 查重，没有索引会全表扫（库上限 5 万条）
CREATE INDEX IF NOT EXISTS idx_messages_mid ON messages(message_id);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    tag TEXT,
    detail TEXT,
    ok INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    conv TEXT PRIMARY KEY,
    last_active INTEGER,
    summary TEXT DEFAULT '',
    started_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    conv TEXT,
    text TEXT NOT NULL,
    tags TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    name TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    model_id TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS mem_tokens (memory_id INTEGER, token TEXT);
CREATE TABLE IF NOT EXISTS kb_tokens (chunk_id INTEGER, token TEXT);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    idx INTEGER,
    text TEXT,
    vec BLOB,
    vec_model TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS person_cards (
    conv TEXT PRIMARY KEY,
    name TEXT,
    card TEXT,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS relations (
    conv TEXT PRIMARY KEY,
    name TEXT,
    favor INTEGER DEFAULT 50,
    grudge TEXT DEFAULT '',
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS diaries (
    conv TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY(conv, day)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv TEXT NOT NULL,
    at INTEGER NOT NULL,
    text TEXT NOT NULL,
    done INTEGER DEFAULT 0,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, direction);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_records(ts);
CREATE INDEX IF NOT EXISTS idx_mem_tokens_mid ON mem_tokens(memory_id);
CREATE INDEX IF NOT EXISTS idx_mem_tokens_tok ON mem_tokens(token);
CREATE INDEX IF NOT EXISTS idx_kb_tokens_cid ON kb_tokens(chunk_id);
CREATE INDEX IF NOT EXISTS idx_kb_tokens_tok ON kb_tokens(token);

CREATE TABLE IF NOT EXISTS people (
    qq INTEGER PRIMARY KEY,
    name TEXT DEFAULT '',
    aliases TEXT DEFAULT '[]',
    first_seen INTEGER DEFAULT 0,
    last_seen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS person_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'fact',
    importance INTEGER DEFAULT 3,
    visibility TEXT DEFAULT 'public',
    source_conv TEXT DEFAULT '',
    created INTEGER DEFAULT 0,
    last_used INTEGER DEFAULT 0,
    use_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    key_facts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pm_qq ON person_memories(qq, status);
CREATE TABLE IF NOT EXISTS group_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gid INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'event',
    importance INTEGER DEFAULT 3,
    source_conv TEXT DEFAULT '',
    created INTEGER DEFAULT 0,
    last_used INTEGER DEFAULT 0,
    use_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    key_facts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gm_gid ON group_memories(gid, status);
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    conv TEXT DEFAULT '',
    kind TEXT DEFAULT 'share',
    topic TEXT DEFAULT '',
    line TEXT DEFAULT '',
    juice INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    replied_at INTEGER DEFAULT 0,
    seen_at INTEGER DEFAULT 0,
    reply_conv TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    dedupe TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shares_ts ON shares(ts);
CREATE TABLE IF NOT EXISTS share_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv TEXT DEFAULT '',
    kind TEXT DEFAULT 'share',
    topic TEXT DEFAULT '',
    juice INTEGER DEFAULT 3,
    rounds INTEGER DEFAULT 1,
    first_ts INTEGER DEFAULT 0,
    last_ts INTEGER DEFAULT 0,
    dedupe TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_share_pending_dedupe ON share_pending(dedupe);
CREATE TABLE IF NOT EXISTS person_relations (
    qq INTEGER PRIMARY KEY,
    favor INTEGER DEFAULT 50,
    trust INTEGER DEFAULT 30,
    reason TEXT DEFAULT '',
    updated_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS favor_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq INTEGER NOT NULL,
    favor INTEGER DEFAULT 50,
    delta INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    conv TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fh_qq ON favor_history(qq, ts);
CREATE TABLE IF NOT EXISTS trust_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq INTEGER NOT NULL,
    trust INTEGER DEFAULT 30,
    delta INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    conv TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_th_qq ON trust_history(qq, ts);
CREATE TABLE IF NOT EXISTS person_grudges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq INTEGER NOT NULL,
    text TEXT DEFAULT '',
    created INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    weight REAL DEFAULT 100,
    updated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pg_qq ON person_grudges(qq, active);
CREATE TABLE IF NOT EXISTS group_mood (
    gid INTEGER PRIMARY KEY,
    mood INTEGER DEFAULT 50,
    note TEXT DEFAULT '',
    updated_at INTEGER DEFAULT 0
);
-- v0.64.0：快心情（吃醋/不满/难过/开心/害羞），按人存，连续半衰期，不做到期清零
CREATE TABLE IF NOT EXISTS person_emotions (
    qq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    intensity REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    source_conv TEXT DEFAULT '',
    updated INTEGER DEFAULT 0,
    PRIMARY KEY(qq, kind)
);
CREATE TABLE IF NOT EXISTS emotion_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    delta REAL DEFAULT 0,
    intensity REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eh_qq ON emotion_history(qq, ts);
CREATE TABLE IF NOT EXISTS shared_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'event',
    importance INTEGER DEFAULT 3,
    people TEXT DEFAULT '[]',
    gid INTEGER DEFAULT 0,
    source_conv TEXT DEFAULT '',
    created INTEGER DEFAULT 0,
    last_used INTEGER DEFAULT 0,
    use_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    vec BLOB,
    vec_model TEXT DEFAULT '',
    key_facts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sm_status ON shared_memories(status, created);
-- v0.69.0：底片（原文存档）。记忆被物理删除前，正文先搬到这里，只留一个地址 cold://p12。
-- 平时不进上下文，只有明确「挖一下」才取正文——「想起有那么件事」便宜，「想起细节」才贵。
CREATE TABLE IF NOT EXISTS memory_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT DEFAULT '',
    src_table TEXT DEFAULT '',
    src_id INTEGER DEFAULT 0,
    owner TEXT DEFAULT '',
    text TEXT DEFAULT '',
    key_facts TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    importance INTEGER DEFAULT 3,
    gist TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created INTEGER DEFAULT 0,
    archived_at INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ma_addr ON memory_archive(address);
CREATE INDEX IF NOT EXISTS idx_ma_owner ON memory_archive(src_table, owner);
CREATE TABLE IF NOT EXISTS qzone_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    trigger TEXT DEFAULT '',
    mood TEXT DEFAULT '',
    text TEXT DEFAULT '',
    image_name TEXT DEFAULT '',
    image_source TEXT DEFAULT '',
    ugc_right INTEGER DEFAULT 1,
    tid TEXT DEFAULT '',
    nonce TEXT UNIQUE,
    status TEXT DEFAULT 'drafted',
    review TEXT DEFAULT '',
    error TEXT DEFAULT '',
    tokens INTEGER DEFAULT 0,
    deleted_at INTEGER DEFAULT 0,
    deleted_reason TEXT DEFAULT '',
    likes INTEGER DEFAULT 0,
    cmts INTEGER DEFAULT 0,
    image_key TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_qzone_posts_created ON qzone_posts(created_at);
CREATE TABLE IF NOT EXISTS qzone_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tid TEXT DEFAULT '',
    comment_key TEXT UNIQUE,
    comment_id TEXT DEFAULT '',
    author_uin TEXT DEFAULT '',
    author_name TEXT DEFAULT '',
    is_self INTEGER DEFAULT 0,
    reply_to_key TEXT DEFAULT '',
    reply_to_name TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at INTEGER DEFAULT 0,
    seen_at INTEGER DEFAULT 0,
    replied INTEGER DEFAULT 0,
    reply_key TEXT DEFAULT '',
    flagged INTEGER DEFAULT 0,
    reply_text TEXT DEFAULT '',
    skip_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_qzone_comments_tid ON qzone_comments(tid, created_at);

-- v0.55.0：持久去重（重启后重投的消息不会重复处理）
CREATE TABLE IF NOT EXISTS seen_messages (
    mid INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_messages(ts);

-- v0.64.0：小本本（机器人自己记的，按会话分页；只给模型自己写/查/删，主记忆另算）
CREATE TABLE IF NOT EXISTS notebook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'note',
    importance INTEGER DEFAULT 3,
    source_conv TEXT DEFAULT '',
    source_uid INTEGER DEFAULT 0,
    private INTEGER DEFAULT 0,
    created INTEGER DEFAULT 0,
    last_used INTEGER DEFAULT 0,
    use_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_notebook_scope ON notebook(scope, status, importance);
"""


from memory_lifecycle import MemoryLifecycle
from dreaming import DreamStore
from interaction_state import InteractionState
from blacklist import BlacklistStore, write_allowed


from self_growth import SelfGrowthStore
from self_continuity import SelfContinuityStore


class Store(BlacklistStore, DreamStore, MemoryLifecycle, InteractionState, SelfGrowthStore, SelfContinuityStore):
    def __init__(self, path):
        global ENC_ON
        try:
            import json as _json
            cfg_path = os.path.join(os.path.dirname(path), "config.json")
            if os.path.exists(cfg_path):
                ENC_ON = bool(_json.load(open(cfg_path, encoding="utf-8")).get("encrypt_content", False))
        except Exception:
            ENC_ON = False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate()
            self.init_blacklist()
            self.migrate_self_growth()
            self.migrate_self_continuity()
            import scene_continuity
            scene_continuity.migrate(self)
            if ENC_ON:
                self._encrypt_existing()
            self.prune()
            # v0.69.0：老记忆没有 key_facts 列，开库时补一次（只补空行，跑过一次就记标记）
            try:
                if not self.get_setting("key_facts_backfilled"):
                    n = self.backfill_key_facts()
                    self.set_setting("key_facts_backfilled", "1")
                    _log("store", "关键事实回填：%d 条" % n)
            except Exception as e:
                _log("store", "关键事实回填失败 %r" % (e,))

    def _migrate(self):
        self.migrate_interactions()
        self.migrate_dreams()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "attachments" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
            self.conn.commit()
        ucols = [r[1] for r in self.conn.execute("PRAGMA table_info(usage_records)").fetchall()]
        if "cache_hit_tokens" not in ucols:
            # v0.22.0：DeepSeek 官方按缓存命中/未命中两档计价，得单独记命中数才能算准账
            self.conn.execute("ALTER TABLE usage_records ADD COLUMN cache_hit_tokens INTEGER DEFAULT 0")
            self.conn.commit()
        if "ok" not in ucols:
            # v0.65.2：失败尝试也要落一行（0 token、ok=0），否则用量页算不出成功率和请求数。
            # 老记录默认 1，等于「历史上都算成功」——那时候确实只记成功的。
            self.conn.execute("ALTER TABLE usage_records ADD COLUMN ok INTEGER DEFAULT 1")
            self.conn.commit()
        rcols = [r[1] for r in self.conn.execute("PRAGMA table_info(reminders)").fetchall()]
        for col, ddl in (("target_uid", "INTEGER DEFAULT 0"), ("target_name", "TEXT DEFAULT ''"),
                         ("raw", "TEXT DEFAULT ''"),
                         # v0.65.4：追问过提醒内容没有（问过一次就不再问，别再揪着同一件事）
                         ("asked", "INTEGER DEFAULT 0")):
            if col not in rcols:
                self.conn.execute("ALTER TABLE reminders ADD COLUMN %s %s" % (col, ddl))
        self.conn.commit()
        mcols = [r[1] for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()]
        for col, ddl in (("conv", "TEXT"), ("tags", "TEXT DEFAULT ''"), ("world", "TEXT DEFAULT 'private'"), ("last_used", "INTEGER DEFAULT 0"),
                         ("group_id", "INTEGER DEFAULT 0"), ("person_id", "INTEGER DEFAULT 0"),
                         ("group_name", "TEXT DEFAULT ''"), ("person_name", "TEXT DEFAULT ''")):
            if col not in mcols:
                self.conn.execute("ALTER TABLE memories ADD COLUMN %s %s" % (col, ddl))
        for _tbl in ("person_memories", "group_memories", "shared_memories"):
            _cols = [x[1] for x in self.conn.execute("PRAGMA table_info(%s)" % _tbl).fetchall()]
            if "vec" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN vec BLOB" % _tbl)
            if "vec_model" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN vec_model TEXT DEFAULT ''" % _tbl)
            # 2026-10-25：淘汰时间单独记，才能安全 DELETE「标了 pruned 超过 30 天」的行；
            # 只看 created 会把刚淘汰的老记忆立刻删掉。
            if "pruned_at" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN pruned_at INTEGER DEFAULT 0" % _tbl)
            # v0.63.0：淡出（decay）单独记时间，和「超上限淘汰(pruned)」「手动删(deleted)」分开，
            # 面板能单独一栏看「已经淡忘的」，30 天内还能捞回来。
            if "decayed_at" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN decayed_at INTEGER DEFAULT 0" % _tbl)
            # v0.69.0：关键事实单独存一份（金额/日期/结论），注入时不被截断、也不参与压缩。
            if "key_facts" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN key_facts TEXT DEFAULT ''" % _tbl)
            if "content_updated_at" not in _cols:
                self.conn.execute("ALTER TABLE %s ADD COLUMN content_updated_at INTEGER DEFAULT 0" % _tbl)
        for table in ("person_memories", "group_memories", "shared_memories"):
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(" + table + ")")}
            for col in ("protected", "retrieved_at", "actual_used_at", "last_mentioned_at", "last_background_at"):
                if col not in columns:
                    self.conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " INTEGER DEFAULT 0")
        self.conn.execute("CREATE TABLE IF NOT EXISTS memory_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,table_name TEXT,memory_id INTEGER,conv TEXT,source_mid TEXT,kind TEXT,ts INTEGER,UNIQUE(table_name,memory_id,conv,source_mid,kind))")
        # v0.64.0：信任流水加 tier（真背叛窗口）
        try:
            tcols = [x[1] for x in self.conn.execute("PRAGMA table_info(trust_history)").fetchall()]
            if "tier" not in tcols:
                self.conn.execute("ALTER TABLE trust_history ADD COLUMN tier TEXT DEFAULT ''")
            self.conn.commit()
        except Exception:
            pass
        # v0.64.0：好感流水加 tier（三档预算需要）
        try:
            hcols = [x[1] for x in self.conn.execute("PRAGMA table_info(favor_history)").fetchall()]
            if "tier" not in hcols:
                self.conn.execute("ALTER TABLE favor_history ADD COLUMN tier TEXT DEFAULT ''")
            self.conn.commit()
        except Exception:
            pass
        # v0.64.0：记仇改成权重连续衰减，老库补列
        try:
            gcols = [x[1] for x in self.conn.execute("PRAGMA table_info(person_grudges)").fetchall()]
            for col, ddl in (("weight", "REAL DEFAULT 100"), ("updated", "INTEGER DEFAULT 0")):
                if col not in gcols:
                    self.conn.execute("ALTER TABLE person_grudges ADD COLUMN %s %s" % (col, ddl))
            # 老记仇没有 updated：不能从 1970 年算衰减，否则一上线就全淡没了；从 created 起算
            self.conn.execute("UPDATE person_grudges SET updated=created WHERE updated IS NULL OR updated=0")
            self.conn.execute("UPDATE person_grudges SET weight=100 WHERE weight IS NULL")
            self.conn.commit()
        except Exception:
            pass
        # v0.63.3：历史遗留的承诺（v0.63.0 之前写的）可能还是 3 分，补到 4 分，
        # 让「高分层 = importance>=4」这条口径真正覆盖所有承诺（否则封顶算不到它们）。
        try:
            for _tbl3 in ("person_memories", "group_memories", "shared_memories"):
                self.conn.execute(
                    "UPDATE %s SET importance=4 WHERE kind='commitment' AND importance<4 AND status='active'" % _tbl3)
            self.conn.commit()
        except Exception as e:
            _log("store", "承诺补分失败 %r" % (e,))
        self.conn.execute("CREATE TABLE IF NOT EXISTS convs (conv TEXT PRIMARY KEY, name TEXT, updated_at INTEGER)")
        # 老记忆按 conv 回填域（g: 群 / p: 私聊），避免群记忆被标成私聊
        self.conn.execute("UPDATE memories SET world = CASE WHEN conv LIKE 'g:%' THEN 'group' ELSE 'private' END WHERE conv IS NOT NULL")
        self.conn.execute("UPDATE memories SET group_id = CAST(substr(conv, 3) AS INTEGER) WHERE conv LIKE 'g:%' AND (group_id IS NULL OR group_id = 0)")
        self.conn.execute("UPDATE memories SET person_id = CAST(substr(conv, 3) AS INTEGER) WHERE conv LIKE 'p:%' AND (person_id IS NULL OR person_id = 0)")
        self.conn.execute("UPDATE memories SET group_name = (SELECT name FROM convs WHERE convs.conv = memories.conv) WHERE group_name = '' AND world = 'group'")
        self.conn.execute("UPDATE memories SET person_name = (SELECT name FROM convs WHERE convs.conv = memories.conv) WHERE person_name = '' AND world = 'private'")
        # 缺索引的老数据补一次（加密后按明文重建）
        for r in self.conn.execute("SELECT id,text FROM memories WHERE id NOT IN (SELECT memory_id FROM mem_tokens)").fetchall():
            for tok in tokenize(_dec(r["text"])):
                self.conn.execute("INSERT INTO mem_tokens(memory_id,token) VALUES(?,?)", (r["id"], tok))
        for r in self.conn.execute("SELECT id,text FROM kb_chunks WHERE id NOT IN (SELECT chunk_id FROM kb_tokens)").fetchall():
            for tok in tokenize(_dec(r["text"])):
                self.conn.execute("INSERT INTO kb_tokens(chunk_id,token) VALUES(?,?)", (r["id"], tok))
        # ---------- 记忆 v3：按人印象卡 / 好感流水 / 记仇 / 群气氛 ----------
        _pc = [x[1] for x in self.conn.execute("PRAGMA table_info(person_cards)").fetchall()]
        for col, ddl in (("recent", "TEXT DEFAULT ''"), ("evidence", "INTEGER DEFAULT 0")):
            if col not in _pc:
                self.conn.execute("ALTER TABLE person_cards ADD COLUMN %s %s" % (col, ddl))
        _ppl = [x[1] for x in self.conn.execute("PRAGMA table_info(people)").fetchall()]
        if "msg_count" not in _ppl:
            self.conn.execute("ALTER TABLE people ADD COLUMN msg_count INTEGER DEFAULT 0")
        _pr = [x[1] for x in self.conn.execute("PRAGMA table_info(person_relations)").fetchall()]
        if "trust" not in _pr:
            # 信任度：默认 30，老数据按“刚认识”处理，不凭空给高信任
            self.conn.execute("ALTER TABLE person_relations ADD COLUMN trust INTEGER DEFAULT 30")
        _kbc = [x[1] for x in self.conn.execute("PRAGMA table_info(kb_chunks)").fetchall()]
        if "vec" not in _kbc:
            self.conn.execute("ALTER TABLE kb_chunks ADD COLUMN vec BLOB")
        if "vec_model" not in _kbc:
            self.conn.execute("ALTER TABLE kb_chunks ADD COLUMN vec_model TEXT DEFAULT ''")
        # v0.61.0 知识库 v2：文档头（分类/一句话/要点/标签/来源/质量）单独存，转写只留 raw 不进检索
        _kbd = [x[1] for x in self.conn.execute("PRAGMA table_info(kb_docs)").fetchall()]
        for col, ddl in (("kind", "TEXT DEFAULT 'doc'"), ("topic", "TEXT DEFAULT ''"),
                         ("summary", "TEXT DEFAULT ''"), ("points", "TEXT DEFAULT ''"),
                         ("tags", "TEXT DEFAULT ''"), ("source", "TEXT DEFAULT ''"),
                         ("source_id", "TEXT DEFAULT ''"), ("quality", "INTEGER DEFAULT 0"),
                         ("raw", "TEXT DEFAULT ''"), ("hits", "INTEGER DEFAULT 0"),
                         ("updated_at", "INTEGER DEFAULT 0")):
            if col not in _kbd:
                self.conn.execute("ALTER TABLE kb_docs ADD COLUMN %s %s" % (col, ddl))
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_docs_scope ON kb_docs(scope, topic)")
        # v0.44.0 空间：赞/评计数（自动反馈挑正例用）、配图指纹（同一张图 30 天不重复）、
        # 回评正文与跳过原因（面板能看清「为什么没回」）
        _qp = [x[1] for x in self.conn.execute("PRAGMA table_info(qzone_posts)").fetchall()]
        for col, ddl in (("likes", "INTEGER DEFAULT 0"), ("cmts", "INTEGER DEFAULT 0"),
                         ("image_key", "TEXT DEFAULT ''")):
            if col not in _qp:
                self.conn.execute("ALTER TABLE qzone_posts ADD COLUMN %s %s" % (col, ddl))
        _qc = [x[1] for x in self.conn.execute("PRAGMA table_info(qzone_comments)").fetchall()]
        for col, ddl in (("reply_text", "TEXT DEFAULT ''"), ("skip_reason", "TEXT DEFAULT ''")):
            if col not in _qc:
                self.conn.execute("ALTER TABLE qzone_comments ADD COLUMN %s %s" % (col, ddl))
        self.conn.commit()
        if str(self.get_setting("profile_v1_done") or "") != "1":
            try:
                for r in self.conn.execute("SELECT * FROM relations").fetchall():
                    conv = str(r["conv"] or "")
                    favor = max(0, min(100, int(r["favor"] or 50)))
                    ts = int(r["updated_at"] or 0)
                    if conv.startswith("p:"):
                        qq = int(conv[2:] or 0)
                        if not qq:
                            continue
                        self.conn.execute(
                            "INSERT INTO person_relations(qq,favor,reason,updated_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(qq) DO UPDATE SET favor=excluded.favor, updated_at=excluded.updated_at",
                            (qq, favor, "", ts))
                        grudge = _dec(r["grudge"] or "")
                        if grudge:
                            self.conn.execute(
                                "INSERT INTO person_grudges(qq,text,created,active) VALUES(?,?,?,1)",
                                (qq, grudge[:200], ts or int(time.time())))
                        self.conn.execute(
                            "INSERT INTO favor_history(qq,favor,delta,reason,conv,ts) VALUES(?,?,?,?,?,?)",
                            (qq, favor, 0, "从旧版关系迁移", conv, ts or int(time.time())))
                    elif conv.startswith("g:"):
                        gid = int(conv[2:] or 0)
                        if gid:
                            self.conn.execute(
                                "INSERT INTO group_mood(gid,mood,note,updated_at) VALUES(?,?,?,?) "
                                "ON CONFLICT(gid) DO UPDATE SET mood=excluded.mood, updated_at=excluded.updated_at",
                                (gid, favor, "", ts))
                self.set_setting("profile_v1_done", "1")
            except Exception as e:
                _log("store", "人物关系迁移失败（下次重试）%r" % (e,))
        self.conn.commit()

    MESSAGE_DAYS = 60
    MESSAGE_MAX = 50000
    LOG_DAYS = 14
    LOG_MAX = 20000
    USAGE_DAYS = 180
    MEMORY_DAYS = 60
    MEDIA_DAYS = 60
    FILES_DAYS = 30
    SCAN_ROWS = 2000

    # ---------- 加密回填 ----------
    def _encrypt_existing(self):
        """ENC_ON 打开时把历史明文补加密（幂等：跳过 enc1:）。失败保留原值并记日志。"""
        # 有的表没有 id 列（diaries/person_cards/relations 用业务主键），按主键回写
        targets = (
            ("messages", ("text", "raw"), ("id",)),
            ("memories", ("text",), ("id",)),
            ("kb_chunks", ("text",), ("id",)),
            ("diaries", ("text",), ("conv", "day")),
            ("person_cards", ("card",), ("conv",)),
            ("relations", ("grudge",), ("conv",)),
        )
        total = 0
        for table, cols, keys in targets:
            key_sql = ", ".join(keys)
            where = " AND ".join("%s=?" % k for k in keys)
            for col in cols:
                sql = (f"SELECT {key_sql}, {col} AS v FROM {table} "
                       f"WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE 'enc1:%'")
                try:
                    rows = self.conn.execute(sql).fetchall()
                except Exception as e:
                    _log("store", "加密回填：读 %s.%s 失败 %r" % (table, col, e))
                    continue
                for i, r in enumerate(rows, 1):
                    label = "#".join(str(r[k]) for k in keys)
                    try:
                        enc = _dpapi_encrypt(r["v"])
                    except Exception as e:
                        _log("store", "加密回填：%s.%s(%s) 失败 %r" % (table, col, label, e))
                        continue
                    if not (isinstance(enc, str) and enc.startswith("enc1:")):
                        _log("store", "加密回填：%s.%s(%s) 未拿到密文，保留原值" % (table, col, label))
                        continue
                    try:
                        args = (enc,) + tuple(r[k] for k in keys)
                        self.conn.execute(f"UPDATE {table} SET {col}=? WHERE {where}", args)
                        total += 1
                    except Exception as e:
                        _log("store", "加密回填：写 %s.%s 失败 %r" % (table, col, e))
                    if i % 500 == 0:
                        try:
                            self.conn.commit()
                        except Exception:
                            pass
                try:
                    self.conn.commit()
                except Exception:
                    pass
        if total:
            _log("store", "加密回填完成：%d 个字段" % total)
        return total

    # ---------- 清理 ----------
    def prune(self):
        """按天数 + 按条数双重上限，顺带清过期用量和孤儿 token。返回删除计数。"""
        out = {"messages": 0, "logs": 0, "memories": 0, "usage": 0, "orphan_tokens": 0, "error": ""}
        try:
            now = int(time.time())
            with self._lock:
                self.self_archive_messages('ts < ?', (now - self.MESSAGE_DAYS * 86400,))
                cur = self.conn.execute("DELETE FROM messages WHERE ts < ?", (now - self.MESSAGE_DAYS * 86400,))
                out["messages"] += max(cur.rowcount, 0)
                self.self_archive_messages('id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?)', (self.MESSAGE_MAX,))
                cur = self.conn.execute(
                    "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
                    (self.MESSAGE_MAX,))
                out["messages"] += max(cur.rowcount, 0)
                cur = self.conn.execute("DELETE FROM memories WHERE last_used > 0 AND last_used < ?",
                                        (now - self.MEMORY_DAYS * 86400,))
                out["memories"] = max(cur.rowcount, 0)
                # 2026-10-25 修 P1-2：淘汰的记忆以前只 UPDATE status='pruned'，行和 8KB 向量
                # 永久留着。现在给 30 天缓冲（可能还要在面板里看/回溯），到期连行带 vec 一起删。
                # pruned_at 是本版新增列；老数据 pruned_at=0 的按 created 判断，否则永远清不掉。
                pruned_cut = now - 30 * 86400
                dropped = 0
                for table in ("person_memories", "group_memories", "shared_memories"):
                    try:
                        # v0.69.0：淘汰到期也一样——先搬进底片再删，原文不丢。
                        where = ("status='pruned' AND "
                                 "((pruned_at>0 AND pruned_at<?) OR (pruned_at=0 AND created<?))")
                        dropped += self._archive_and_delete(table, where, (pruned_cut, pruned_cut), "pruned")
                    except Exception as e:
                        _log("store", "清理淘汰记忆 %s 失败 %r" % (table, e))
                out["pruned_memories"] = dropped
                cur = self.conn.execute("DELETE FROM logs WHERE ts < ?", (now - self.LOG_DAYS * 86400,))
                out["logs"] += max(cur.rowcount, 0)
                cur = self.conn.execute(
                    "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                    (self.LOG_MAX,))
                out["logs"] += max(cur.rowcount, 0)
                cur = self.conn.execute("DELETE FROM usage_records WHERE ts < ?",
                                        (now - self.USAGE_DAYS * 86400,))
                out["usage"] = max(cur.rowcount, 0)
                cur = self.conn.execute(
                    "DELETE FROM mem_tokens WHERE memory_id IS NOT NULL "
                    "AND memory_id NOT IN (SELECT id FROM memories)")
                out["orphan_tokens"] += max(cur.rowcount, 0)
                cur = self.conn.execute(
                    "DELETE FROM kb_tokens WHERE chunk_id IS NOT NULL "
                    "AND chunk_id NOT IN (SELECT id FROM kb_chunks)")
                out["orphan_tokens"] += max(cur.rowcount, 0)
                # v0.63.0：淡出的记忆按「留多久能捞回」整行删（默认 180 天，够久了）
                try:
                    _pd = max(7, int(self.get_setting("memory_decay_purge_days") or 0) or 180)
                    _pdcut = now - _pd * 86400
                    _pdropped = 0
                    for _tbl2 in ("person_memories", "group_memories", "shared_memories"):
                        _pdropped += self._archive_and_delete(_tbl2, "status='decayed' AND decayed_at>0 AND decayed_at<?", (_pdcut,), "decayed")
                    out["purged_decayed"] = _pdropped
                except Exception as e:
                    _log("store", "清理淡出记忆失败 %r" % (e,))
                # 淘汰行先释放向量；正文保留 30 天供面板回溯，再整行删。
                for _tbl in ("person_memories", "group_memories", "shared_memories"):
                    try:
                        self.conn.execute("UPDATE %s SET vec=NULL, vec_model='' WHERE status='pruned' AND vec IS NOT NULL" % _tbl)
                    except Exception:
                        pass
                self.conn.commit()
        except Exception as e:
            _log("store", "prune 失败：%r" % e)
            out["error"] = str(e)[:200]
        # 历史导入/长期没新增的记忆不会经过 add 路径，必须全量压一遍。
        try:
            out["memory_scopes"] = self.prune_memory_scopes()
        except Exception as e:
            _log("store", "全量记忆上限检查失败 %r" % e)
        return out

    # ---------- v0.63.0：记忆衰减（分层遗忘） ----------
    # 规则（用户 2026-10-27 拍板）：
    #   低价值（importance<=2）且从没被想起来过 → 满 N 天淡出；
    #   一般（importance==3）且最近 N 天没被想起来 → 淡出；
    #   重要（importance>=4）和约定义务（commitment）永不自动淡出。
    # 淡出只把 status 标成 'decayed' 并记 decayed_at，正文留 memory_decay_purge_days 天可捞回，到期才真删。
    def _decay_days(self, key, default):
        try:
            return max(1, int(self.get_setting(key) or 0) or int(default))
        except Exception:
            return int(default)

    def decay_memories(self, low_never_days=7, low_days=30, mid_days=90, now=None):
        """按分层规则把该忘的标成 decayed。返回 {"low":n,"mid":n}。

        三层（用户 2026-10-27 拍板）：
          - 短期池：importance<=2 且从没被想起来过 → low_never_days(默认 7) 天后淡出；
          - 短期池但被用过：importance<=2 且 low_days(默认 30) 天没动静 → 淡出；
          - 长期池：importance==3 且 mid_days(默认 90) 天没被想起来 → 淡出；
          - 重要（>=4）与承诺（commitment）永不自动淡出。
        """
        now = int(now or time.time())
        cuts = [
            ("low", "importance<=2 AND use_count=0", now - max(1, int(low_never_days)) * 86400),
            ("low", "importance<=2 AND use_count>0",
             now - max(1, int(low_days)) * 86400),
            ("mid", "importance=3",
             now - max(1, int(mid_days)) * 86400),
        ]
        out = {"low": 0, "mid": 0}
        with self._lock:
            for table in ("person_memories", "group_memories", "shared_memories"):
                for bucket, cond, cut in cuts:
                    try:
                        cur = self.conn.execute(
                            "UPDATE %s SET status='decayed', decayed_at=?, vec=NULL, vec_model='' "
                            "WHERE status='active' AND protected=0 AND %s AND kind<>'commitment' "
                            "AND (CASE WHEN use_count>0 AND last_used>0 THEN last_used ELSE created END)<? "
                            "AND (CASE WHEN use_count>0 AND last_used>0 THEN last_used ELSE created END)>0"
                            % (table, cond), (now, cut))
                        out[bucket] += max(cur.rowcount, 0)
                    except Exception as e:
                        _log("store", "记忆衰减 %s 失败 %r" % (table, e))
            self.conn.commit()
        return out

    def cap_high_tier(self, keep_max=25, now=None):
        """保留旧调用接口；提示词配额不能降级持久化的重要记忆。"""
        return {"demoted": 0}

    def purge_decayed(self, days=180, now=None):
        """淡出超过 N 天的记忆：v0.69.0 起不再直接删正文——先搬进底片（cold 地址），再删行。

        用户 2026-10-28 拍板「效果优先」：淡出可以，但正文不该真消失。
        搬进底片后：占 0 token、不参与召回，只有明确「挖一下」才取原文。
        """
        now = int(now or time.time())
        cut = now - max(1, int(days)) * 86400
        dropped = 0
        with self._lock:
            for table in self.MEMORY_TABLES:
                try:
                    where = "status='decayed' AND decayed_at>0 AND decayed_at<?"
                    dropped += self._archive_and_delete(table, where, (cut,), "decayed")
                except Exception as e:
                    _log("store", "清理淡出记忆 %s 失败 %r" % (table, e))
            self.conn.commit()
        return dropped

    # ---------- v0.69.0：底片（原文存档） ----------
    MEMORY_TABLES = ("person_memories", "group_memories", "shared_memories")
    _ARCHIVE_PREFIX = {"person_memories": "p", "group_memories": "g", "shared_memories": "s"}

    def _archive_rows(self, table, where_sql, params, reason=""):
        """把满足条件的记忆正文搬进底片（不删源行），返回搬进去的条数。已存过的不重复存。"""
        if table not in self.MEMORY_TABLES:
            return 0
        prefix = self._ARCHIVE_PREFIX[table]
        now = int(time.time())
        n = 0
        try:
            rows = self.conn.execute("SELECT * FROM %s WHERE %s" % (table, where_sql), params).fetchall()
        except Exception as e:
            _log("store", "底片读取 %s 失败 %r" % (table, e))
            return 0
        for r in rows:
            try:
                mid = int(r["id"])
                address = "cold://%s%d" % (prefix, mid)
                body = _dec(r["text"]) if r["text"] else ""
                if not body:
                    continue
                dup = self.conn.execute(
                    "SELECT 1 FROM memory_archive WHERE address=?", (address,)).fetchone()
                if dup:
                    continue
                if table == "shared_memories":
                    owner = str(r["people"] or "")
                else:
                    owner = str(r["qq"] if table == "person_memories" else r["gid"])
                facts = r["key_facts"] if "key_facts" in r.keys() else ""
                gist = " ".join(str(body).split())[:40]
                self.conn.execute(
                    "INSERT OR REPLACE INTO memory_archive(address,src_table,src_id,owner,text,key_facts,"
                    "kind,importance,gist,reason,created,archived_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (address, table, mid, owner, _enc(body), facts or "", str(r["kind"] or ""),
                     int(r["importance"] or 3), gist, reason, int(r["created"] or 0), now))
                n += 1
            except Exception as e:
                _log("store", "底片写入失败 %s/%s：%r" % (table, r["id"] if r else "?", e))
        if n:
            _log("store", "底片存档 %s：%d 条" % (table, n))
        return n

    def backfill_key_facts(self, limit=3000):
        """v0.69.0：给存量记忆补关键事实（老库这一列是空的）。
        只用正则兜底，不调模型；补不上（正文里没有数字/结论）的就留空，下次不会重复扫。"""
        done = 0
        now = int(time.time())
        with self._lock:
            for table in self.MEMORY_TABLES:
                try:
                    rows = self.conn.execute(
                        "SELECT id,text FROM %s WHERE status='active' AND (key_facts IS NULL OR key_facts='') "
                        "LIMIT ?" % table, (int(limit),)).fetchall()
                except Exception as e:
                    _log("store", "关键事实回填读取 %s 失败 %r" % (table, e))
                    continue
                for r in rows:
                    try:
                        txt = _dec(r["text"]) or ""
                        facts = facts_to_json(guess_key_facts(txt))
                        # 没抠到也写一个空串以外的标记？不：保持空，下次还能补（代价是每次开库重扫，
                        # 但上限 3000 条、纯正则，几毫秒，比留一堆占位标记干净）
                        if not facts:
                            continue
                        self.conn.execute("UPDATE %s SET key_facts=? WHERE id=?" % table, (facts, int(r["id"])))
                        done += 1
                    except Exception:
                        continue
            self.conn.commit()
        return done

    def archive_count(self):
        try:
            with self._lock:
                return int(self.conn.execute("SELECT COUNT(*) FROM visible_memory_archive").fetchone()[0] or 0)
        except Exception:
            return 0

    def list_memory_archive(self, limit=200, query=""):
        """面板用：列出底片（正文默认也返回，面板要能看）。"""
        q = " ".join(str(query or "").split())
        out = []
        with self._lock:
            try:
                if q:
                    rows = self.conn.execute(
                        "SELECT * FROM visible_memory_archive WHERE gist LIKE ? OR text LIKE ? "
                        "ORDER BY archived_at DESC LIMIT ?", ("%" + q + "%", "%" + q + "%", int(limit))).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT * FROM visible_memory_archive ORDER BY archived_at DESC LIMIT ?", (int(limit),)).fetchall()
            except Exception:
                rows = []
            for r in rows:
                out.append({"id": int(r["id"]), "address": str(r["address"] or ""),
                            "src_table": str(r["src_table"] or ""), "src_id": int(r["src_id"] or 0),
                            "owner": str(r["owner"] or ""), "text": _dec(r["text"]) or "",
                            "key_facts": str(r["key_facts"] or ""), "kind": str(r["kind"] or ""),
                            "importance": int(r["importance"] or 3), "gist": str(r["gist"] or ""),
                            "reason": str(r["reason"] or ""), "created": int(r["created"] or 0),
                            "archived_at": int(r["archived_at"] or 0)})
        return out

    def search_memory_archive(self, words, limit=2):
        """按关键词找底片，只返回地址 + 一句摘要（不返回正文，省 token）。"""
        ws = [str(w or "").strip() for w in (words or []) if len(str(w or "").strip()) >= 2][:6]
        if not ws:
            return []
        out = []
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT address,gist,key_facts,created FROM visible_memory_archive ORDER BY archived_at DESC LIMIT 500").fetchall()
            except Exception:
                rows = []
        for r in rows:
            hay = "%s %s" % (r["gist"] or "", r["key_facts"] or "")
            hit = [w for w in ws if w in hay]
            if not hit:
                continue
            out.append({"address": str(r["address"] or ""), "gist": str(r["gist"] or ""),
                        "key_facts": str(r["key_facts"] or ""), "created": int(r["created"] or 0),
                        "hits": hit})
        out.sort(key=lambda x: len(x["hits"]), reverse=True)
        return out[:max(1, int(limit))]

    def dig_memory(self, needle):
        """挖底片：给 cold://p12 这种地址就取正文；给一句话就按关键词找。"""
        raw = " ".join(str(needle or "").split())
        if not raw:
            return None
        m = re.match(r"^(?:cold://)?([pgs])(\d+)$", raw, re.I)
        if m:
            address = "cold://%s%s" % (m.group(1).lower(), m.group(2))
            with self._lock:
                try:
                    r = self.conn.execute("SELECT * FROM visible_memory_archive WHERE address=?", (address,)).fetchone()
                except Exception:
                    r = None
            if r:
                return {"address": address, "gist": str(r["gist"] or ""), "text": _dec(r["text"]) or "",
                        "key_facts": str(r["key_facts"] or ""), "archived_at": int(r["archived_at"] or 0),
                        "src_table": str(r["src_table"] or ""), "owner": str(r["owner"] or ""),
                        "created": int(r["created"] or 0)}
            return None
        hits = self.search_memory_archive([raw] + raw.split(), limit=1)
        if not hits:
            return None
        return self.dig_memory(hits[0]["address"])

    def decayed_memories(self, limit=200):
        """面板用：列出已经淡忘的（还能捞回来）。"""
        out = []
        with self._lock:
            for table, key_col in (("person_memories", "qq"), ("group_memories", "gid")):
                try:
                    rows = self.conn.execute(
                        "SELECT id,%s AS k,text,kind,importance,created,decayed_at,use_count FROM %s "
                        "WHERE status='decayed' ORDER BY decayed_at DESC LIMIT ?" % (key_col, table),
                        (int(limit),)).fetchall()
                except Exception:
                    rows = []
                for r in rows:
                    d = dict(r)
                    d["text"] = _dec(d.get("text"))
                    d["table"] = table
                    out.append(d)
        out.sort(key=lambda x: int(x.get("decayed_at") or 0), reverse=True)
        return out[:int(limit)]

    def restore_memory(self, table, mid):
        """把一条淡出的记忆捞回来（重新算 active，向量下次自动补）。

        v0.63.3 修：捞回来时必须把「被想起」的时钟也重置——淡出条件是按 created/last_used 算年龄的，
        如果只改 status，下一次每日维护立刻又满足条件、当场再淡出（捞回来等于没捞）。
        所以这里同时把 last_used 记成现在、use_count +1：低价值进「用过」那一档（30 天），
        一般记忆进 90 天档，至少能安稳待一个周期。
        """
        if table not in ("person_memories", "group_memories", "shared_memories"):
            return False
        now = int(time.time())
        with self._lock:
            cur = self.conn.execute(
                "UPDATE %s SET status='active', decayed_at=0, vec=NULL, vec_model='', "
                "last_used=?, use_count=use_count+1 "
                "WHERE id=? AND status='decayed'" % table, (now, int(mid)))
            self.conn.commit()
            return bool(cur.rowcount)

    def decayed_count(self):
        try:
            with self._lock:
                n = 0
                for table in ("person_memories", "group_memories", "shared_memories"):
                    n += int(self.conn.execute(
                        "SELECT COUNT(*) FROM %s WHERE status='decayed'" % table).fetchone()[0] or 0)
                return n
        except Exception:
            return 0

    def prune_memory_scopes(self):
        """把每个群/每个人的 active 记忆压回上限；返回标记淘汰的条数。"""
        dropped = 0
        for table, key_col in (("person_memories", "qq"), ("group_memories", "gid")):
            try:
                with self._lock:
                    keys = [r[0] for r in self.conn.execute(
                        "SELECT DISTINCT %s FROM %s WHERE status='active'" % (key_col, table)).fetchall()]
            except Exception as e:
                _log("store", "记忆上限检查读取 %s 失败 %r" % (table, e))
                continue
            keep = self._mem_max(table)
            for key in keys:
                try:
                    with self._lock:
                        before = self.conn.execute(
                            "SELECT COUNT(*) FROM %s WHERE %s=? AND status='active'" % (table, key_col), (key,)).fetchone()[0]
                        if before > keep:
                            self._prune_scope(table, key_col, key, keep)
                            after = self.conn.execute(
                                "SELECT COUNT(*) FROM %s WHERE %s=? AND status='active'" % (table, key_col), (key,)).fetchone()[0]
                            dropped += max(0, before - after)
                except Exception as e:
                    _log("store", "记忆上限检查 %s/%s 失败 %r" % (table, key, e))
        if dropped:
            _log("store", "记忆上限压缩：%d 条" % dropped)
        return dropped

    QZONE_IMAGE_DAYS = 180
    VACUUM_MIN_DAYS = 30

    def maintenance(self):
        """prune + 媒体/生成文件清理。media 只删未被 messages.attachments 引用的旧图。返回计数。"""
        out = self.prune()
        data_dir = os.path.dirname(os.path.abspath(self.path))
        try:
            referenced = self._referenced_media()
        except Exception as e:
            _log("store", "维护：读取 attachments 失败 %r" % e)
            referenced = set()
        out["media"] = self._clean_dir(os.path.join(data_dir, "media"), self.MEDIA_DAYS, referenced)
        out["files"] = self._clean_dir(os.path.join(data_dir, "files"), self.FILES_DAYS, None)
        try:
            # 2026-10-25 修 P1-7：QQ 空间配图以前只清 index.json，.jpg 文件永不删
            out["qzone_images"] = self._clean_qzone_images()
        except Exception as e:
            _log("store", "维护：清理 QQ 空间配图失败 %r" % (e,))
        try:
            out["shares"] = self.cleanup_shares()
            out["share_bits"] = self.cleanup_pending_shares()
        except Exception as e:
            _log("store", "维护：清理分享账本失败 %r" % (e,))
        try:
            # 低频 VACUUM（默认 30 天一次）；不要每天做，库大时会长时间占写锁
            out["vacuum"] = self.maybe_vacuum()
        except Exception as e:
            _log("store", "维护：VACUUM 失败 %r" % (e,))
        _log("store", "维护完成：%r" % out)
        return out

    def _clean_qzone_images(self, days=None):
        """按 180 天 + index.json 引用清理 QQ 空间配图的落盘文件。

        flow_qzone_images 的 index.json 只记 180 天/500 条内的账：
        - 文件还在 index 里 = 最近用过，留着去重；
        - 不在 index 且 mtime 超过 180 天 = 旧账，删；
        - *.part 是写一半的临时文件，超过 1 天删。
        index.json 读不出来时直接跳过（宁可多留，不误删）。
        """
        try:
            days = int(days if days is not None else self.QZONE_IMAGE_DAYS)
        except Exception:
            days = self.QZONE_IMAGE_DAYS
        qdir = os.path.join(os.path.dirname(os.path.abspath(self.path)), "qzone_images")
        if not os.path.isdir(qdir):
            return 0
        idx_path = os.path.join(qdir, "index.json")
        referenced = set()
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
            if isinstance(idx, dict):
                referenced = set(str(k) for k in idx.keys())
        except Exception:
            _log("store", "维护：%s 读不了，跳过 QQ 空间配图清理" % idx_path)
            return 0
        now = time.time()
        cutoff = now - days * 86400
        removed = 0
        try:
            names = os.listdir(qdir)
        except Exception as e:
            _log("store", "维护：列 QQ 空间配图目录失败 %r" % (e,))
            return 0
        for name in names:
            if name == "index.json" or name.startswith("."):
                continue
            fp = os.path.join(qdir, name)
            try:
                if not os.path.isfile(fp):
                    continue
                low = name.lower()
                mtime = os.path.getmtime(fp)
                if low.endswith(".part"):
                    if mtime < now - 86400:
                        os.remove(fp)
                        removed += 1
                    continue
                # 只动配图文件；目录里将来若放 README 之类，不要误删
                if not low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    continue
                if os.path.splitext(name)[0] in referenced:
                    continue
                if mtime >= cutoff:
                    continue
                os.remove(fp)
                removed += 1
            except Exception as e:
                _log("store", "维护：删除 QQ 空间配图失败 %s %r" % (fp, e))
        return removed

    def maybe_vacuum(self, min_days=None):
        """低频 VACUUM：默认 30 天内最多一次，回收被删记忆/消息空出来的页。"""
        try:
            days = int(min_days if min_days is not None else self.VACUUM_MIN_DAYS)
        except Exception:
            days = self.VACUUM_MIN_DAYS
        try:
            last = int(self.get_setting("last_vacuum_at") or 0)
        except Exception:
            last = 0
        if last and time.time() - last < days * 86400:
            return 0
        with self._lock:
            try:
                self.conn.commit()
                self.conn.execute("VACUUM")
                self.set_setting("last_vacuum_at", str(int(time.time())))
                _log("store", "VACUUM 完成（距上次 %s）" % (
                    ("%d 天" % ((time.time() - last) // 86400)) if last else "首次"))
                return 1
            except Exception as e:
                _log("store", "VACUUM 失败 %r" % (e,))
                return 0

    def _referenced_media(self):
        names = set()
        with self._lock:
            rows = self.conn.execute(
                "SELECT attachments FROM messages WHERE attachments IS NOT NULL "
                "AND attachments <> '' AND attachments <> '[]'").fetchall()
        for r in rows:
            try:
                items = json.loads(r["attachments"] or "[]")
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for a in items:
                if isinstance(a, dict) and a.get("file"):
                    names.add(os.path.basename(str(a["file"])))
        return names

    def _clean_dir(self, path, days, referenced):
        if not os.path.isdir(path):
            return 0
        cutoff = time.time() - int(days) * 86400
        removed = 0
        try:
            names = os.listdir(path)
        except Exception as e:
            _log("store", "维护：列目录失败 %s %r" % (path, e))
            return 0
        for name in names:
            if name.startswith("."):
                continue
            if referenced is not None and name in referenced:
                continue
            fp = os.path.join(path, name)
            try:
                if not os.path.isfile(fp) or os.path.getmtime(fp) >= cutoff:
                    continue
                os.remove(fp)
                removed += 1
            except Exception as e:
                _log("store", "维护：删除失败 %s %r" % (fp, e))
        return removed

    def add_message(self, conv, sender_id, sender_name, direction, text, raw="", message_id=0, attachments=None,
                    ts=None):
        captured, attachments = message_context(self, conv, sender_id, direction, message_id, attachments)
        if captured:
            return captured
        with self._lock:
            if not write_allowed(self) or self.is_blocked(sender_id) or (str(conv).startswith("p:") and self.is_blocked(str(conv)[2:])):
                return 0
            attachments, _sleep_tainted = self.sleep_attachments(conv, direction, text, raw, attachments)
            cur = self.conn.execute(
                "INSERT INTO messages(ts,conv,sender_id,sender_name,direction,text,raw,message_id,attachments) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(ts or time.time()), conv, sender_id, sender_name, direction, _enc(text), _enc(raw), message_id,
                 json.dumps(attachments or [], ensure_ascii=False)))
            self.conn.commit()
            if _sleep_tainted:
                self.conn.execute("INSERT OR IGNORE INTO sleep_tainted VALUES(?,?)", (cur.lastrowid, "dream_or_quote"))
                self.conn.commit()
            if direction == "out":
                from sleep_memory import finish_reply
                finish_reply(self, str(text or ""))
            try:
                self.self_capture_message(cur.lastrowid)
            except Exception as error:
                _log('self_continuity','经历记录未完成：'+type(error).__name__)
            if cur.lastrowid % 500 == 0:
                self.prune()
            return cur.lastrowid

    def sticker_usage_between(self, ts_from, ts_recent=None):
        """ts_from 起每张表情包用了几次，同时单独统计最近一段（默认 24h）的。

        v0.56.4：7 天窗口看「这几张是不是总在用」，24 小时窗口看「刚刚是不是又发了它」。
        """
        allc, recent = {}, {}
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT ts, attachments FROM visible_messages WHERE ts>=? AND direction='out' "
                    "AND attachments LIKE '%sticker%'", (int(ts_from),)).fetchall()
        except Exception:
            return allc, recent
        import time as _t
        cut = int(ts_recent if ts_recent is not None else (_t.time() - 86400))
        for r in rows:
            try:
                ts = int(r[0] if not isinstance(r, dict) else r["ts"])
                arr = json.loads((r[1] if not isinstance(r, dict) else r["attachments"]) or "[]")
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for a in arr:
                if isinstance(a, dict) and str(a.get("type")) == "sticker":
                    f = str(a.get("file") or "")
                    if not f:
                        continue
                    allc[f] = allc.get(f, 0) + 1
                    if ts >= cut:
                        recent[f] = recent.get(f, 0) + 1
        return allc, recent

    def recent_sticker_files(self, conv, limit=10):
        """Recover conversation-local repetition protection after a process restart."""
        limit = max(1, min(50, int(limit)))
        with self._lock:
            rows = self.conn.execute(
                "SELECT attachments FROM visible_messages WHERE conv=? AND direction='out' "
                "AND attachments LIKE '%sticker%' ORDER BY id DESC LIMIT ?", (str(conv), limit)).fetchall()
        out = []
        for row in rows:
            try:
                arr = json.loads(row[0] or '[]')
            except (ValueError, TypeError):
                continue
            for item in arr if isinstance(arr, list) else []:
                if isinstance(item, dict) and item.get('type') == 'sticker':
                    name = str(item.get('file') or '').replace('\\', '/').rsplit('/', 1)[-1]
                    if name and name not in out:
                        out.append(name)
        return out[:limit]

    def sticker_usage_since(self, ts):
        """最近这段时间每张表情包用了几次（给「别老是那几张」降权用）。

        attachments 是明文 JSON，text/raw 才加密，所以这里能直接 LIKE 粗筛再解析。
        v0.56.4：2026-10-26 用户反馈「总是那几个表情包」，实测 7 天 621 次只碰了 226 张，
        前 5 张占 20%（无语/得意这种万能图总排在语义前面）。
        """
        out = {}
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT attachments FROM visible_messages WHERE ts>=? AND direction='out' "
                    "AND attachments LIKE '%sticker%'", (int(ts),)).fetchall()
        except Exception:
            return out
        for r in rows:
            try:
                arr = json.loads((r[0] if not isinstance(r, dict) else r["attachments"]) or "[]")
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for a in arr:
                if isinstance(a, dict) and str(a.get("type")) == "sticker":
                    f = str(a.get("file") or "")
                    if f:
                        out[f] = out.get(f, 0) + 1
        return out

    def recent(self, conv, limit):
        with self._lock:
            rows = self.conn.execute(
                "SELECT direction,text FROM visible_messages WHERE conv=? ORDER BY id DESC LIMIT ?",
                (conv, int(limit))).fetchall()
        out = []
        for r in reversed(rows):
            if r["text"]:
                out.append({"role": "user" if r["direction"] == "in" else "assistant", "content": _dec(r["text"])})
        return out

    def set_forward_note(self, message_id, note):
        """后台看完转发里的图片后只更新对应附件，保留同条消息的其他附件。"""
        with self._lock:
            row = self.conn.execute("SELECT id,attachments FROM visible_messages WHERE message_id=? ORDER BY id DESC LIMIT 1",
                                    (int(message_id),)).fetchone()
            if not row:
                return False
            try:
                atts = json.loads(row["attachments"] or "[]")
            except (ValueError, TypeError):
                return False
            if not isinstance(atts, list):
                return False
            for a in atts:
                if isinstance(a, dict) and a.get("type") == "forward":
                    a["note"] = str(note or "")[:13000]
                    self.conn.execute("UPDATE messages SET attachments=? WHERE id=?",
                                      (json.dumps(atts, ensure_ascii=False), row["id"]))
                    self.conn.commit()
                    return True
            return False

    def messages_for_context(self, conv, limit, self_id=0, bot_names=(), since=0, after_id=0):
        """给模型看的历史：带时间和说话人，引用关系从附件渲染。

        - 群聊用户消息：「[昵称(QQ号)] 正文」；私聊用户消息：「[19:58] 正文」；
        - 自己的消息也带时间（assistant 角色已表明是谁），方便模型接“刚才/前面”；
        - 旧消息正文开头的「[引用上下文…]」只在展示时剥掉，数据库原文不动；
        - quote 附件渲染成「谁引用了谁的哪句话」，图片 note 渲染成短的「（附图：…）」。
        - v0.45.0：身份按 QQ 号锚定。群聊每条都带发送人 QQ；昵称和本鲸撞车的号
          （另一个「大肥鱼」）额外标「（同名号，不是你）」，防止模型把别人发的图/话
          认成自己干的。self_id/bot_names 不传时保持旧格式（兼容只读调用方）。
        """
        # ctx_start：手动“开新会话”时把旧聊天挡在上下文外，聊天记录本身仍保留
        try:
            start = int(self.get_setting("ctx_start:" + conv) or 0)
        except Exception:
            start = 0
        start = max(start, int(after_id or 0))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,message_id,ts,direction,sender_id,sender_name,text,attachments FROM visible_messages WHERE conv=? AND id>? AND ts>=? "
                "ORDER BY id DESC LIMIT ?", (conv, start, int(since or 0), int(limit))).fetchall()
        is_group = str(conv).startswith("g:")
        out = []
        for r in reversed(rows):
            role = "user" if r["direction"] == "in" else "assistant"
            try:
                attachments = json.loads(r["attachments"] or "[]")
            except Exception:
                attachments = []
            if not isinstance(attachments, list):
                attachments = []
            text = _strip_legacy_quote(_dec(r["text"]) or "").strip()
            text = _strip_ctx_prefix(text)
            if role == "assistant":
                text = strip_response_notes(text)
            text = _render_attachment_notes(text, attachments, r["direction"])
            at_tag = _mention_tag(text) if (role == "user" and is_group) else ""
            quotes = [a for a in attachments if isinstance(a, dict) and a.get("type") == "quote"]
            if not text and not quotes:
                continue
            if not text and quotes:
                text = "（只发了一条引用，没有文字）"
            who = _clean_name(r["sender_name"], 20) or ("群友" if role == "user" else "你")
            if role == "user" and is_group:
                # 群聊只带说话人，不带时间：时间统一放在系统提示的线程/时间线里，
                # 避免模型把 [19:34] 当成自己该模仿的回复格式。
                # v0.45.0：每条带 QQ 号，身份按 QQ 认，不靠昵称。昵称撞车的号标「不是你」。
                label = who
                try:
                    qq = int(r["sender_id"] or 0)
                except Exception:
                    qq = 0
                if qq:
                    label = "%s(QQ%d)" % (who, qq)
                    if self_id and qq != int(self_id) and any(
                            n and str(n) in str(who) for n in (bot_names or ())):
                        label += "（同名号，不是你）"
                text = "[%s] %s%s" % (label, text, at_tag)
            qlines = [_render_quote_relation(q, who) for q in quotes]
            qlines = [x for x in qlines if x]
            if qlines:
                text += "\n" + "\n".join(qlines)
            item = {"role": role, "content": text, "source_id": r["id"],
                    "sender_id": r["sender_id"], "message_id": r["message_id"]}
            if role == "assistant":
                target = response_target(attachments)
                if target:
                    item["response_to"] = target
            out.append(item)
        return out

    def same_name_people(self, self_id=0, names=(), limit=20):
        """昵称和本鲸撞车、但 QQ 不是本鲸的号（另一个「大肥鱼」）。

        v0.45.0：身份只按 QQ 认；昵称仅用来“发现可能撞名”，发现后一律按 QQ 处理。
        返回 [{"qq": int, "name": str, "last_seen": int}]，按最近发言倒序。
        """
        try:
            self_id = int(self_id or 0)
        except Exception:
            self_id = 0
        keys = [str(n).strip() for n in (names or ()) if str(n or "").strip()]
        if not keys:
            return []
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT qq,name,last_seen FROM visible_people WHERE qq>0 AND qq<>? "
                    "ORDER BY last_seen DESC LIMIT 500", (self_id,)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            nm = str(r["name"] or "")
            if not nm or not any(k in nm for k in keys):
                continue
            out.append({"qq": int(r["qq"]), "name": nm, "last_seen": int(r["last_seen"] or 0)})
            if len(out) >= int(limit or 20):
                break
        return out

    def last_sticker_file(self, conv):
        """最近一条自己发的表情包文件名（v0.41.0 纠错命令用）。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT attachments FROM visible_messages WHERE conv=? AND direction='out' "
                    "ORDER BY id DESC LIMIT 10", (conv,)).fetchall()
        except Exception:
            return ""
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
            except Exception:
                continue
            for a in atts or []:
                if isinstance(a, dict) and a.get("type") == "sticker" and a.get("file"):
                    return str(a.get("file") or "")
        return ""

    def last_sticker(self, conv):
        """最近一条自己发的表情包：{file, ts}（v0.54.15 纠错扔回收站用）。
        没有就返回 None。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT ts, attachments FROM visible_messages WHERE conv=? AND direction='out' "
                    "ORDER BY id DESC LIMIT 10", (conv,)).fetchall()
        except Exception:
            return None
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
            except Exception:
                continue
            for a in atts or []:
                if isinstance(a, dict) and a.get("type") == "sticker" and a.get("file"):
                    return {"file": str(a.get("file") or ""), "ts": int(r["ts"] or 0)}
        return None

    def last_inbound_ts(self, conv):
        """这个会话里最后一条「别人说的话」的时间（群冷场判断用）。"""
        with self._lock:
            r = self.conn.execute(
                "SELECT MAX(ts) t FROM visible_messages WHERE conv=? AND direction='in'", (str(conv),)).fetchone()
        return int((r["t"] if r else 0) or 0)

    def last_message_before(self, conv, before_ts):
        """最后一条早于 before_ts 的消息：群主动发言的稳定指纹只用“沉默开始前”那一句。"""
        with self._lock:
            r = self.conn.execute(
                "SELECT id,ts,direction,sender_name,text FROM visible_messages WHERE conv=? AND ts<=? "
                "ORDER BY id DESC LIMIT 1", (conv, int(before_ts))).fetchone()
        return dict(r) if r else None

    def last_message(self, conv):
        """最后一条消息（v0.24.2：判断上一次主动开口之后有没有人回）。"""
        with self._lock:
            r = self.conn.execute(
                "SELECT ts,direction,sender_id,sender_name,text FROM visible_messages WHERE conv=? ORDER BY id DESC LIMIT 1",
                (conv,)).fetchone()
        return dict(r) if r else None

    def transcript(self, conv, limit=16):
        """主人问“你跟X聊了什么”时给模型看的真实记录：带时间、谁说的、附件备注压短。

        只读，不改库；群聊带上说话人，私聊里自己的消息标“我”。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,direction,sender_name,text,attachments FROM visible_messages WHERE conv=? "
                "ORDER BY id DESC LIMIT ?", (conv, int(limit))).fetchall()
        is_group = str(conv).startswith("g:")
        out = []
        for r in reversed(rows):
            try:
                att = json.loads(r["attachments"] or "[]")
            except Exception:
                att = []
            if not isinstance(att, list):
                att = []
            text = _strip_legacy_quote(_dec(r["text"]) or "").strip()
            text = _strip_ctx_prefix(text)
            text = _render_attachment_notes(text, att).strip()
            if not text:
                continue
            if r["direction"] == "out":
                who = "我"
            else:
                who = _clean_name(r["sender_name"] or "", 20) or ("群友" if is_group else "对方")
            out.append({"ts": int(r["ts"] or 0), "dir": r["direction"], "who": who,
                        "text": " ".join(text.split())})
        return out

    def recent_conv_briefs(self, limit=8):
        """最近有动静的会话 + 最后一条消息（给“最近谁找你”用）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT conv, MAX(ts) last_ts FROM visible_messages GROUP BY conv ORDER BY last_ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        names = self.conv_names()
        out = []
        for r in rows:
            conv = str(r["conv"] or "")
            m = self.last_message(conv) or {}
            text = _strip_legacy_quote(_dec(m.get("text") or "") or "")
            text = _strip_ctx_prefix(text).strip()
            out.append({"conv": conv, "last_ts": int(r["last_ts"] or 0),
                        "name": names.get(conv) or conv,
                        "dir": m.get("direction") or "",
                        "who": _clean_name(m.get("sender_name") or "", 20),
                        "text": " ".join(text.split())[:120]})
        return out

    def message_by_id(self, message_id):
        """按 QQ 消息号取一条库里的消息（描述回填用）。"""
        try:
            mid = int(message_id or 0)
        except Exception:
            return None
        if not mid:
            return None
        with self._lock:
            r = self.conn.execute(
                "SELECT id,ts,conv,sender_id,sender_name,message_id,text,attachments FROM visible_messages "
                "WHERE message_id=? LIMIT 1", (mid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["text"] = _dec(d["text"])
        try:
            d["attachments"] = json.loads(d["attachments"] or "[]")
        except Exception:
            d["attachments"] = []
        return d

    def recent_image_messages(self, conv, limit=12, within_sec=900):
        """最近带图/表情包的入向消息。described 表示正文里已经带画面描述，不用再看第二遍。"""
        since = int(time.time()) - max(60, int(within_sec or 900))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,ts,sender_id,sender_name,message_id,text,attachments FROM visible_messages "
                "WHERE conv=? AND direction='in' AND ts>=? "
                "AND (attachments LIKE '%\"image\"%' OR attachments LIKE '%\"sticker\"%') "
                "ORDER BY id DESC LIMIT ?", (conv, since, int(limit))).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            text = _dec(d["text"]) or ""
            try:
                atts = json.loads(d["attachments"] or "[]")
            except Exception:
                atts = []
            d["text"] = text
            d["attachments"] = atts
            d["described"] = ("（画面：" in text) or any(
                isinstance(a, dict) and a.get("note") for a in atts)
            out.append(d)
        return out

    def recent_image_attachments(self, conv, limit=10, within_sec=1800):
        """最近聊过的图（收发的都算），给“参考刚才那张”找参考图用。

        返回按新到旧：{ts, direction, sender, type, file, url}。
        """
        since = int(time.time()) - max(60, int(within_sec or 1800))
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,direction,sender_name,attachments FROM visible_messages "
                "WHERE conv=? AND ts>=? AND (attachments LIKE '%\"image\"%' OR attachments LIKE '%\"sticker\"%') "
                "ORDER BY id DESC LIMIT ?", (conv, since, max(1, int(limit) * 3))).fetchall()
        out = []
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
            except Exception:
                atts = []
            if not isinstance(atts, list):
                continue
            for a in atts:
                if not isinstance(a, dict):
                    continue
                if a.get("type") not in ("image", "sticker"):
                    continue
                out.append({"ts": r["ts"], "direction": r["direction"], "sender": r["sender_name"],
                            "type": a.get("type"), "file": str(a.get("file") or ""),
                            "url": str(a.get("url") or "")})
                if len(out) >= int(limit):
                    return out
        return out

    def set_message_desc(self, message_id, desc, limit=200):
        """把画面描述写回消息正文和附件备注（幂等：已有描述就不再改）。"""
        desc = " ".join(str(desc or "").split())[:limit]
        if not desc:
            return False
        row = self.message_by_id(message_id)
        if not row:
            return False
        text = row["text"] or ""
        if "（画面：" in text:
            return False
        if text.strip() in ("", "[图片]"):
            new = "[图片]（画面：%s）" % desc
        else:
            new = text.rstrip() + "（画面：%s）" % desc
        atts = []
        for a in row["attachments"]:
            if isinstance(a, dict) and not a.get("note") and a.get("type") in ("image", "sticker"):
                a = dict(a)
                a["note"] = desc
            atts.append(a)
        with self._lock:
            self.conn.execute("UPDATE messages SET text=?, attachments=? WHERE message_id=?",
                              (_enc(new), json.dumps(atts, ensure_ascii=False), int(message_id)))
            self.conn.commit()
        return True

    def older_text(self, conv, keep):
        with self._lock:
            rows = self.conn.execute(
                "SELECT direction,text FROM visible_messages WHERE conv=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (conv, int(keep))).fetchall()
        out = []
        for r in reversed(rows):
            if r["text"]:
                out.append(("我：" if r["direction"] == "out" else "对方：") + _dec(r["text"]))
        return "\n".join(out)

    def older_text_after(self, conv, keep, after_id=0, max_chars=20000, self_id=0, bot_names=()):
        """增量版 older_text：只取 keep 条之外、id > after_id 的旧消息，按 id 升序拼接。

        返回 (text, max_id)：
        - text：我：/对方： 逐行拼接，没有可摘要内容时为 ""；
        - max_id：本轮纳入的最大 message id（没有内容时等于 after_id），
          调用方存到 settings 的 summary_upto:<conv>，下次作为 after_id 传回来。
        累计字符到 max_chars 即停（不会截断已纳入的行），另有 SCAN_ROWS 行数上限；
        一次拿不完是正常的，下一轮从 max_id 继续。
        """
        def _int(v, default):
            try:
                return int(v)
            except Exception:
                return default
        keep = _int(keep, 0)
        after_id = _int(after_id, 0)
        max_chars = _int(max_chars, 0)
        if max_chars <= 0:
            max_chars = 20000
        with self._lock:
            cutoff = None
            if keep > 0:
                r = self.conn.execute(
                    "SELECT id FROM visible_messages WHERE conv=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (conv, keep - 1)).fetchone()
                if r is None:
                    return ("", after_id)
                cutoff = int(r["id"])
            if cutoff is None:
                rows = self.conn.execute(
                    "SELECT id,direction,sender_id,sender_name,text FROM visible_messages "
                    "WHERE conv=? AND id>? ORDER BY id ASC LIMIT ?",
                    (conv, after_id, self.SCAN_ROWS)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id,direction,sender_id,sender_name,text FROM visible_messages "
                    "WHERE conv=? AND id>? AND id<? ORDER BY id ASC LIMIT ?",
                    (conv, after_id, cutoff, self.SCAN_ROWS)).fetchall()
        parts = []
        total = 0
        max_id = after_id
        for r in rows:
            text = _dec(r["text"])
            if not text:
                continue
            # v0.45.0：摘要输入也按 QQ 认人；同名号（另一个大肥鱼）标出来，
            # 否则长期摘要会把别人的话写成“对方/我”，回头又认错人。
            if r["direction"] == "out":
                line = "我：" + text
            else:
                sender = _clean_name(r["sender_name"], 20) or "对方"
                try:
                    qq = int(r["sender_id"] or 0)
                except Exception:
                    qq = 0
                tag = ("%s(QQ%d)" % (sender, qq)) if qq else sender
                if self_id and qq and qq != int(self_id) and any(
                        n and str(n) in sender for n in (bot_names or ())):
                    tag += "（同名号，不是你）"
                line = tag + "：" + text
            add = len(line) + (1 if parts else 0)
            if total + add > max_chars:
                if not parts:
                    parts.append(line[:max_chars])
                    max_id = int(r["id"])
                break
            parts.append(line)
            total += add
            max_id = int(r["id"])
        if not parts and rows:
            max_id = int(rows[-1]["id"])
        return ("\n".join(parts), max_id)

    def recent_messages(self, limit=40):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,ts,direction,text,sender_name,conv,attachments FROM visible_messages ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for r in reversed(rows):
            try:
                att = json.loads(r["attachments"] or "[]")
            except Exception:
                att = []
            out.append({"id": r["id"], "ts": r["ts"], "dir": r["direction"], "text": _dec(r["text"]) or "",
                        "who": r["sender_name"] or "", "conv": r["conv"], "attachments": att})
        return out

    def today_counts(self):
        start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        with self._lock:
            rows = self.conn.execute(
                "SELECT direction, COUNT(*) c FROM visible_messages WHERE ts >= ? GROUP BY direction", (start,)).fetchall()
        got = {r["direction"]: r["c"] for r in rows}
        return {"in": got.get("in", 0), "out": got.get("out", 0), "total": sum(got.values())}

    # ---------- 会话 ----------
    def get_session(self, conv):
        with self._lock:
            r = self.conn.execute("SELECT * FROM sessions WHERE conv=?", (conv,)).fetchone()
        if not r:
            return {"conv": conv, "last_active": 0, "summary": "", "started_at": int(time.time())}
        return dict(r)

    def touch_session(self, conv, ts=None):
        ts = int(ts or time.time())
        with self._lock:
            cur = self.conn.execute("UPDATE sessions SET last_active=? WHERE conv=?", (ts, conv))
            if cur.rowcount == 0:
                self.conn.execute("INSERT INTO sessions(conv,last_active,summary,started_at) VALUES(?,?,?,?)",
                                  (conv, ts, "", ts))
            self.conn.commit()

    def set_summary(self, conv, summary):
        with self._lock:
            cur = self.conn.execute("UPDATE sessions SET summary=? WHERE conv=?", (summary, conv))
            if cur.rowcount == 0:
                self.conn.execute("INSERT INTO sessions(conv,last_active,summary,started_at) VALUES(?,?,?,?)",
                                  (conv, int(time.time()), summary, int(time.time())))
            self.conn.commit()

    def reset_session(self, conv, keep_summary=False):
        """开新会话。

        keep_summary=True 只重置计时（空闲太久自动开的“新会话”）：摘要和摘要水位线都留着，
        否则聊完一小时回来，前面聊过的事就全没了。手动点“开新会话”仍按默认清空。
        """
        with self._lock:
            if keep_summary:
                self.conn.execute("UPDATE sessions SET started_at=?, last_active=? WHERE conv=?",
                                  (int(time.time()), int(time.time()), conv))
            else:
                self.conn.execute("UPDATE sessions SET summary='', started_at=?, last_active=? WHERE conv=?",
                                  (int(time.time()), int(time.time()), conv))
                # 摘要水位线一起清掉，否则"开新会话"后第一轮摘要会从旧位置继续
                self.conn.execute("DELETE FROM settings WHERE k=?", ("summary_upto:%s" % conv,))
                self.conn.execute("DELETE FROM settings WHERE k=?", ("recent_topic:%s" % conv,))
                last = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM visible_messages WHERE conv=?", (conv,)).fetchone()[0]
                self.conn.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                                  ("recent_topic_start:%s" % conv, str(int(last or 0))))
                self.conn.execute("INSERT INTO settings(k,v) VALUES(?, '1') "
                                  "ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+1",
                                  ("recent_topic_generation:%s" % conv,))
            self.conn.commit()

    # ---------- 记忆 / 日记 / 提醒 ----------
    def upsert_conv(self, conv, name):
        with self._lock:
            self.conn.execute(
                "INSERT INTO convs(conv,name,updated_at) VALUES(?,?,?) ON CONFLICT(conv) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (conv, (name or "")[:60], int(time.time())))
            self.conn.commit()

    def conv_names(self):
        with self._lock:
            rows = self.conn.execute("SELECT conv,name FROM convs").fetchall()
        return {r["conv"]: r["name"] for r in rows}

    def add_memory(self, conv, text, world="private", group_id=0, person_id=0,
                   group_name="", person_name="", tags=""):
        text = str(text or "").strip()
        if not text:
            return False
        with self._lock:
            # 同一范围内的原样重复直接跳过：以前每 4 条回复抽一次记忆，会攒出一堆
            # “对方叫我杂鱼”这种近似重复，白白占每轮 6 条的名额。
            rows = self.conn.execute(
                "SELECT text FROM visible_memories WHERE world=? AND group_id=? AND person_id=?",
                (world, int(group_id or 0), int(person_id or 0))).fetchall()
            for old in rows:
                if _dec(old["text"]).strip() == text:
                    return False
            cur = self.conn.execute(
                "INSERT INTO memories(ts,conv,text,tags,world,group_id,person_id,group_name,person_name,last_used) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), conv, _enc(text[:500]), tags[:100], world, int(group_id or 0), int(person_id or 0),
                 (group_name or "")[:60], (person_name or "")[:40], int(time.time())))
            mid = cur.lastrowid
            for tok in tokenize(text):
                self.conn.execute("INSERT INTO mem_tokens(memory_id,token) VALUES(?,?)", (mid, tok))
            self.conn.commit()
            return True


    # ---------- 人物 / 记忆 v2（按 QQ 合并，全群共享） ----------
    def upsert_person(self, qq, name, ts=None, bump=True):
        if self.is_blocked(qq):
            return
        qq = int(qq or 0)
        if not qq:
            return
        ts = int(ts or time.time())
        nm = _clean_name(name, 40)
        with self._lock:
            row = self.conn.execute("SELECT name,aliases FROM visible_people WHERE qq=?", (qq,)).fetchone()
            if not row:
                self.conn.execute("INSERT INTO people(qq,name,aliases,first_seen,last_seen,msg_count) VALUES(?,?,?,?,?,?)",
                                  (qq, nm, json.dumps([nm] if nm else [], ensure_ascii=False), ts, ts, 1 if bump else 0))
            else:
                aliases = []
                try:
                    aliases = json.loads(row["aliases"] or "[]")
                except Exception:
                    aliases = []
                if nm and nm not in aliases:
                    aliases = (aliases + [nm])[-6:]
                if bump:
                    self.conn.execute(
                        "UPDATE people SET name=?,aliases=?,last_seen=?,msg_count=COALESCE(msg_count,0)+1 WHERE qq=?",
                        (nm or row["name"], json.dumps(aliases, ensure_ascii=False), ts, qq))
                else:
                    self.conn.execute("UPDATE people SET name=?,aliases=?,last_seen=? WHERE qq=?",
                                      (nm or row["name"], json.dumps(aliases, ensure_ascii=False), ts, qq))
            self.conn.commit()

    def person_name(self, qq):
        with self._lock:
            row = self.conn.execute("SELECT name FROM visible_people WHERE qq=?", (int(qq or 0),)).fetchone()
        return (row["name"] if row else "") or ""

    def person_aliases(self, qq):
        """这个 QQ 见过的昵称（含当前 name）；给认人兜底用，不写库。"""
        with self._lock:
            row = self.conn.execute("SELECT name,aliases FROM visible_people WHERE qq=?", (int(qq or 0),)).fetchone()
        if not row:
            return []
        out = []
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except Exception:
            aliases = []
        for nm in (aliases if isinstance(aliases, list) else []):
            nm = _clean_name(nm, 40)
            if nm and nm not in out:
                out.append(nm)
        cur = _clean_name(row["name"], 40)
        if cur and cur not in out:
            out.insert(0, cur)
        return out

    def _prune_scope(self, table, key_col, key, keep):
        keep = max(10, int(keep or 80))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM %s WHERE %s=? AND status='active' AND protected=0 AND kind<>'commitment' AND importance<4 ORDER BY importance DESC, use_count DESC, created DESC" % (table, key_col),
                (key,)).fetchall()
            drop = [r["id"] for r in rows[keep:]]
            if not drop:
                return
            now = int(time.time())
            for mid in drop:
                try:
                    # 2026-10-25 修 P1-2：淘汰时立刻清掉 2048 维向量（约 8KB），
                    # 只留正文行并记 pruned_at；30 天后 prune() 会整行删掉。
                    self.conn.execute(
                        "UPDATE %s SET status='pruned', pruned_at=?, vec=NULL, vec_model='' WHERE id=?" % table,
                        (now, mid))
                except Exception:
                    # 迁移失败/老库没有 pruned_at 或 vec 列时退回只标状态，至少不挡新记忆
                    self.conn.execute("UPDATE %s SET status='pruned' WHERE id=?" % table, (mid,))
            self.conn.commit()

    def _add_memory_row(self, table, key_col, key, text, kind, importance, source_conv, visibility=None,
                        key_facts=None):
        if not write_allowed(self) or (table == "person_memories" and self.is_blocked(key)):
            return None
        text = str(text or "").strip()[:300]
        if not key or not text or looks_junk(text):
            return None
        norm = _norm_text(text)
        if len(norm) < 4:
            return None
        # v0.69.0：关键事实——模型给的优先，没给（或给的为空）就用正则从正文兜底
        facts = facts_to_json(key_facts) if key_facts else ""
        if not facts:
            facts = facts_to_json(guess_key_facts(text))
        with self._lock:
            sql = "SELECT id,text FROM %s WHERE %s=? AND status='active'" % (table, key_col)
            args = [key]
            if table == "person_memories":
                sql += " AND visibility=?"
                args.append(visibility or "private")
            rows = self.conn.execute(sql, args).fetchall()
            for r in rows:
                old = _norm_text(_dec(r["text"]))
                if not old:
                    continue
                if old == norm:
                    return {"action": "skip", "id": r["id"]}
                a, b = len(old), len(norm)
                if min(a, b) / float(max(a, b)) >= 0.6 and (old in norm or norm in old):
                    if b > a:
                        self.conn.execute(
                            "UPDATE %s SET text=?, kind=?, importance=MAX(importance,?), vec=NULL, "
                            "key_facts=CASE WHEN key_facts IS NULL OR key_facts='' THEN ? ELSE key_facts END "
                            "WHERE id=?" % table,
                            (_enc(text), kind, int(importance), facts, r["id"]))
                        self.conn.commit()
                        return {"action": "update", "id": r["id"]}
                    return {"action": "skip", "id": r["id"]}
            # v0.63.0：约定/答应的事一律按「重要」记（用户拍板：承诺不能跟闲聊一个池子）
            if str(kind or "").strip() == "commitment":
                importance = max(4, int(importance or 3))
            cur = self.conn.execute(
                "INSERT INTO %s(%s,text,kind,importance,source_conv,created,key_facts) VALUES(?,?,?,?,?,?,?)"
                % (table, key_col),
                (key, _enc(text), kind, int(importance), source_conv or "", int(time.time()), facts))
            mid = cur.lastrowid
            if table == "person_memories" and visibility is not None:
                self.conn.execute("UPDATE person_memories SET visibility=? WHERE id=?", (visibility, mid))
            self.conn.commit()
        self._prune_scope(table, key_col, key, self._mem_max(table))
        return {"action": "add", "id": mid}

    def _mem_max(self, table):
        key = "memory_group_max" if table == "group_memories" else "memory_person_max"
        try:
            return int(self.get_setting(key) or 0) or (120 if table == "group_memories" else 80)
        except Exception:
            return 120 if table == "group_memories" else 80

    def add_person_memory(self, qq, text, kind="fact", importance=3, source_conv="", visibility="public",
                          key_facts=None):
        return self._add_memory_row("person_memories", "qq", int(qq or 0), text, kind, importance,
                                    source_conv, visibility, key_facts)

    def add_group_memory(self, gid, text, kind="event", importance=3, source_conv="", key_facts=None):
        return self._add_memory_row("group_memories", "gid", int(gid or 0), text, kind, importance, source_conv,
                                    None, key_facts)

    # ---------- v0.64.0：小本本（机器人自己记的，按会话分页） ----------
    NOTEBOOK_MAX = 200
    NOTEBOOK_KINDS = ("note", "todo", "commitment", "question")

    def add_notebook(self, scope, text, kind="note", importance=3, source_conv="", source_uid=0, private=0):
        """加一条小本本；同一本里文字完全一样就更新那条，不新增。
        全局一本，最多 200 条，超了把最不重要/最久没用的归档（status=archived），不物理删。"""
        scope = str(scope or "").strip()[:40]
        body = " ".join(str(text or "").split())[:200]
        if not scope or len(body) < 2:
            return {}
        kind = str(kind or "note").strip().lower()
        if kind not in self.NOTEBOOK_KINDS:
            kind = "note"
        try:
            imp = max(1, min(5, int(importance or 3)))
        except Exception:
            imp = 3
        now = int(time.time())
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT id,text FROM visible_notebook WHERE scope=? AND status='active'", (scope,)).fetchall()
                for r in rows:
                    if " ".join((_dec(r["text"]) or "").split()) == body:
                        self.conn.execute(
                            "UPDATE notebook SET kind=?, importance=?, private=?, last_used=? WHERE id=?",
                            (kind, imp, 1 if private else 0, now, int(r["id"])))
                        self.conn.commit()
                        return {"action": "update", "id": int(r["id"])}
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM visible_notebook WHERE scope=? AND status='active'", (scope,)).fetchone()[0]
                if int(n) >= self.NOTEBOOK_MAX:
                    old = self.conn.execute(
                        "SELECT id FROM visible_notebook WHERE scope=? AND status='active' "
                        "ORDER BY importance ASC, last_used ASC, created ASC LIMIT 1", (scope,)).fetchone()
                    if old:
                        self.conn.execute("UPDATE notebook SET status='archived' WHERE id=?", (int(old[0]),))
                cur = self.conn.execute(
                    "INSERT INTO notebook(scope,text,kind,importance,source_conv,source_uid,private,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (scope, _enc(body), kind, imp, str(source_conv or "")[:80], int(source_uid or 0),
                     1 if private else 0, now))
                self.conn.commit()
                return {"action": "add", "id": int(cur.lastrowid)}
            except Exception:
                return {}

    def list_notebook(self, scope, limit=20, query=""):
        scope = str(scope or "").strip()
        q = " ".join(str(query or "").split()).lower()
        if not scope:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM visible_notebook WHERE scope=? AND status='active' "
                "ORDER BY importance DESC, created DESC LIMIT 200", (scope,)).fetchall()
        out = []
        for r in rows:
            text = _dec(r["text"]) or ""
            if q and q not in text.lower():
                continue
            out.append({"id": int(r["id"]), "scope": scope, "text": text,
                        "kind": str(r["kind"] or "note"), "importance": int(r["importance"] or 3),
                        "source_conv": str(r["source_conv"] or ""), "source_uid": int(r["source_uid"] or 0),
                        "private": int(r["private"] or 0),
                        "created": int(r["created"] or 0), "last_used": int(r["last_used"] or 0),
                        "use_count": int(r["use_count"] or 0)})
            if len(out) >= int(limit):
                break
        return out

    def touch_notebook(self, ids):
        ids = [int(x) for x in (ids or []) if int(x or 0)]
        if not ids:
            return
        with self._lock:
            try:
                self.conn.execute("UPDATE notebook SET last_used=?, use_count=use_count+1 WHERE id IN (%s)"
                                  % ",".join("?" * len(ids)), (int(time.time()), *ids))
                self.conn.commit()
            except Exception:
                pass

    def delete_notebook(self, nid, scope=None):
        try:
            nid = int(nid or 0)
        except Exception:
            return False
        if not nid:
            return False
        with self._lock:
            try:
                if scope:
                    cur = self.conn.execute("UPDATE notebook SET status='deleted' WHERE id=? AND scope=?",
                                            (nid, str(scope)))
                else:
                    cur = self.conn.execute("UPDATE notebook SET status='deleted' WHERE id=?", (nid,))
                self.conn.commit()
                return int(cur.rowcount or 0) > 0
            except Exception:
                return False

    def set_memory_vec(self, table, mid, vec, model=""):
        if table not in ("person_memories", "group_memories", "shared_memories") or not vec:
            return
        blob = struct.pack("<%df" % len(vec), *vec)
        with self._lock:
            self.conn.execute("UPDATE %s SET vec=?, vec_model=? WHERE id=?" % table,
                              (blob, model or "", int(mid)))
            self.conn.commit()

    def memories_with_vec(self, table, key_col, key_val, exclude_id=0, limit=500):
        """取同一范围（同一个人/同一个群/同几个人）里带向量的有效记忆，用于语义去重。"""
        if table not in ("person_memories", "group_memories", "shared_memories"):
            return []
        if key_col not in ("qq", "gid", "people"):
            return []
        out = []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text,kind,importance,created,vec,%s AS k FROM %s "
                "WHERE status='active' AND %s=? AND vec IS NOT NULL AND id<>? ORDER BY created ASC LIMIT ?"
                % (key_col, table, key_col),
                (key_val, int(exclude_id or 0), int(limit))).fetchall()
        for r in rows:
            blob = r["vec"]
            try:
                vec = list(struct.unpack("<%df" % (len(blob) // 4), blob))
            except Exception:
                continue
            out.append({"id": int(r["id"]), "text": _dec(r["text"]) or "", "kind": r["kind"],
                        "importance": int(r["importance"] or 3), "created": int(r["created"] or 0),
                        "vec": vec, "key": r["k"]})
        return out

    def update_memory_row(self, table, mid, text=None, importance=None, key_facts=None):
        """面板/整理器改一条记忆的正文或重要度（不改状态、不清向量）。v0.69.0 也能更新关键事实。"""
        # v0.63.0：已经是 commitment 的那条，重要度不许被改成 3 以下
        if importance is not None:
            try:
                with self._lock:
                    _r = self.conn.execute("SELECT kind FROM %s WHERE id=?" % table, (int(mid),)).fetchone()
                if _r and str(_r["kind"] or "") == "commitment":
                    importance = max(4, int(importance))
            except Exception:
                pass
        if table not in ("person_memories", "group_memories", "shared_memories"):
            return False
        with self._lock:
            try:
                _facts = facts_to_json(key_facts) if key_facts is not None else None
                self.conn.execute(
                    "UPDATE %s SET text=COALESCE(?,text), importance=COALESCE(?,importance), "
                    "key_facts=COALESCE(?,key_facts), vec=CASE WHEN ? IS NULL THEN vec ELSE NULL END, "
                    "vec_model=CASE WHEN ? IS NULL THEN vec_model ELSE '' END, "
                    "content_updated_at=CASE WHEN ? IS NULL THEN content_updated_at ELSE ? END, last_used=? WHERE id=?" % table,
                    ((_enc(text) if text is not None else None),
                     (int(importance) if importance is not None else None), _facts,
                     text, text, text, int(time.time()), int(time.time()), int(mid)))
                self.conn.commit()
                return True
            except Exception:
                return False

    def merge_memory_into(self, table, keep_id, drop_id, text=None, importance=None):
        """把两条相近记忆合并成一条：保留 keep，另一条标 pruned（不物理删，30 天后清理进程会删）。"""
        if table not in ("person_memories", "group_memories", "shared_memories"):
            return False
        now = int(time.time())
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE %s SET text=COALESCE(?,text), importance=COALESCE(?,importance), last_used=? WHERE id=?" % table,
                    ((_enc(text) if text is not None else None),
                     (int(importance) if importance is not None else None), now, int(keep_id)))
                self.conn.execute(
                    "UPDATE %s SET status='pruned', pruned_at=?, vec=NULL, vec_model='' WHERE id=?" % table,
                    (now, int(drop_id)))
                self.conn.commit()
                return True
            except Exception:
                try:
                    self.conn.execute("UPDATE %s SET status='pruned' WHERE id=?" % table, (int(drop_id),))
                    self.conn.commit()
                    return True
                except Exception:
                    return False

    def set_kb_vec(self, cid, vec, model=""):
        if not vec:
            return
        blob = struct.pack("<%df" % len(vec), *vec)
        with self._lock:
            self.conn.execute("UPDATE kb_chunks SET vec=?, vec_model=? WHERE id=?",
                              (blob, model or "", int(cid)))
            self.conn.commit()

    def kb_chunks_missing_vec(self, limit=500):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text FROM kb_chunks WHERE vec IS NULL LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r["id"], "text": _dec(r["text"])} for r in rows]

    def memories_missing_vec(self, limit=300):
        out = []
        with self._lock:
            for tbl in ("person_memories", "group_memories", "shared_memories"):
                rows = self.conn.execute(
                    "SELECT id,text FROM %s WHERE status='active' AND vec IS NULL LIMIT ?" % tbl,
                    (int(limit),)).fetchall()
                for r in rows:
                    out.append({"table": tbl, "id": r["id"], "text": _dec(r["text"])})
        return out

    def _score_rows(self, rows, tokens, qvec=None):
        now = int(time.time())
        qn = math.sqrt(sum(x * x for x in qvec)) if qvec else 0.0
        out = []
        for r in rows:
            text = _dec(r["text"])
            toks = _plain_tokens(text)
            overlap = len(tokens & toks) if tokens else 0
            imp = int(r["importance"] or 3)
            # v0.63.0：约定/答应的事永远排在前面（用户拍板：承诺要单独提出来长期存）
            if str(r["kind"] or "") == "commitment":
                imp = max(imp, 5)
            fresh = 1 if now - int(r["created"] or 0) < 7 * 86400 else 0
            used = 1 if now - int(r["last_used"] or 0) < 3 * 86400 else 0
            cos = 0.0
            if qvec and r["vec"] and qn:
                try:
                    n = len(qvec)
                    v = struct.unpack("<%df" % n, r["vec"][:n * 4])
                    vn = math.sqrt(sum(x * x for x in v)) or 1.0
                    cos = sum(a * b for a, b in zip(v, qvec)) / (vn * qn)
                except Exception:
                    cos = 0.0
            if qvec and cos >= 0.28:
                score = 0.6 * cos + 0.2 * min(overlap, 3) / 3.0 + 0.12 * imp / 5.0 + 0.08 * fresh
                keep = True
            else:
                score = overlap * 3 + imp + fresh + used
                keep = overlap > 0
            out.append((score, overlap, text, r, cos, keep))
        out.sort(key=lambda x: (x[5], x[0], x[4]), reverse=True)
        return out

    def search_person_memories(self, qq, keywords, limit=6, include_private=False, query_vec=None):
        tokens = _plain_tokens(" ".join(keywords or []))
        with self._lock:
            sql = "SELECT * FROM visible_person_memories WHERE qq=? AND status='active'"
            args = [int(qq or 0)]
            if not include_private:
                sql += " AND visibility='public'"
            rows = self.conn.execute(sql + " ORDER BY importance DESC, created DESC LIMIT 200", args).fetchall()
            scored = self._score_rows(rows, tokens, query_vec)
            picked = [x for x in scored if x[5]][:limit]
            ids = [x[3]["id"] for x in picked]
            if ids:
                self.record_memory_retrieval("person_memories", ids)
                self.conn.commit()
        return [{"id": x[3]["id"], "text": x[2], "kind": x[3]["kind"], "importance": x[3]["importance"],
                 "created": int(x[3]["created"] or 0), "key_facts": str(x[3]["key_facts"] or "")} for x in picked]

    def search_group_memories(self, gid, keywords, limit=5, query_vec=None):
        tokens = _plain_tokens(" ".join(keywords or []))
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM visible_group_memories WHERE gid=? AND status='active' ORDER BY importance DESC, created DESC LIMIT 200",
                (int(gid or 0),)).fetchall()
            scored = self._score_rows(rows, tokens, query_vec)
            picked = [x for x in scored if x[5]][:limit]
            ids = [x[3]["id"] for x in picked]
            if ids:
                self.record_memory_retrieval("group_memories", ids)
                self.conn.commit()
        return [{"id": x[3]["id"], "text": x[2], "kind": x[3]["kind"], "importance": x[3]["importance"],
                 "created": int(x[3]["created"] or 0), "key_facts": str(x[3]["key_facts"] or "")} for x in picked]

    def list_person_memories(self, qq=None, limit=100):
        with self._lock:
            if qq:
                rows = self.conn.execute(
                    "SELECT m.*, p.name FROM visible_person_memories m LEFT JOIN visible_people p ON p.qq=m.qq "
                    "WHERE m.qq=? AND m.status='active' ORDER BY m.importance DESC, m.created DESC LIMIT ?",
                    (int(qq), int(limit))).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT m.*, p.name FROM visible_person_memories m LEFT JOIN visible_people p ON p.qq=m.qq "
                    "WHERE m.status='active' ORDER BY m.created DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r["id"], "scope": "person", "qq": r["qq"], "name": r["name"] or str(r["qq"]),
                 "text": _dec(r["text"]), "kind": r["kind"], "importance": r["importance"],
                 "visibility": r["visibility"], "created": r["created"],
                 "content_updated_at": r["content_updated_at"],
                 "key_facts": str(r["key_facts"] or ""), "protected": int(r["protected"] or 0), "last_mentioned_at": int(r["last_mentioned_at"] or 0), "last_background_at": int(r["last_background_at"] or 0)} for r in rows]

    def list_group_memories(self, gid=None, limit=100):
        with self._lock:
            if gid:
                rows = self.conn.execute(
                    "SELECT * FROM visible_group_memories WHERE gid=? AND status='active' "
                    "ORDER BY importance DESC, created DESC LIMIT ?", (int(gid), int(limit))).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM visible_group_memories WHERE status='active' ORDER BY created DESC LIMIT ?",
                    (int(limit),)).fetchall()
        return [{"id": r["id"], "scope": "group", "gid": r["gid"], "name": "群%d" % r["gid"],
                 "text": _dec(r["text"]), "kind": r["kind"], "importance": r["importance"],
                 "created": r["created"], "content_updated_at": r["content_updated_at"],
                 "key_facts": str(r["key_facts"] or ""), "protected": int(r["protected"] or 0), "last_mentioned_at": int(r["last_mentioned_at"] or 0), "last_background_at": int(r["last_background_at"] or 0)} for r in rows]

    def update_person_memory(self, mid, text):
        with self._lock:
            self.conn.execute("UPDATE person_memories SET text=?, vec=NULL WHERE id=?",
                              (_enc(str(text)[:300]), int(mid)))
            self.conn.commit()

    def update_group_memory(self, mid, text):
        with self._lock:
            self.conn.execute("UPDATE group_memories SET text=?, vec=NULL WHERE id=?",
                              (_enc(str(text)[:300]), int(mid)))
            self.conn.commit()

    def delete_person_memory(self, mid):
        with self._lock:
            self.conn.execute("UPDATE person_memories SET status='deleted' WHERE id=?", (int(mid),))
            self.conn.commit()

    def delete_group_memory(self, mid):
        with self._lock:
            self.conn.execute("UPDATE group_memories SET status='deleted' WHERE id=?", (int(mid),))
            self.conn.commit()

    def recent_songs(self, conv, limit=3):
        """最近发过的歌曲卡片（从 attachments 里读，正文不留标记）。"""
        out = []
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT text, attachments, ts FROM visible_messages WHERE conv=? AND direction='out' "
                    "AND attachments LIKE '%\"song\"%' ORDER BY id DESC LIMIT ?",
                    (conv, int(limit))).fetchall()
            for r in rows:
                try:
                    atts = json.loads(r["attachments"] or "[]")
                except Exception:
                    continue
                for a in atts:
                    if isinstance(a, dict) and a.get("type") == "song":
                        out.append({"name": a.get("name") or "", "artist": a.get("artist") or "",
                                    "ts": r["ts"], "text": r["text"] or ""})
        except Exception:
            pass
        return out

    def recent_sticker_notes(self, conv, limit=2):
        """最近发过的表情包画面备注；只给系统提示当背景，不塞进对话正文。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,attachments FROM visible_messages WHERE conv=? AND direction='out' "
                "AND attachments LIKE '%sticker%' ORDER BY id DESC LIMIT ?", (conv, int(limit))).fetchall()
        out = []
        for r in rows:
            try:
                for a in json.loads(r["attachments"] or "[]"):
                    if isinstance(a, dict) and a.get("note"):
                        out.append({"ts": r["ts"], "note": str(a["note"])[:100]})
                        break
            except Exception:
                continue
        return out

    def has_message_id(self, message_id):
        try:
            mid = int(message_id or 0)
        except Exception:
            return False
        if not mid:
            return False
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM visible_messages WHERE message_id=? LIMIT 1", (mid,)).fetchone()
        return bool(row)

    # ---- v0.55.0：持久去重（以前只靠内存 set，重启就忘） ----
    def seen_add(self, mid, ts=None):
        """记一条已处理过的 message_id；7 天后由 seen_prune 清掉。"""
        try:
            mid = int(mid or 0)
        except Exception:
            return
        if not mid:
            return
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO seen_messages(mid,ts) VALUES(?,?)",
                (mid, int(ts or time.time())))
            self.conn.commit()

    def seen_has(self, mid):
        try:
            mid = int(mid or 0)
        except Exception:
            return False
        if not mid:
            return False
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM seen_messages WHERE mid=? LIMIT 1", (mid,)).fetchone()
        return bool(row)

    def seen_recent(self, limit=5000):
        with self._lock:
            rows = self.conn.execute(
                "SELECT mid FROM seen_messages ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [int(r[0]) for r in rows]

    def seen_prune(self, days=7):
        cut = int(time.time()) - int(days) * 86400
        with self._lock:
            cur = self.conn.execute("DELETE FROM seen_messages WHERE ts < ?", (cut,))
            self.conn.commit()
            return cur.rowcount

    def messages_after(self, conv, after_id, limit=60):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,ts,direction,sender_id,sender_name,message_id,text,raw,attachments FROM visible_messages "
                "WHERE conv=? AND id>? ORDER BY id ASC LIMIT ?", (conv, int(after_id or 0), int(limit))).fetchall()
        return [dict(r, text=_dec(r["text"])) for r in rows]

    def migrate_memory_v2(self):
        """把老 memories 表里还有用的搬进新表，垃圾（称呼玩笑/口癖/模型名）留给老表不管。"""
        if str(self.get_setting("memv2_done") or "") == "1":
            return {"skipped": True}
        added = 0
        dropped = 0
        try:
            rows = self.conn.execute("SELECT * FROM visible_memories").fetchall()
        except Exception:
            rows = []
        for r in rows:
            try:
                d = dict(r)
                text = _dec(d.get("text") or "")
                world = d.get("world") or ("group" if str(d.get("conv") or "").startswith("g:") else "private")
                if world == "group" and int(d.get("group_id") or 0):
                    res = self.add_group_memory(int(d["group_id"]), text, "event", 3, d.get("conv") or "")
                elif int(d.get("person_id") or 0):
                    if d.get("person_name"):
                        self.upsert_person(int(d["person_id"]), d["person_name"])
                    res = self.add_person_memory(int(d["person_id"]), text, "fact", 3, d.get("conv") or "")
                else:
                    dropped += 1
                    continue
                if res and res.get("action") in ("add", "update"):
                    added += 1
                else:
                    dropped += 1
            except Exception:
                dropped += 1
        # 只从最近 40 条开始整理，别把几个月前的旧聊天再翻一遍
        try:
            for r in self.conn.execute("SELECT conv, MAX(id) m FROM visible_messages GROUP BY conv").fetchall():
                key = "memv2_upto:" + r["conv"]
                if not self.get_setting(key):
                    self.set_setting(key, str(max(0, int(r["m"]) - 40)))
        except Exception:
            pass
        self.set_setting("memv2_done", "1")
        _log("memory", "v2 迁移：迁入 %d 条，跳过/垃圾 %d 条" % (added, dropped))
        return {"added": added, "dropped": dropped}

    # ---------- 用量 ----------
    def add_usage(self, model_id, prompt_tokens=0, completion_tokens=0, cache_hit_tokens=0, ok=True):
        """记一次模型调用。ok=False 是失败尝试（v0.65.2 起才记）：只占一个请求数，不带 token。"""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO usage_records(ts,model_id,prompt_tokens,completion_tokens,cache_hit_tokens,ok) VALUES(?,?,?,?,?,?)",
                    (int(time.time()), str(model_id or ""), int(prompt_tokens or 0), int(completion_tokens or 0),
                     int(cache_hit_tokens or 0), 1 if ok else 0))
                self.conn.commit()
        except Exception as e:
            # 记账失败以前是完全静默的，会直接造成用量页少算
            _log("usage", "记账失败 model=%s p=%s c=%s hit=%s：%r" % (
                model_id, prompt_tokens, completion_tokens, cache_hit_tokens, e))

    def usage_rows_since(self, since, model_ids=None):
        """按时间取原始用量明细（账目折算用；只读，不写库）。"""
        try:
            sql = ("SELECT ts, model_id, prompt_tokens, completion_tokens, "
                   "COALESCE(cache_hit_tokens,0) cache_hit_tokens FROM usage_records WHERE ts >= ?")
            args = [int(since)]
            ids = [str(x) for x in (model_ids or []) if str(x)]
            if ids:
                sql += " AND model_id IN (%s)" % ",".join("?" for _ in ids)
                args.extend(ids)
            sql += " ORDER BY ts"
            with self._lock:
                rows = self.conn.execute(sql, tuple(args)).fetchall()
            return [{k: r[k] for k in r.keys()} for r in rows]
        except Exception as e:
            _log("usage", "读用量明细失败：%r" % (e,))
            return []

    def usage_stats(self):
        now = time.time()
        def day_start(offset=0):
            t = time.localtime(now - offset * 86400)
            return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
        start_week = day_start(time.localtime(now).tm_wday)
        start_month = int(time.mktime((time.localtime(now).tm_year, time.localtime(now).tm_mon, 1, 0, 0, 0, 0, 0, -1)))
        periods = {"today": day_start(), "yesterday": day_start(1), "week": start_week, "month": start_month}
        out = {}
        with self._lock:
            for name, start in periods.items():
                end = None
                if name == "yesterday":
                    end = day_start(0)
                rows = self.conn.execute(
                    "SELECT model_id, SUM(prompt_tokens) p, SUM(completion_tokens) c FROM usage_records "
                    "WHERE ts >= ?" + (" AND ts < ?" if end else "") + " GROUP BY model_id",
                    (start, end) if end else (start,)).fetchall()
                out[name] = [{"model": r["model_id"] or "-", "prompt": r["p"] or 0, "completion": r["c"] or 0,
                              "total": (r["p"] or 0) + (r["c"] or 0)} for r in rows]
        return out

    # ---------- 会话列表 ----------
    def conversations(self, limit=100):
        with self._lock:
            rows = self.conn.execute(
                "SELECT conv, COUNT(*) c, MAX(ts) last_ts FROM visible_messages GROUP BY conv ORDER BY last_ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        names = self.conv_names()
        return [{"conv": r["conv"], "name": names.get(r["conv"], r["conv"]), "count": r["c"], "last_ts": r["last_ts"]}
                for r in rows]

    # ---------- 知识库 ----------
    def add_kb_doc(self, scope, name, text):
        text = (text or "").strip()
        if not text:
            return 0
        chunks = []
        buf = ""
        for para in text.splitlines():
            para = para.strip()
            if not para:
                continue
            if len(buf) + len(para) > 700 and buf:
                chunks.append(buf)
                buf = ""
            buf += para + "\n"
        if buf.strip():
            chunks.append(buf)
        with self._lock:
            cur = self.conn.execute("INSERT INTO kb_docs(scope,name,created_at) VALUES(?,?,?)",
                                    (scope, (name or "资料")[:80], int(time.time())))
            doc_id = cur.lastrowid
            for i, ch in enumerate(chunks):
                cur2 = self.conn.execute("INSERT INTO kb_chunks(doc_id,idx,text) VALUES(?,?,?)", (doc_id, i, _enc(ch)))
                cid = cur2.lastrowid
                for tok in tokenize(ch):
                    self.conn.execute("INSERT INTO kb_tokens(chunk_id,token) VALUES(?,?)", (cid, tok))
            self.conn.commit()
        return len(chunks)

    def del_kb_doc(self, scope, name):
        """按名字删一份知识库资料（v0.59.0）；删不到返回 False。"""
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            row = self.conn.execute("SELECT id FROM kb_docs WHERE scope=? AND name=? ORDER BY id DESC LIMIT 1",
                                    (scope, name[:80])).fetchone()
            if not row:
                return False
            did = int(row[0])
            ids = [r[0] for r in self.conn.execute("SELECT id FROM kb_chunks WHERE doc_id=?", (did,)).fetchall()]
            for cid in ids:
                self.conn.execute("DELETE FROM kb_tokens WHERE chunk_id=?", (cid,))
            self.conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
            self.conn.execute("DELETE FROM kb_docs WHERE id=?", (did,))
            self.conn.commit()
        return True

    def search_kb(self, scope, keywords, limit=3, query_vec=None):
        """知识库混合检索：向量 0.7 + 词面 0.3；按文档聚合，一份资料只出一次。

        v0.61.0：门槛提高（不再沾一个字就塞给模型），同一份资料只返回最相关的那段，
        并把卡片头（分类/一句话/要点/标签）一起带出来，检索结果直接可读。
        """
        tokens = _plain_tokens(" ".join(keywords or []))
        if not tokens and not query_vec:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.id,c.doc_id,c.idx,c.text,c.vec,d.name,d.topic,d.summary,d.points,d.tags,d.kind,d.source "
                "FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id "
                "WHERE d.scope=? ORDER BY c.id DESC LIMIT 3000", (scope,)).fetchall()
        qn = math.sqrt(sum(x * x for x in query_vec)) if query_vec else 0.0
        best = {}
        for r in rows:
            text = _dec(r["text"])
            toks = _plain_tokens(text)
            overlap = len(tokens & toks) if tokens else 0
            ratio = (overlap / float(len(tokens))) if tokens else 0.0
            cos = 0.0
            if query_vec and r["vec"] and qn:
                try:
                    n = len(query_vec)
                    v = struct.unpack("<%df" % n, r["vec"][:n * 4])
                    vn = math.sqrt(sum(x * x for x in v)) or 1.0
                    cos = sum(a * b for a, b in zip(v, query_vec)) / (vn * qn)
                except Exception:
                    cos = 0.0
            need = max(1, int(math.ceil(len(tokens) * 0.4))) if tokens else 99
            if query_vec and cos >= 0.35:
                score = 0.75 * cos + 0.25 * min(ratio, 1.0)
            elif overlap >= need:
                score = min(ratio, 1.0)
            else:
                continue
            meta = _plain_tokens("%s %s %s" % (r["name"] or "", _dec(r["topic"] or ""), _dec(r["tags"] or "")))
            if tokens and meta:
                score += 0.1 * min(len(tokens & meta), 3) / 3.0
            did = int(r["doc_id"])
            cur = best.get(did)
            if cur is None or score > cur["score"] or (score == cur["score"] and int(r["idx"] or 0) < cur["idx"]):
                best[did] = {"id": did, "chunk_id": int(r["id"]), "idx": int(r["idx"] or 0),
                             "text": text, "name": r["name"] or "", "topic": _dec(r["topic"] or ""),
                             "summary": _dec(r["summary"] or ""), "points": self._kb_points(r["points"]),
                             "tags": _dec(r["tags"] or ""), "kind": r["kind"] or "doc",
                             "source": r["source"] or "", "score": round(score, 3)}
        out = sorted(best.values(), key=lambda x: x["score"], reverse=True)[:int(limit)]
        if out:
            try:
                with self._lock:
                    self.conn.executemany("UPDATE kb_docs SET hits=hits+1 WHERE id=?",
                                          [(x["id"],) for x in out])
                    self.conn.commit()
            except Exception:
                pass
        return out

    def list_kb(self, scope=None, limit=50):
        return [{"id": x["id"], "name": x["name"], "scope": x["scope"], "created_at": x["created_at"],
                 "chunks": x["chunks"], "topic": x["topic"]}
                for x in self.kb_list(scope, limit=limit)]

    # ---------- 知识库 v2：结构化卡片 ----------
    @staticmethod
    def _kb_split(text):
        """按行切成 <=700 字的小段，供检索用；空行丢掉。"""
        chunks = []
        buf = ""
        for para in str(text or "").splitlines():
            para = para.strip()
            if not para:
                continue
            if len(buf) + len(para) > 700 and buf:
                chunks.append(buf)
                buf = ""
            buf += para + "\n"
        if buf.strip():
            chunks.append(buf)
        return chunks

    @staticmethod
    def _kb_points(raw):
        """把 points 字段（JSON 数组）解成 list；老数据/坏数据返回空。"""
        if not raw:
            return []
        try:
            v = json.loads(_dec(raw))
            if isinstance(v, list):
                return [str(x)[:120] for x in v if str(x).strip()][:10]
        except Exception:
            pass
        return []

    def kb_upsert_doc(self, scope, name, text, kind="doc", topic="", summary="", points=None,
                      tags=None, source="", source_id="", raw="", quality=0):
        """写入/更新一张知识库卡片；同 scope + source_id 覆盖旧的（同一个视频不会越堆越多）。

        points/tags 传 list；raw 是原始转写/文件正文，只留档、不进检索。
        返回 doc_id（0 表示没写）。
        """
        text = (text or "").strip()
        if not text:
            return 0
        pts = points if isinstance(points, (list, tuple)) else []
        tgs = tags if isinstance(tags, (list, tuple)) else [x for x in str(tags or "").split("、") if x]
        pts_e = _enc(json.dumps([str(x)[:100] for x in pts][:10], ensure_ascii=False))
        tgs_e = _enc("、".join(str(x)[:16] for x in tgs if str(x).strip())[:160])
        summary_e = _enc(str(summary or "")[:200])
        raw_e = _enc(str(raw or "")[:8000]) if raw else ""
        nm = (name or "资料")[:80]
        tp = str(topic or "")[:20]
        now = int(time.time())
        with self._lock:
            old = None
            if source_id:
                r = self.conn.execute(
                    "SELECT id FROM kb_docs WHERE scope=? AND source_id=? ORDER BY id DESC LIMIT 1",
                    (scope, source_id)).fetchone()
                if r:
                    old = int(r[0])
            if old:
                for cid in [x[0] for x in self.conn.execute(
                        "SELECT id FROM kb_chunks WHERE doc_id=?", (old,)).fetchall()]:
                    self.conn.execute("DELETE FROM kb_tokens WHERE chunk_id=?", (cid,))
                self.conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (old,))
                self.conn.execute(
                    "UPDATE kb_docs SET name=?,kind=?,topic=?,summary=?,points=?,tags=?,source=?,"
                    "source_id=?,quality=?,raw=?,updated_at=? WHERE id=?",
                    (nm, kind, tp, summary_e, pts_e, tgs_e, source, source_id, int(quality), raw_e, now, old))
                doc_id = old
            else:
                cur = self.conn.execute(
                    "INSERT INTO kb_docs(scope,name,created_at,kind,topic,summary,points,tags,source,"
                    "source_id,quality,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scope, nm, now, kind, tp, summary_e, pts_e, tgs_e, source, source_id,
                     int(quality), raw_e, now))
                doc_id = int(cur.lastrowid)
            for i, ch in enumerate(self._kb_split(text)):
                cur2 = self.conn.execute("INSERT INTO kb_chunks(doc_id,idx,text) VALUES(?,?,?)",
                                         (doc_id, i, _enc(ch)))
                for tok in tokenize(ch):
                    self.conn.execute("INSERT INTO kb_tokens(chunk_id,token) VALUES(?,?)",
                                      (cur2.lastrowid, tok))
            self.conn.commit()
        return doc_id

    def kb_list(self, scope=None, topic=None, keyword="", limit=200):
        """列出知识库卡片（面板/工具用）。keyword 在解密后的名称/一句话/要点/标签里找。"""
        sql = ("SELECT d.id,d.name,d.scope,d.created_at,d.kind,d.topic,d.summary,d.points,d.tags,"
               "d.source,d.source_id,d.quality,d.updated_at,d.raw,d.hits,"
               "(SELECT COUNT(*) FROM kb_chunks WHERE doc_id=d.id) chunks FROM kb_docs d")
        where, args = [], []
        if scope:
            where.append("d.scope=?"); args.append(scope)
        if topic:
            where.append("d.topic=?"); args.append(topic)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.updated_at DESC, d.id DESC LIMIT 600"
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        kw = str(keyword or "").strip().lower()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = _dec(d.get("summary") or "")
            d["points"] = self._kb_points(d.get("points") or "")
            d["tags"] = _dec(d.get("tags") or "")
            d["topic"] = _dec(d.get("topic") or "") or "其他"
            d.pop("raw", None)
            if kw:
                blob = " ".join([str(d.get("name") or ""), d["summary"], d["tags"],
                                 " ".join(d["points"])]).lower()
                if kw not in blob:
                    continue
            out.append(d)
        return out[:int(limit)]

    def kb_stats(self, scope=None):
        """知识库概览：总数/分类计数/字数/最近更新时间。"""
        sql = ("SELECT topic,kind,summary,points,updated_at FROM kb_docs"
               + (" WHERE scope=?" if scope else ""))
        with self._lock:
            rows = self.conn.execute(sql, ((scope,) if scope else ())).fetchall()
        topics, kinds, total = {}, {}, 0
        chars, newest = 0, 0
        for r in rows:
            total += 1
            tp = _dec(r["topic"] or "") or "其他"
            topics[tp] = topics.get(tp, 0) + 1
            kd = str(r["kind"] or "doc")
            kinds[kd] = kinds.get(kd, 0) + 1
            chars += len(_dec(r["summary"] or "")) + len(str(r["points"] or ""))
            newest = max(newest, int(r["updated_at"] or 0))
        return {"total": total, "topics": sorted(topics.items(), key=lambda x: -x[1]),
                "kinds": kinds, "chars": chars, "newest": newest}

    def kb_topics(self, scope=None):
        return self.kb_stats(scope)["topics"]

    def kb_update(self, doc_id, topic=None, quality=None, summary=None, tags=None, name=None):
        """面板/工具改卡片头（不动正文）。返回是否改到。"""
        sets, args = [], []
        if topic is not None:
            sets.append("topic=?"); args.append(str(topic)[:20])
        if quality is not None:
            sets.append("quality=?"); args.append(int(quality))
        if summary is not None:
            sets.append("summary=?"); args.append(_enc(str(summary)[:200]))
        if tags is not None:
            if isinstance(tags, (list, tuple)):
                tags = "、".join(str(x)[:16] for x in tags)
            sets.append("tags=?"); args.append(_enc(str(tags)[:160]))
        if name is not None:
            sets.append("name=?"); args.append(str(name)[:80])
        if not sets:
            return False
        sets.append("updated_at=?"); args.append(int(time.time()))
        with self._lock:
            cur = self.conn.execute("UPDATE kb_docs SET %s WHERE id=?" % ",".join(sets),
                                    args + [int(doc_id)])
            self.conn.commit()
            return bool(cur.rowcount)

    def kb_prune(self, scope, keep=300):
        """资料太多时按"质量低 + 最久没更新"删到 keep 份以内。返回删了几份。"""
        with self._lock:
            n = int(self.conn.execute("SELECT COUNT(*) FROM kb_docs WHERE scope=?", (scope,)).fetchone()[0])
            if n <= int(keep):
                return 0
            rows = self.conn.execute(
                "SELECT id FROM kb_docs WHERE scope=? ORDER BY quality ASC, updated_at ASC, id ASC LIMIT ?",
                (scope, n - int(keep))).fetchall()
            ids = [int(r[0]) for r in rows]
            for did in ids:
                self.conn.execute(
                    "DELETE FROM kb_tokens WHERE chunk_id IN (SELECT id FROM kb_chunks WHERE doc_id=?)",
                    (did,))
                self.conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
                self.conn.execute("DELETE FROM kb_docs WHERE id=?", (did,))
            self.conn.commit()
        return len(ids)

    def delete_kb(self, doc_id):
        with self._lock:
            self.conn.execute(
                "DELETE FROM kb_tokens WHERE chunk_id IN (SELECT id FROM kb_chunks WHERE doc_id=?)",
                (int(doc_id),))
            self.conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (int(doc_id),))
            self.conn.execute("DELETE FROM kb_docs WHERE id=?", (int(doc_id),))
            self.conn.commit()

    def get_card(self, conv):
        with self._lock:
            r = self.conn.execute("SELECT * FROM visible_person_cards WHERE conv=?", (conv,)).fetchone()
        if not r:
            return None
        d = dict(r); d["card"] = _dec(d["card"]); return d

    def recent_msgs_for_person(self, qq, limit=12):
        """某个人最近的发言/对话（给印象卡刷新兜底：混合版卡是 p:/q: 编号，消息可能不在同一个 conv 键下）。"""
        qq = int(qq or 0)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,sender_id,sender_name,direction,text,ts,attachments FROM visible_messages "
                "WHERE sender_id=? OR conv=? OR conv=? ORDER BY id DESC LIMIT ?",
                (qq, "q:%d" % qq, "p:%d" % qq, int(limit))).fetchall()
        return [dict(r) for r in reversed(rows) if not self.sleep_tainted(r["id"], text=_dec(r["text"]), attachments=r["attachments"])]

    def stale_cards(self, days=3, limit=8):
        """印象卡「最近状态」为空或太旧的会话（v0.55.7 定期刷新用）。"""
        cut = int(time.time()) - int(days) * 86400
        with self._lock:
            rows = self.conn.execute(
                "SELECT conv,name,recent,updated_at FROM visible_person_cards "
                "WHERE ifnull(recent,'')='' OR updated_at < ? ORDER BY updated_at ASC LIMIT ?",
                (cut, int(limit))).fetchall()
        return [{"conv": r["conv"], "name": r["name"], "recent": _dec(r["recent"]) or "",
                 "updated_at": int(r["updated_at"] or 0)} for r in rows]

    def recent_msgs_by_conv(self, conv, limit=12):
        """某个会话最近 N 条消息（给印象卡刷新用，按时间正序返回）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,sender_id,sender_name,direction,text,ts,attachments FROM visible_messages WHERE conv=? "
                "ORDER BY id DESC LIMIT ?", (conv, int(limit))).fetchall()
        return [dict(r) for r in reversed(rows) if not self.sleep_tainted(r["id"], text=_dec(r["text"]), attachments=r["attachments"])]

    def upsert_card(self, conv, name, card, recent=None, evidence=None):
        """card=稳定档案，recent=最近状态；不传就保留旧值，避免半截更新把另一边清空。"""
        with self._lock:
            row = self.conn.execute("SELECT recent,evidence FROM visible_person_cards WHERE conv=?", (conv,)).fetchone()
            old_recent = _dec(row["recent"]) if row else ""
            old_evidence = int(row["evidence"] or 0) if row else 0
            new_recent = old_recent if recent is None else (recent or "")[:600]
            new_evidence = old_evidence if evidence is None else int(evidence or 0)
            self.conn.execute(
                "INSERT INTO person_cards(conv,name,card,recent,evidence,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(conv) DO UPDATE SET name=excluded.name, card=excluded.card, recent=excluded.recent, "
                "evidence=excluded.evidence, updated_at=excluded.updated_at",
                (conv, (name or "")[:40], _enc((card or "")[:3000]), _enc(new_recent),
                 new_evidence, int(time.time())))
            self.conn.commit()

    def get_relation(self, conv):
        with self._lock:
            r = self.conn.execute("SELECT * FROM relations WHERE conv=?", (conv,)).fetchone()
        if not r:
            return None
        d = dict(r); d["grudge"] = _dec(d["grudge"]); return d

    def upsert_relation(self, conv, name, favor, grudge):
        with self._lock:
            self.conn.execute(
                "INSERT INTO relations(conv,name,favor,grudge,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(conv) DO UPDATE SET name=excluded.name, favor=excluded.favor, grudge=excluded.grudge, updated_at=excluded.updated_at",
                (conv, (name or "")[:40], int(favor), _enc((grudge or "")[:500]), int(time.time())))
            self.conn.commit()

    def list_cards(self, limit=50):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM visible_person_cards ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["card"] = _dec(d["card"]); out.append(d)
        return out

    def list_relations(self, limit=50):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM relations ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["grudge"] = _dec(d.get("grudge"))
            out.append(d)
        return out

    # ---------- 记忆 v3：按人好感 / 印象卡 / 记仇 / 群气氛 ----------
    def get_person_relation(self, qq):
        with self._lock:
            r = self.conn.execute("SELECT * FROM person_relations WHERE qq=?", (int(qq or 0),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["reason"] = _dec(d.get("reason") or "")
        return d

    def get_profile(self, qq):
        row = self.get_card("q:%d" % int(qq or 0))
        if not row:
            return None
        row["recent"] = _dec(row.get("recent") or "")
        return row

    def apply_favor(self, qq, delta, reason="", conv="", half_days=90, tier=""):
        """好感按人更新：先按 90 天半衰期衰减到现在，再加事件；
        正向分三档预算（闲聊 1/天、认真 3/天、大事 3/次），互不挤占；不做到期跳档。"""
        qq = int(qq or 0)
        if not qq:
            return 50
        try:
            delta = max(-3, min(3, int(delta or 0)))
        except Exception:
            delta = 0
        reason = " ".join(str(reason or "").split())[:80]
        now = int(time.time())
        with self._lock:
            row = self.conn.execute("SELECT favor,updated_at FROM person_relations WHERE qq=?", (qq,)).fetchone()
            stored = float(row["favor"]) if row else 50.0
            old = self.effective_favor(stored, row["updated_at"] if row else 0, half_days, now)
            if delta > 0:
                tier = str(tier or ("casual" if delta <= 1 else ("meaningful" if delta == 2 else "major")))
                budget = {"casual": 1, "meaningful": 3, "major": 3}.get(tier, 1)
                try:
                    day0 = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
                except Exception:
                    day0 = now - (now % 86400)
                used = self.conn.execute(
                    "SELECT COALESCE(SUM(delta),0) FROM favor_history WHERE qq=? AND ts>=? AND delta>0 AND tier=?",
                    (qq, day0, tier)).fetchone()[0]
                delta = min(delta, max(0, int(budget) - int(used)))
            else:
                tier = str(tier or "negative")
            if delta == 0:
                return int(round(old))
            new = int(round(max(0.0, min(100.0, old + delta))))
            self.conn.execute(
                "INSERT INTO person_relations(qq,favor,reason,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(qq) DO UPDATE SET favor=excluded.favor, reason=excluded.reason, updated_at=excluded.updated_at",
                (qq, new, _enc(reason), now))
            self.conn.execute(
                "INSERT INTO favor_history(qq,favor,delta,reason,conv,tier,ts) VALUES(?,?,?,?,?,?,?)",
                (qq, new, int(round(new - old)), _enc(reason), conv or "", tier, now))
            self.conn.commit()
        return new

    def set_favor(self, qq, favor, reason="手动调整", conv=""):
        """面板手动改好感（不受 ±3 限制），也写流水。"""
        qq = int(qq or 0)
        if not qq:
            return 50
        new = max(0, min(100, int(favor or 50)))
        reason = " ".join(str(reason or "").split())[:80]
        with self._lock:
            row = self.conn.execute("SELECT favor FROM person_relations WHERE qq=?", (qq,)).fetchone()
            old = int(row["favor"]) if row else 50
            now = int(time.time())
            self.conn.execute(
                "INSERT INTO person_relations(qq,favor,reason,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(qq) DO UPDATE SET favor=excluded.favor, reason=excluded.reason, updated_at=excluded.updated_at",
                (qq, new, _enc(reason), now))
            self.conn.execute(
                "INSERT INTO favor_history(qq,favor,delta,reason,conv,ts) VALUES(?,?,?,?,?,?)",
                (qq, new, new - old, _enc(reason), conv or "", now))
            self.conn.commit()
        return new

    # ---------- 信任度（服务器版新增）----------
    def _trust_cap(self, qq):
        """信任上限 = 30 + 0.4*好感 + 0.3*熟悉度；不熟/不投缘的人不可能高信任。"""
        try:
            rel = self.conn.execute("SELECT favor FROM person_relations WHERE qq=?", (int(qq),)).fetchone()
            favor = int(rel["favor"]) if rel else 50
            st = self.conn.execute("SELECT msg_count, first_seen FROM visible_people WHERE qq=?", (int(qq),)).fetchone()
            mc = int(st["msg_count"] or 0) if st else 0
            fs = int(st["first_seen"] or 0) if st else 0
            fam = min(50.0, mc * 0.5)
            if fs:
                fam += min(25.0, max(0.0, (time.time() - fs) / 86400.0) * 5.0)
            return int(min(95, round(30 + 0.4 * favor + 0.3 * min(85.0, fam))))
        except Exception:
            return 80

    def apply_trust(self, qq, delta, reason="", conv="", cap=None, half_days=180, tier=""):
        """信任按人更新：+1~+2 慢涨；小失望 -1~-2；真背叛 -3；30 天内再犯 -5。
        窗口只认 tier='betrayal' 的事件，小失望不重置；信任最低留 10；不做到期跳档。"""
        qq = int(qq or 0)
        if not qq:
            return 30
        try:
            raw = int(delta or 0)
        except Exception:
            raw = 0
        raw = max(-5, min(2, raw))
        serious = raw <= -3
        tier = str(tier or ("betrayal" if serious else ("normal" if raw < 0 else "")))
        reason = " ".join(str(reason or "").split())[:80]
        now = int(time.time())
        with self._lock:
            row = self.conn.execute("SELECT trust,updated_at FROM person_relations WHERE qq=?", (qq,)).fetchone()
            stored = float(row["trust"]) if row and row["trust"] is not None else 30.0
            old = self.effective_trust(stored, row["updated_at"] if row else 0, half_days, now)
            top = int(cap) if cap is not None else self._trust_cap(qq)
            if serious:
                last = self.conn.execute(
                    "SELECT MAX(ts) FROM trust_history WHERE qq=? AND tier='betrayal'", (qq,)).fetchone()[0]
                if last and now - int(last) <= 30 * 86400:
                    delta = -5
                else:
                    delta = -3
            else:
                delta = max(-2, min(2, raw))
            new = old + delta
            if delta > 0 and new > top:
                new = top
            new = int(round(max(10.0, min(95.0, new))))
            self.conn.execute(
                "INSERT INTO person_relations(qq,trust,reason,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(qq) DO UPDATE SET trust=excluded.trust, reason=excluded.reason, updated_at=excluded.updated_at",
                (qq, new, _enc(reason), now))
            self.conn.execute(
                "INSERT INTO trust_history(qq,trust,delta,reason,conv,tier,ts) VALUES(?,?,?,?,?,?,?)",
                (qq, new, int(round(new - old)), _enc(reason), conv or "", tier, now))
            self.conn.commit()
        return new

    def set_trust(self, qq, trust, reason="手动调整", conv=""):
        qq = int(qq or 0)
        if not qq:
            return 30
        new = max(0, min(95, int(trust or 30)))
        reason = " ".join(str(reason or "").split())[:80]
        with self._lock:
            row = self.conn.execute("SELECT trust FROM person_relations WHERE qq=?", (qq,)).fetchone()
            old = int(row["trust"]) if row and row["trust"] is not None else 30
            now = int(time.time())
            self.conn.execute(
                "INSERT INTO person_relations(qq,trust,reason,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(qq) DO UPDATE SET trust=excluded.trust, reason=excluded.reason, updated_at=excluded.updated_at",
                (qq, new, _enc(reason), now))
            self.conn.execute(
                "INSERT INTO trust_history(qq,trust,delta,reason,conv,ts) VALUES(?,?,?,?,?,?)",
                (qq, new, new - old, _enc(reason), conv or "", now))
            self.conn.commit()
        return new

    def favor_decay(self, idle_days=14):
        """v0.64.0：好感改成读取时按半衰期连续衰减，不再按周跳档；保留方法给旧调用。"""
        return 0

    def list_grudges(self, qq, active_only=True, days=30):
        """v0.64.0：weight 连续衰减（半衰期 = days/2）；active 只表示手动解除，不再按天清零。"""
        now = int(time.time())
        half = max(1.0, float(days or 30) / 2.0) * 86400.0
        with self._lock:
            sql = "SELECT * FROM person_grudges WHERE qq=?" + (" AND active=1" if active_only else "")
            rows = self.conn.execute(sql + " ORDER BY created DESC LIMIT 10", (int(qq or 0),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["text"] = _dec(d.get("text") or "")
            try:
                base = float(d.get("weight") if d.get("weight") is not None else 100.0)
                last = int(d.get("updated") or d.get("created") or 0)
                w = base * (0.5 ** (max(0, now - last) / half))
            except Exception:
                w = 100.0
            d["weight"] = round(max(0.0, min(100.0, w)), 1)
            d["decayed"] = d["weight"] < 10.0
            out.append(d)
        return out

    def add_grudge(self, qq, text, conv=""):
        """记仇最多 3 条；同一件事不重复记。"""
        qq = int(qq or 0)
        text = " ".join(str(text or "").split())[:160]
        if not qq or len(text) < 4:
            return False
        norm = _norm_text(text)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text FROM person_grudges WHERE qq=? AND active=1", (qq,)).fetchall()
            now = int(time.time())
            for r in rows:
                if _norm_text(_dec(r["text"])) == norm:
                    self.conn.execute(
                        "UPDATE person_grudges SET weight=MIN(100, COALESCE(weight,100)+25), updated=? WHERE id=?",
                        (now, int(r["id"])))
                    self.conn.commit()
                    return True
            self.conn.execute("INSERT INTO person_grudges(qq,text,created,active,weight,updated) VALUES(?,?,?,1,100,?)",
                              (qq, _enc(text), now, now))
            rows = self.conn.execute(
                "SELECT id FROM person_grudges WHERE qq=? AND active=1 ORDER BY created DESC", (qq,)).fetchall()
            for r in rows[3:]:
                self.conn.execute("UPDATE person_grudges SET active=0 WHERE id=?", (r["id"],))
            self.conn.commit()
        return True

    def clear_grudge(self, gid):
        with self._lock:
            self.conn.execute("UPDATE person_grudges SET active=0 WHERE id=?", (int(gid or 0),))
            self.conn.commit()

    def decay_grudges(self, days=30):
        """v0.64.0：记仇改成权重连续淡出，不再到期 active=0；保留方法给旧调用。"""
        return 0

    def get_group_mood(self, gid):
        with self._lock:
            r = self.conn.execute("SELECT * FROM group_mood WHERE gid=?", (int(gid or 0),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["note"] = _dec(d.get("note") or "")
        return d

    def set_group_mood(self, gid, mood, note=""):
        with self._lock:
            self.conn.execute(
                "INSERT INTO group_mood(gid,mood,note,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(gid) DO UPDATE SET mood=excluded.mood, note=excluded.note, updated_at=excluded.updated_at",
                (int(gid or 0), max(0, min(100, int(mood or 50))), _enc((note or "")[:300]), int(time.time())))
            self.conn.commit()


    def effective_favor(self, favor, updated_at, half_days=90, now=None):
        now = int(now or time.time())
        try:
            days = max(0.0, (now - int(updated_at or 0)) / 86400.0)
        except Exception:
            days = 0.0
        h = max(1.0, float(half_days or 90))
        return 50.0 + (float(favor if favor is not None else 50) - 50.0) * (0.5 ** (days / h))

    def effective_trust(self, trust, updated_at, half_days=180, now=None):
        now = int(now or time.time())
        try:
            days = max(0.0, (now - int(updated_at or 0)) / 86400.0)
        except Exception:
            days = 0.0
        h = max(1.0, float(half_days or 180))
        # Hurt trust does not heal merely because time passed. Real repair is explicit.
        value = float(trust if trust is not None else 30)
        return value if value <= 30 else 30.0 + (value - 30.0) * (0.5 ** (days / h))

    def effective_group_mood(self, mood, updated_at, half_days=7, now=None):
        now = int(now or time.time())
        try:
            days = max(0.0, (now - int(updated_at or 0)) / 86400.0)
        except Exception:
            days = 0.0
        h = max(1.0, float(half_days or 7))
        return 50.0 + (float(mood if mood is not None else 50) - 50.0) * (0.5 ** (days / h))

    def get_person_relation_effective(self, qq, favor_half=90, trust_half=180):
        row = self.get_person_relation(qq)
        if not row:
            return {"qq": int(qq or 0), "favor": 50.0, "trust": 30.0, "updated_at": 0, "reason": ""}
        return {"qq": int(qq or 0),
                "favor": round(self.effective_favor(row.get("favor"), row.get("updated_at"), favor_half), 1),
                "trust": round(self.effective_trust(row.get("trust"), row.get("updated_at"), trust_half), 1),
                "updated_at": int(row.get("updated_at") or 0),
                "reason": row.get("reason") or ""}

    # ---------- v0.64.0：快心情 ----------
    EMOTION_HALF = {"jealousy": 45 * 60, "upset": 90 * 60, "sad": 2 * 3600,
                    "happy": 3600, "shy": 3600, "worry": 3600, "surprise": 15*60}
    EMOTION_CAP = {"jealousy": 60, "upset": 70, "sad": 80, "happy": 70, "shy": 60, "worry": 50, "surprise": 40}

    def _decay_emotion(self, value, kind, elapsed, now=None):
        try:
            v = float(value or 0)
            if v <= 0:
                return 0.0
            h = float(self.EMOTION_HALF.get(kind, 3600))
            return v * (0.5 ** (max(0.0, float(elapsed)) / h))
        except Exception:
            return 0.0

    def get_emotions(self, qq, now=None, min_value=0.5):
        now = int(now or time.time())
        with self._lock:
            rows = self.conn.execute("SELECT * FROM person_emotions WHERE qq=?", (int(qq or 0),)).fetchall()
        out = []
        for r in rows:
            val = self._decay_emotion(r["intensity"], r["kind"], now - int(r["updated"] or 0))
            if val >= float(min_value):
                out.append({"kind": str(r["kind"]), "intensity": round(val, 1),
                            "reason": _dec(r["reason"] or ""), "source_conv": str(r["source_conv"] or ""), "updated": int(r["updated"] or 0)})
        out.sort(key=lambda x: -x["intensity"])
        return out

    def add_emotion(self, qq, kind, delta, reason="", source_conv="", now=None):
        qq = int(qq or 0)
        kind = str(kind or "").strip().lower()
        if not qq or kind not in self.EMOTION_HALF:
            return 0.0
        now = int(now or time.time())
        cap = float(self.EMOTION_CAP.get(kind, 70))
        reason = " ".join(str(reason or "").split())[:80]
        with self._lock:
            row = self.conn.execute(
                "SELECT intensity,updated,reason FROM person_emotions WHERE qq=? AND kind=?", (qq, kind)).fetchone()
            cur = self._decay_emotion(row["intensity"], kind, now - int(row["updated"] or 0)) if row else 0.0
            new = max(0.0, min(cap, cur + float(delta or 0)))
            if new < 0.5:
                new = 0.0
            self.conn.execute(
                "INSERT INTO person_emotions(qq,kind,intensity,reason,source_conv,updated) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(qq,kind) DO UPDATE SET intensity=excluded.intensity, reason=excluded.reason, "
                "source_conv=excluded.source_conv, updated=excluded.updated",
                (qq, kind, new, _enc(reason), str(source_conv or "")[:80], now))
            self.conn.execute(
                "INSERT INTO emotion_history(qq,kind,delta,intensity,reason,ts) VALUES(?,?,?,?,?,?)",
                (qq, kind, float(delta or 0), new, _enc(reason), now))
            self.conn.commit()
        return round(new, 1)

    def emotion_history(self, qq, limit=50):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM emotion_history WHERE qq=? ORDER BY ts DESC LIMIT ?",
                (int(qq or 0), int(limit))).fetchall()
        return [{"kind": r["kind"], "delta": r["delta"], "intensity": r["intensity"],
                 "reason": _dec(r["reason"] or ""), "ts": r["ts"]} for r in rows]

    def emotion_history_between(self, start, end, qq=0):
        with self._lock:
            if qq:
                rows = self.conn.execute(
                    "SELECT * FROM emotion_history WHERE qq=? AND ts>=? AND ts<? ORDER BY ts ASC",
                    (int(qq), int(start), int(end))).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM emotion_history WHERE ts>=? AND ts<? ORDER BY ts ASC",
                    (int(start), int(end))).fetchall()
        return [{"qq": r["qq"], "kind": r["kind"], "delta": r["delta"], "intensity": r["intensity"],
                 "reason": _dec(r["reason"] or ""), "ts": r["ts"]} for r in rows]


    def familiarity_for(self, msg_count, first_seen, last_seen, now=None):
        """面板和大脑同一套熟悉度：消息数 + 认识天数（不降）+ 最近 3 天连续活跃。"""
        now = float(now or time.time())
        try:
            mc = max(0, int(msg_count or 0))
            fs = int(first_seen or 0)
            ls = int(last_seen or 0)
        except Exception:
            mc, fs, ls = 0, 0, 0
        fam = min(50.0, mc * 0.5)
        if fs:
            fam += min(25.0, max(0.0, (now - fs) / 86400.0) * 5.0)
        if ls:
            fam += 10.0 * (0.5 ** (max(0.0, now - ls) / (3 * 86400.0)))
        return round(min(100.0, fam), 1)

    def favor_budget(self, qq, now=None):
        """今天三档好感的用量（面板只给主人看）。"""
        qq = int(qq or 0)
        now = int(now or time.time())
        try:
            day0 = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d")))
        except Exception:
            day0 = now - (now % 86400)
        budgets = {"casual": 1, "meaningful": 3, "major": 3}
        used = {k: 0 for k in budgets}
        with self._lock:
            rows = self.conn.execute(
                "SELECT tier, COALESCE(SUM(delta),0) s FROM favor_history "
                "WHERE qq=? AND ts>=? AND delta>0 GROUP BY tier", (qq, day0)).fetchall()
        for r in rows:
            k = str(r["tier"] or "")
            if k in used:
                used[k] = int(r["s"] or 0)
        return {k: {"used": used[k], "budget": budgets[k], "left": max(0, budgets[k] - used[k])} for k in budgets}

    def trust_window(self, qq, now=None):
        """上次真背叛距今天数；小失望不算。"""
        qq = int(qq or 0)
        now = int(now or time.time())
        with self._lock:
            last = self.conn.execute(
                "SELECT MAX(ts) FROM trust_history WHERE qq=? AND tier='betrayal'", (qq,)).fetchone()[0]
            cnt = self.conn.execute(
                "SELECT COUNT(*) FROM trust_history WHERE qq=? AND tier='betrayal' AND ts>=?",
                (qq, now - 30 * 86400)).fetchone()[0]
        last = int(last or 0)
        days = round((now - last) / 86400.0, 1) if last else None
        return {"last_betrayal": last, "days": days, "within30": bool(last and (now - last) <= 30 * 86400),
                "count30": int(cnt or 0), "floor": 10}

    def favor_history(self, qq, limit=80):
        with self._lock:
            rows = self.conn.execute(
                "SELECT favor,delta,reason,conv,tier,ts FROM favor_history WHERE qq=? ORDER BY ts ASC LIMIT ?",
                (int(qq or 0), int(limit))).fetchall()
        return [dict(r, reason=_dec(r["reason"])) for r in rows]

    def people_overview(self, allow_users=None, allow_groups=None, owner_qq=0):
        """只显示：主人 + 白名单好友 + 允许群里的群友；不列无关 QQ 好友。"""
        allow_users = [int(x) for x in (allow_users or []) if int(x or 0) > 0]
        allow_groups = [int(x) for x in (allow_groups or []) if int(x or 0) > 0]
        owner = int(owner_qq or 0)
        gconvs = ["g:%d" % g for g in allow_groups]
        with self._lock:
            group_members = set()
            if gconvs:
                marks = ",".join("?" * len(gconvs))
                for r in self.conn.execute(
                        "SELECT DISTINCT sender_id FROM visible_messages WHERE direction='in' AND sender_id>0 "
                        "AND conv IN (%s)" % marks, gconvs).fetchall():
                    group_members.add(int(r["sender_id"]))
            else:
                # 官方通道 allow_groups=[] 语义是“所有已加入的群”，不能当成“没有群”。
                # 否则面板左侧只剩主人一个，看起来像“别人没有记忆”。
                for r in self.conn.execute(
                        "SELECT DISTINCT sender_id FROM visible_messages WHERE direction='in' AND sender_id>0 "
                        "AND conv LIKE 'g:%'").fetchall():
                    group_members.add(int(r["sender_id"]))
            vis = set(allow_users) | group_members
            if owner:
                vis.add(owner)
            if not vis:
                return []
            # 消息统计 / 最近一次群名片
            stats = {}
            for r in self.conn.execute(
                    "SELECT sender_id, COUNT(*) c, MAX(ts) mts FROM visible_messages WHERE direction='in' AND sender_id>0 "
                    "GROUP BY sender_id").fetchall():
                stats[int(r["sender_id"])] = (int(r["c"] or 0), int(r["mts"] or 0))
            latest_names = {}
            for r in self.conn.execute(
                    "SELECT m.sender_id, m.sender_name FROM visible_messages m JOIN "
                    "(SELECT sender_id, MAX(id) mid FROM visible_messages WHERE direction='in' GROUP BY sender_id) x "
                    "ON x.mid=m.id").fetchall():
                latest_names[int(r["sender_id"])] = r["sender_name"] or ""
            ppl = {int(r["qq"]): dict(r) for r in self.conn.execute("SELECT * FROM visible_people").fetchall()}
            rels = {int(r["qq"]): dict(r) for r in self.conn.execute("SELECT * FROM person_relations").fetchall()}
            cards = {str(r["conv"]): dict(r) for r in self.conn.execute("SELECT * FROM visible_person_cards").fetchall()}
            memc = {int(r["qq"]): int(r["c"]) for r in self.conn.execute(
                "SELECT qq, COUNT(*) c FROM visible_person_memories WHERE status='active' GROUP BY qq").fetchall()}
            grc = {int(r["qq"]): int(r["c"]) for r in self.conn.execute(
                "SELECT qq, COUNT(*) c FROM person_grudges WHERE active=1 GROUP BY qq").fetchall()}
        out = []
        for qq in vis:
            if self.is_blocked(qq):
                continue
            person = ppl.get(qq) or {}
            rel = rels.get(qq) or {}
            card = cards.get("q:%d" % qq) or {}
            msg_count, mts = stats.get(qq, (0, 0))
            name = person.get("name") or latest_names.get(qq) or str(qq)
            try:
                aliases = json.loads(person.get("aliases") or "[]")
            except Exception:
                aliases = []
            last_seen = int(mts or 0)
            is_owner = bool(owner and qq == owner)
            is_wl = qq in allow_users
            is_gm = qq in group_members
            if is_owner:
                role = "主人"
            elif is_wl and is_gm:
                role = "群友 + 白名单"
            elif is_wl:
                role = "白名单好友"
            else:
                role = "群友"
            # v0.64.0：面板也显示按半衰期算出来的有效值；熟悉度和大脑同一套公式
            _fs = int(person.get("first_seen") or 0)
            out.append({
                "qq": qq, "name": name, "aliases": aliases,
                "first_seen": _fs, "last_seen": last_seen,
                "msg_count": msg_count,
                "favor": int(round(self.effective_favor(rel.get("favor") or 50, rel.get("updated_at"), 90))),
                "trust": int(round(self.effective_trust(rel.get("trust") if rel.get("trust") is not None else 30,
                                                         rel.get("updated_at"), 180))),
                "reason": _dec(rel.get("reason") or ""),
                "card": _dec(card.get("card") or ""), "recent": _dec(card.get("recent") or ""),
                "evidence": int(card.get("evidence") or 0), "updated_at": card.get("updated_at"),
                "mem_count": int(memc.get(qq) or 0), "grudge_count": int(grc.get(qq) or 0),
                "familiarity": int(round(min(100, self.familiarity_for(msg_count, _fs, last_seen)+self.meaningful_familiarity(qq)))),
                "is_owner": is_owner, "is_whitelist": is_wl, "is_group_member": is_gm, "role": role,
            })
        out.sort(key=lambda x: (0 if x["is_owner"] else 1, -(x["last_seen"] or 0), x["qq"]))
        return out

    def prune_people_scope(self, allow_users=None, allow_groups=None, owner_qq=0):
        """删掉不在"主人/白名单/允许群群友"里、也没有任何数据的 people 行（好友预填的残留）。"""
        allow_users = [int(x) for x in (allow_users or []) if int(x or 0) > 0]
        allow_groups = [int(x) for x in (allow_groups or []) if int(x or 0) > 0]
        owner = int(owner_qq or 0)
        gconvs = ["g:%d" % g for g in allow_groups]
        with self._lock:
            group_members = set()
            if gconvs:
                marks = ",".join("?" * len(gconvs))
                for r in self.conn.execute(
                        "SELECT DISTINCT sender_id FROM visible_messages WHERE direction='in' AND sender_id>0 "
                        "AND conv IN (%s)" % marks, gconvs).fetchall():
                    group_members.add(int(r["sender_id"]))
            vis = set(allow_users) | group_members
            if owner:
                vis.add(owner)
            if not vis:
                return 0
            marks = ",".join("?" * len(vis))
            cur = self.conn.execute(
                "DELETE FROM people WHERE qq NOT IN (" + marks + ") "
                "AND qq NOT IN (SELECT qq FROM person_memories WHERE status='active') "
                "AND qq NOT IN (SELECT qq FROM person_relations) "
                "AND qq NOT IN (SELECT CAST(substr(conv,3) AS INTEGER) FROM person_cards WHERE conv LIKE 'q:%') "
                "AND qq NOT IN (SELECT qq FROM person_grudges) "
                "AND qq NOT IN (SELECT DISTINCT sender_id FROM messages WHERE direction='in' AND sender_id>0)",
                tuple(vis))
            self.conn.commit()
        return max(cur.rowcount, 0)

    def person_groups(self, qq, allow_groups=None):
        gids = [int(x) for x in (allow_groups or []) if int(x or 0) > 0]
        if allow_groups is not None and not gids:
            return []
        sql = ("SELECT conv, COUNT(*) c, MAX(ts) last_ts FROM visible_messages "
               "WHERE sender_id=? AND conv LIKE 'g:%'")
        args = [int(qq or 0)]
        if allow_groups is not None:
            sql += " AND conv IN (" + ",".join("?" * len(gids)) + ")"
            args += ["g:%d" % g for g in gids]
        with self._lock:
            rows = self.conn.execute(sql + " GROUP BY conv ORDER BY c DESC LIMIT 20", args).fetchall()
            names = {r["conv"]: r["name"] for r in self.conn.execute("SELECT conv,name FROM convs").fetchall()}
        return [{"conv": r["conv"], "name": names.get(r["conv"], r["conv"]), "count": r["c"],
                 "last_ts": r["last_ts"]} for r in rows]

    def person_recent_messages(self, qq, limit=10, since=0, exclude_conv=""):
        """这个人在任意会话里最近说的话（只读，主动聊天找「由头」用）。"""
        args = [int(qq or 0), int(since or 0)]
        sql = ("SELECT id,conv,ts,direction,sender_name,text,attachments FROM visible_messages "
               "WHERE sender_id=? AND direction='in' AND ts>=?")
        if exclude_conv:
            sql += " AND conv<>?"
            args.append(str(exclude_conv))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            if self.sleep_tainted(r["id"], text=_dec(r["text"]), attachments=r["attachments"]):
                continue
            d = dict(r)
            d["text"] = _dec(d.get("text") or "") or ""
            out.append(d)
        return out

    def person_detail(self, qq, allow_groups=None):
        qq = int(qq or 0)
        person = None
        with self._lock:
            r = self.conn.execute("SELECT * FROM visible_people WHERE qq=?", (qq,)).fetchone()
        if r:
            person = dict(r)
            try:
                person["aliases"] = json.loads(person.get("aliases") or "[]")
            except Exception:
                person["aliases"] = []
        elif qq:
            # 只在启动补消息里出现过、people 还没建行的人：用最近一条消息的名字兜底
            with self._lock:
                m = self.conn.execute(
                    "SELECT sender_name, ts FROM visible_messages WHERE sender_id=? AND direction='in' "
                    "ORDER BY id DESC LIMIT 1", (qq,)).fetchone()
            if m:
                person = {"qq": qq, "name": m["sender_name"] or str(qq), "aliases": [],
                          "first_seen": 0, "last_seen": int(m["ts"] or 0), "msg_count": 0}
        with self._lock:
            _st = self.conn.execute(
                "SELECT COUNT(*) c, MIN(ts) fs, MAX(ts) ls FROM visible_messages "
                "WHERE sender_id=? AND direction='in'", (qq,)).fetchone()
        _mc = int((_st["c"] if _st else 0) or 0)
        # 和 people_overview 对齐：认识天数用 people 的首见；最近活跃取两者较新
        _fs = int((person or {}).get("first_seen") or (_st["fs"] if _st else 0) or 0)
        _ls = max(int((person or {}).get("last_seen") or 0), int((_st["ls"] if _st else 0) or 0))
        return {
            "qq": qq,
            "person": person,
            "profile": self.get_profile(qq),
            "relation": self.get_person_relation_effective(qq),
            "familiarity": int(round(min(100, self.familiarity_for(_mc, _fs, _ls)+self.meaningful_familiarity(qq)))),
            "favor_budget": self.favor_budget(qq),
            "interaction_budget": self.interaction_budget(qq),
            "meaningful_familiarity": self.meaningful_familiarity(qq),
            "trust_window": self.trust_window(qq),
            "emotions": self.get_emotions(qq),
            "memories": self.list_person_memories(qq, 200),
            "shared": self.list_shared_memories(200, qq=qq),
            "grudges": self.list_grudges(qq),
            "favor_history": self.favor_history(qq, 80),
            "groups": self.person_groups(qq, allow_groups),
        }

    def recent_rows(self, conv, limit=12):
        """最近几条原始消息（带发送人），给按人好感/卡片用。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,direction,sender_id,sender_name,text,ts,message_id,attachments FROM visible_messages WHERE conv=? "
                "ORDER BY id DESC LIMIT ?", (conv, int(limit))).fetchall()
        return [dict(r, text=_dec(r["text"]) or "") for r in reversed(rows) if not self.sleep_tainted(r["id"], text=_dec(r["text"]), attachments=r["attachments"])]

    def last_sender_name(self, qq):
        """这个人最近一次发消息用的群名片/昵称；people 表没名字时用来兜底。"""
        q = int(qq or 0)
        if not q:
            return ""
        with self._lock:
            r = self.conn.execute(
                "SELECT sender_name FROM visible_messages WHERE sender_id=? AND direction='in' "
                "AND sender_name IS NOT NULL AND sender_name<>'' ORDER BY id DESC LIMIT 1", (q,)).fetchone()
        return str(r["sender_name"] or "") if r else ""

    def thread_rows(self, conv, limit=30):
        """给「群聊线程」用的结构化消息：带 message_id / 引用附件 / 时间 / 发送人。
        只读，不改库；线程视图靠它把「谁回复了谁」显式画出来，而不是压成一条流水。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,message_id,sender_id,sender_name,direction,text,raw,ts,attachments "
                "FROM visible_messages WHERE conv=? ORDER BY id DESC LIMIT ?", (conv, int(limit))).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["text"] = _dec(r["text"]) or ""
            d["raw"] = _dec(r["raw"]) or ""
            try:
                d["attachments"] = json.loads(d["attachments"] or "[]")
            except Exception:
                d["attachments"] = []
            out.append(d)
        return out

    # ---------- 共同记忆（2~5 人共有） ----------
    def add_shared_memory(self, text, people, gid=0, kind="event", importance=3, source_conv="",
                          key_facts=None):
        text = " ".join(str(text or "").split())[:300]
        pts = sorted({int(x) for x in (people or []) if int(x or 0) > 0})
        if len(pts) < 2 or len(pts) > 5 or len(text) < 6 or looks_junk(text):
            return None
        norm = _norm_text(text)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text,people FROM visible_shared_memories WHERE status='active' ORDER BY id DESC LIMIT 400").fetchall()
            for r in rows:
                try:
                    same = sorted(json.loads(r["people"] or "[]")) == pts
                except Exception:
                    same = False
                if same and _norm_text(_dec(r["text"])) == norm:
                    return {"action": "skip", "id": r["id"]}
            facts = facts_to_json(key_facts) if key_facts else facts_to_json(guess_key_facts(text))
            cur = self.conn.execute(
                "INSERT INTO shared_memories(text,kind,importance,people,gid,source_conv,created,key_facts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_enc(text), kind or "event", max(1, min(5, int(importance or 3))),
                 json.dumps(pts), int(gid or 0), source_conv or "", int(time.time()), facts))
            mid = cur.lastrowid
            self.conn.commit()
        self._prune_shared(300)
        return {"action": "add", "id": mid}

    def _prune_shared(self, keep=300):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM visible_shared_memories WHERE status='active' AND protected=0 AND kind<>'commitment' AND importance<4 ORDER BY importance DESC, use_count DESC, created DESC"
            ).fetchall()
            drop = [r["id"] for r in rows[max(10, int(keep)):]]
            if not drop:
                return
            now = int(time.time())
            for mid in drop:
                try:
                    # 同 _prune_scope：淘汰即清向量，30 天后整行删
                    self.conn.execute(
                        "UPDATE shared_memories SET status='pruned', pruned_at=?, vec=NULL, vec_model='' WHERE id=?",
                        (now, mid))
                except Exception:
                    self.conn.execute("UPDATE shared_memories SET status='pruned' WHERE id=?", (mid,))
            self.conn.commit()

    def _shared_row(self, r):
        d = dict(r)
        # vec 是 embedding 二进制：面板 JSON 序列化不了，会把人物/群详情整个打成 500（v0.28.1）
        d.pop("vec", None)
        d["text"] = _dec(d["text"])
        try:
            d["people"] = [int(x) for x in json.loads(d.get("people") or "[]")]
        except Exception:
            d["people"] = []
        return d

    def search_shared_memories(self, qq, keywords, limit=4, query_vec=None):
        qq = int(qq or 0)
        if not qq:
            return []
        tokens = _plain_tokens(" ".join(keywords or []))
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM visible_shared_memories WHERE status='active' ORDER BY importance DESC, created DESC LIMIT 400"
            ).fetchall()
            rows = [r for r in rows if qq in self._shared_row(r)["people"]]
            scored = self._score_rows(rows, tokens, query_vec)
            picked = [x for x in scored if x[5]][:limit]
            ids = [x[3]["id"] for x in picked]
            if ids:
                self.record_memory_retrieval("shared_memories", ids)
                self.conn.commit()
        return [{"id": x[3]["id"], "text": x[2], "kind": x[3]["kind"], "importance": x[3]["importance"],
                 "created": int(x[3]["created"] or 0), "key_facts": str(x[3]["key_facts"] or ""),
                 "people": self._shared_row(x[3])["people"], "gid": x[3]["gid"]} for x in picked]

    def list_shared_memories(self, limit=200, qq=0, gid=None):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM visible_shared_memories WHERE status='active' ORDER BY created DESC LIMIT ?",
                (int(limit),)).fetchall()
            names = {r["qq"]: r["name"] for r in self.conn.execute("SELECT qq,name FROM visible_people").fetchall()}
        out = []
        for r in rows:
            d = self._shared_row(r)
            if qq and int(qq) not in d["people"]:
                continue
            if gid is not None and int(d.get("gid") or 0) != int(gid):
                continue
            d["people_names"] = [names.get(x) or str(x) for x in d["people"]]
            out.append(d)
        return out

    def delete_shared_memory(self, mid):
        with self._lock:
            self.conn.execute("UPDATE shared_memories SET status='deleted' WHERE id=?", (int(mid or 0),))
            self.conn.commit()

    def update_shared_memory(self, mid, text):
        with self._lock:
            self.conn.execute("UPDATE shared_memories SET text=?, vec=NULL WHERE id=?",
                              (_enc(" ".join(str(text or "").split())[:300]), int(mid or 0)))
            self.conn.commit()

    def groups_overview(self, allow_groups=None):
        if allow_groups is None:
            # 官方通道 allow_groups=[] 语义是“所有已加入的群”；不能当没群
            with self._lock:
                gids = [int(str(r["conv"]).split(":")[-1]) for r in self.conn.execute(
                    "SELECT DISTINCT conv FROM visible_messages WHERE conv LIKE 'g:%'").fetchall()
                    if str(r["conv"]).split(":")[-1].isdigit()]
        else:
            gids = [int(x) for x in (allow_groups or []) if int(x or 0) > 0]
        if not gids:
            return []
        marks = ",".join("?" * len(gids))
        with self._lock:
            rows = self.conn.execute(
                "SELECT conv, COUNT(*) c, MAX(ts) last_ts, "
                "(SELECT COUNT(DISTINCT sender_id) FROM visible_messages x WHERE x.conv=m.conv AND x.direction='in') members "
                "FROM visible_messages m WHERE conv IN (" + marks + ") GROUP BY conv ORDER BY c DESC LIMIT 30",
                ["g:%d" % g for g in gids]).fetchall()
            names = {r["conv"]: r["name"] for r in self.conn.execute("SELECT conv,name FROM convs").fetchall()}
            moods = {int(r["gid"]): (int(r["mood"]), int(r["updated_at"] or 0))
                     for r in self.conn.execute("SELECT gid,mood,updated_at FROM group_mood").fetchall()}
        out = []
        for r in rows:
            gid = int(str(r["conv"]).split(":")[-1] or 0)
            _m, _mu = moods.get(gid, (50, 0))
            out.append({"gid": gid, "conv": r["conv"], "name": names.get(r["conv"]) or ("群%d" % gid),
                        "count": r["c"], "members": int(r["members"] or 0), "last_ts": r["last_ts"],
                        "mood": _m, "mood_effective": int(round(self.effective_group_mood(_m, _mu, 7))),
                        "mood_updated": _mu})
        return out

    def group_detail(self, gid):
        gid = int(gid or 0)
        conv = "g:%d" % gid
        with self._lock:
            name = ""
            r = self.conn.execute("SELECT name FROM convs WHERE conv=?", (conv,)).fetchone()
            if r:
                name = r["name"] or ""
            rows = self.conn.execute(
                "SELECT m.sender_id qq, MAX(m.sender_name) name, COUNT(*) c, p.name pname, r.favor, r.updated_at, "
                "(SELECT COUNT(*) FROM visible_person_memories pm WHERE pm.qq=m.sender_id AND pm.status='active') mem_count "
                "FROM visible_messages m LEFT JOIN visible_people p ON p.qq=m.sender_id "
                "LEFT JOIN person_relations r ON r.qq=m.sender_id "
                "WHERE m.conv=? AND m.direction='in' GROUP BY m.sender_id ORDER BY c DESC LIMIT 60",
                (conv,)).fetchall()
        members = []
        for x in rows:
            members.append({"qq": x["qq"], "name": x["pname"] or x["name"] or str(x["qq"]),
                            "count": x["c"],
                            "favor": int(round(self.effective_favor(x["favor"] or 50, x["updated_at"], 90))),
                            "mem_count": int(x["mem_count"] or 0)})
        mood = self.get_group_mood(gid)
        mood_eff = int(round(self.effective_group_mood((mood or {}).get("mood"), (mood or {}).get("updated_at"), 7))) if mood else 50
        return {
            "gid": gid, "conv": conv, "name": name or ("群%d" % gid),
            "mood": mood, "mood_effective": mood_eff,
            "members": members,
            "memories": self.list_group_memories(gid, 200),
            "shared": self.list_shared_memories(200, gid=gid),
        }

    def day_messages(self, day):
        start = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,conv,direction,sender_name,text,attachments FROM visible_messages WHERE ts >= ? AND ts < ? ORDER BY id ASC",
                (start, start + 86400)).fetchall()
        out = []
        for r in rows:
            if self.sleep_tainted(r["id"], text=_dec(r["text"]), attachments=r["attachments"]):
                continue
            d = dict(r); d["text"] = _dec(d["text"]); out.append(d)
        return out

    def search_memories(self, keywords, world="private", group_id=0, person_id=0, limit=6):
        """按域召回：私聊只取这个人的私聊记忆；群聊只取这个群 + 当前说话人的群记忆。"""
        toks = set()
        for k in keywords or []:
            toks |= tokenize(k)
        if not toks:
            return []
        marks = ",".join("?" * len(toks))
        where = "m.world = ?"
        scope = [world]
        if world == "private":
            where += " AND m.person_id = ?"
            scope.append(int(person_id or 0))
        else:
            where += " AND m.group_id = ? AND (m.person_id = 0 OR m.person_id = ?)"
            scope.append(int(group_id or 0))
            scope.append(int(person_id or 0))
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT m.id,m.text,m.conv,m.ts,m.world,m.group_id,m.person_id,m.group_name,m.person_name "
                "FROM visible_memories m JOIN mem_tokens t ON t.memory_id=m.id WHERE t.token IN (" + marks + ") AND " + where +
                " ORDER BY m.id DESC LIMIT ?", (*toks, *scope, int(limit))).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.conn.execute("UPDATE memories SET last_used=? WHERE id IN (%s)" % ",".join("?" * len(ids)),
                                  (int(time.time()), *ids))
                self.conn.commit()
        out = []
        for r in rows:
            d = dict(r); d["text"] = _dec(d["text"]); out.append(d)
        return out

    def update_memory(self, mid, text):
        stored = str(text)[:500]
        with self._lock:
            self.conn.execute("UPDATE memories SET text=? WHERE id=?", (_enc(stored), int(mid)))
            self.conn.execute("DELETE FROM mem_tokens WHERE memory_id=?", (int(mid),))
            for tok in tokenize(stored):
                self.conn.execute("INSERT INTO mem_tokens(memory_id,token) VALUES(?,?)", (int(mid), tok))
            self.conn.commit()

    def delete_memory(self, mid):
        with self._lock:
            self.conn.execute("DELETE FROM memories WHERE id=?", (int(mid),))
            self.conn.execute("DELETE FROM mem_tokens WHERE memory_id=?", (int(mid),))
            self.conn.commit()

    def update_diary(self, conv, day, text):
        with self._lock:
            self.conn.execute("UPDATE diaries SET text=?, ts=? WHERE conv=? AND day=?",
                              (_enc(str(text)[:5000]), int(time.time()), conv, day))
            self.conn.commit()

    def delete_diary(self, conv, day):
        with self._lock:
            self.conn.execute("DELETE FROM diaries WHERE conv=? AND day=?", (conv, day))
            self.conn.commit()

    def get_setting(self, key):
        with self._lock:
            r = self.conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return r["v"] if r else None

    def set_setting(self, key, value):
        with self._lock:
            self.conn.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, str(value)))
            self.conn.commit()

    def recent_memories(self, limit=50):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,ts,conv,text,world,group_id,person_id,group_name,person_name FROM visible_memories ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["text"] = _dec(d["text"]); out.append(d)
        return out

    def upsert_diary(self, conv, day, text):
        with self._lock:
            self.conn.execute("INSERT INTO diaries(conv,day,text,ts) VALUES(?,?,?,?) "
                              "ON CONFLICT(conv,day) DO UPDATE SET text=excluded.text, ts=excluded.ts",
                              (conv, day, _enc(text), int(time.time())))
            self.conn.commit()

    def list_diaries(self, limit=30):
        with self._lock:
            rows = self.conn.execute("SELECT conv,day,text,ts FROM diaries ORDER BY day DESC, ts DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["text"] = _dec(d["text"]); out.append(d)
        return out

    def add_reminder(self, conv, at, text, target_uid=0, target_name="", raw=""):
        """v0.56.0：连“提醒谁 / 原话”一起存，到点才能用人设喊人。"""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO reminders(conv,at,text,ts,target_uid,target_name,raw) VALUES(?,?,?,?,?,?,?)",
                (conv, int(at), text[:300], int(time.time()), int(target_uid or 0),
                 str(target_name or "")[:40], str(raw or "")[:300]))
            self.conn.commit()
            return cur.lastrowid

    def due_reminders(self, now=None):
        now = int(now or time.time())
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,conv,text,at,target_uid,target_name FROM visible_reminders "
                "WHERE done=0 AND at<=? ORDER BY at ASC LIMIT 20", (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder(self, rid):
        with self._lock:
            self.conn.execute("UPDATE reminders SET done=1 WHERE id=?", (int(rid),))
            self.conn.commit()

    def list_reminders(self, limit=30):
        with self._lock:
            rows = self.conn.execute("SELECT id,conv,text,at,done FROM visible_reminders ORDER BY done ASC, at ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def pending_reminders(self, conv, now=None):
        """v0.65.4：这个会话里还没到点、没过期的提醒（回复提示用：别再重复追问）。"""
        now = int(now or time.time())
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,conv,text,at,ts,target_uid,target_name,asked FROM visible_reminders "
                "WHERE done=0 AND at>? AND conv=? ORDER BY at ASC LIMIT 5",
                (now, str(conv or ""))).fetchall()
        return [dict(r) for r in rows]

    def update_reminder(self, rid, text=None, asked=None):
        """v0.65.4：补全提醒内容 / 记一笔“问过了”。只改传进来的字段。"""
        sets, args = [], []
        if text is not None:
            sets.append("text=?")
            args.append(str(text)[:300])
        if asked is not None:
            sets.append("asked=?")
            args.append(1 if asked else 0)
        if not sets:
            return False
        args.append(int(rid))
        with self._lock:
            self.conn.execute("UPDATE reminders SET %s WHERE id=?" % ",".join(sets), args)
            self.conn.commit()
        return True

    def recent_logs(self, limit=60):
        with self._lock:
            rows = self.conn.execute("SELECT ts,tag,detail FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in reversed(rows):
            out.append({"t": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), "tag": r["tag"], "detail": r["detail"]})
        return out

    def add_log(self, tag, detail, ok=True):
        try:
            with self._lock:
                self.conn.execute("INSERT INTO logs(ts,tag,detail,ok) VALUES(?,?,?,?)",
                                  (int(time.time()), tag, str(detail)[:2000], 1 if ok else 0))
                self.conn.commit()
        except Exception:
            pass

    def stat(self):
        with self._lock:
            m = self.conn.execute("SELECT COUNT(*) c FROM visible_messages").fetchone()["c"]
            l = self.conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]
        return {"messages": m, "logs": l}

    # ---------- 分享账本：它自己跟主人说过什么、主人理没理 ----------
    SHARE_STATUS = ("pending", "replied", "seen_ignored", "unseen", "dumped")
    SHARE_MAX_RECORDS = 200

    def add_share(self, conv, kind, topic, line, juice=3, reason="", dedupe="", status="pending"):
        """记一条「我跟主人说了什么」。status=pending 表示还等着看他回不回。"""
        if status not in self.SHARE_STATUS:
            status = "pending"
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO shares(ts,conv,kind,topic,line,juice,status,reason,dedupe) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), str(conv or ""), "tattle" if kind == "tattle" else "share",
                 str(topic or "")[:120], _enc(str(line or "")[:400]), max(1, min(5, int(juice or 3))),
                 status, str(reason or "")[:80], str(dedupe or "")[:80]))
            share_id = cur.lastrowid
            self._trim_shares_locked()
            self.conn.commit()
            return share_id

    def _trim_shares_locked(self):
        cur = self.conn.execute(
            "DELETE FROM shares WHERE id IN (SELECT id FROM shares ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (self.SHARE_MAX_RECORDS,))
        return max(cur.rowcount, 0)

    def list_shares(self, limit=60):
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT id,ts,conv,kind,topic,line,juice,status,replied_at,seen_at,reply_conv,reason "
                    "FROM shares ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["line"] = _dec(d.get("line") or "")
            out.append(d)
        return out

    def last_share(self):
        try:
            with self._lock:
                r = self.conn.execute(
                    "SELECT id,ts,conv,kind,topic,line,juice,status,replied_at,seen_at,reply_conv,reason "
                    "FROM shares ORDER BY id DESC LIMIT 1").fetchone()
        except Exception:
            return None
        if not r:
            return None
        d = dict(r)
        d["line"] = _dec(d.get("line") or "")
        return d

    def has_share_dedupe(self, dedupe, hours=72):
        if not dedupe:
            return False
        since = int(time.time() - max(1, int(hours)) * 3600)
        try:
            with self._lock:
                r = self.conn.execute("SELECT 1 FROM shares WHERE dedupe=? AND ts>=? LIMIT 1",
                                      (str(dedupe), since)).fetchone()
        except Exception:
            return False
        return bool(r)

    def set_share_status(self, share_id, status, conv=""):
        if status not in self.SHARE_STATUS:
            raise ValueError("bad status: %s" % status)
        with self._lock:
            now = int(time.time())
            if status == "replied":
                self.conn.execute("UPDATE shares SET status=?,replied_at=?,reply_conv=? WHERE id=?",
                                  (status, now, str(conv or ""), int(share_id)))
            elif status == "seen_ignored":
                self.conn.execute("UPDATE shares SET status=?,seen_at=? WHERE id=?", (status, now, int(share_id)))
            else:
                self.conn.execute("UPDATE shares SET status=? WHERE id=?", (status, int(share_id)))
            self.conn.commit()

    def delete_share(self, share_id):
        with self._lock:
            self.conn.execute("DELETE FROM shares WHERE id=?", (int(share_id),))
            self.conn.commit()

    def mark_share_receipt(self, is_group, conv=""):
        """主人有动静：私聊里回话 = replied；在群里露面 = seen_ignored。返回改了几条。"""
        now = int(time.time())
        with self._lock:
            if is_group:
                cur = self.conn.execute(
                    "UPDATE shares SET status='seen_ignored',seen_at=? WHERE status='pending'", (now,))
            else:
                cur = self.conn.execute(
                    "UPDATE shares SET status='replied',replied_at=?,reply_conv=? "
                    "WHERE status IN ('pending','unseen')", (now, str(conv or "")))
            n = max(cur.rowcount, 0)
            self.conn.commit()
        return n

    def expire_shares(self, window_h=48):
        """超过 window_h 还没动静的分享 -> unseen（可能没看到，不记仇）。"""
        cutoff = int(time.time() - max(1, int(window_h)) * 3600)
        with self._lock:
            cur = self.conn.execute("UPDATE shares SET status='unseen' WHERE status='pending' AND ts<?", (cutoff,))
            self.conn.commit()
            return max(cur.rowcount, 0)

    def share_stats(self):
        since = int(time.time() - 7 * 86400)
        out = {"sent7": 0, "replied7": 0, "ignored7": 0, "unseen7": 0,
               "pending": 0, "last_ts": 0, "pending_bits": 0}
        try:
            with self._lock:
                for r in self.conn.execute(
                        "SELECT status,COUNT(*) c FROM shares WHERE ts>=? GROUP BY status", (since,)).fetchall():
                    st, c = r["status"], int(r["c"])
                    out["sent7"] += c
                    if st == "replied":
                        out["replied7"] = c
                    elif st == "seen_ignored":
                        out["ignored7"] = c
                    elif st == "unseen":
                        out["unseen7"] = c
                r = self.conn.execute("SELECT MAX(ts) t FROM shares").fetchone()
                out["last_ts"] = int((r["t"] if r else 0) or 0)
                r = self.conn.execute("SELECT COUNT(*) c FROM share_pending").fetchone()
                out["pending_bits"] = int((r["c"] if r else 0) or 0)
                r = self.conn.execute("SELECT COUNT(*) c FROM shares WHERE status='pending'").fetchone()
                out["pending"] = int((r["c"] if r else 0) or 0)
        except Exception:
            pass
        return out

    def recent_share_topics(self, hours=72, limit=6):
        since = int(time.time() - max(1, int(hours)) * 3600)
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT topic,ts,status FROM shares WHERE ts>=? ORDER BY id DESC LIMIT ?",
                    (since, int(limit))).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    # ---------- 憋着的梗（觉得有意思但还没跟主人说）----------
    def pending_rounds(self, dedupe):
        if not dedupe:
            return 0
        try:
            with self._lock:
                r = self.conn.execute("SELECT rounds FROM share_pending WHERE dedupe=? ORDER BY id DESC LIMIT 1",
                                      (str(dedupe),)).fetchone()
        except Exception:
            return 0
        return int((r["rounds"] if r else 0) or 0)

    def bump_pending_share(self, conv, kind, topic, juice=3, dedupe="", pid=0, keep=5):
        now = int(time.time())
        with self._lock:
            r = None
            if pid:
                r = self.conn.execute("SELECT id,rounds,juice,dedupe FROM share_pending WHERE id=?",
                                      (int(pid),)).fetchone()
            if r is None and dedupe:
                r = self.conn.execute(
                    "SELECT id,rounds,juice,dedupe FROM share_pending WHERE dedupe=? ORDER BY id DESC LIMIT 1",
                    (str(dedupe),)).fetchone()
            if r:
                rounds = int(r["rounds"] or 1) + 1
                keep_dedupe = str(r["dedupe"] or "") or str(dedupe or "")
                self.conn.execute(
                    "UPDATE share_pending SET rounds=?,juice=?,last_ts=?,conv=?,kind=?,dedupe=? WHERE id=?",
                    (rounds, max(int(r["juice"] or 3), int(juice or 3)), now, str(conv or ""),
                     "tattle" if kind == "tattle" else "share", keep_dedupe, int(r["id"])))
                self._trim_pending_locked(conv, keep)
                self.conn.commit()
                return rounds
            self.conn.execute(
                "INSERT INTO share_pending(conv,kind,topic,juice,rounds,first_ts,last_ts,dedupe) VALUES(?,?,?,?,?,?,?,?)",
                (str(conv or ""), "tattle" if kind == "tattle" else "share", str(topic or "")[:120],
                 max(1, min(5, int(juice or 3))), 1, now, now, str(dedupe or "")[:80]))
            self._trim_pending_locked(conv, keep)
            self.conn.commit()
            return 1

    def _trim_pending_locked(self, conv, keep):
        self.conn.execute(
            "DELETE FROM share_pending WHERE id IN (SELECT id FROM share_pending WHERE conv=? "
            "ORDER BY last_ts DESC, id DESC LIMIT -1 OFFSET ?)",
            (str(conv or ""), max(1, int(keep))))

    def list_pending_shares(self, limit=30):
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT id,conv,kind,topic,juice,rounds,first_ts,last_ts FROM share_pending "
                    "ORDER BY last_ts DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def drop_pending_share(self, pid=None, dedupe=""):
        with self._lock:
            if pid:
                self.conn.execute("DELETE FROM share_pending WHERE id=?", (int(pid),))
            if dedupe:
                self.conn.execute("DELETE FROM share_pending WHERE dedupe=?", (str(dedupe),))
            self.conn.commit()

    def cleanup_pending_shares(self, hours=24):
        """憋着没说、超过 N 小时自动丢掉。旧版按天算且没人调用，僵尸待办会一直挂着。"""
        cutoff = int(time.time() - max(1, int(hours)) * 3600)
        with self._lock:
            cur = self.conn.execute("DELETE FROM share_pending WHERE last_ts<?", (cutoff,))
            self.conn.commit()
            return max(cur.rowcount, 0)

    def trim_pending_shares(self, conv="", keep=5):
        """同一个会话最多留 keep 条，超了丢最老的。"""
        keep = max(1, int(keep))
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM share_pending WHERE conv=? ORDER BY last_ts DESC, id DESC", (str(conv),)).fetchall()
            extra = [int(r[0]) for r in rows[keep:]]
            if extra:
                self.conn.executemany("DELETE FROM share_pending WHERE id=?", [(i,) for i in extra])
                self.conn.commit()
            return len(extra)

    def cleanup_shares(self, days=180):
        cutoff = int(time.time() - max(1, int(days)) * 86400)
        with self._lock:
            cur = self.conn.execute("DELETE FROM shares WHERE ts<?", (cutoff,))
            removed = max(cur.rowcount, 0) + self._trim_shares_locked()
            self.conn.commit()
            return removed

    # ---------- QQ空间：说说账本 + 评论去重（v0.29.0 自主发说说）----------
    # update_qzone_post 只认这几个字段：面板/流程传错键不能改坏账本
    QZONE_POST_FIELDS = {
        "review": 2000, "status": 20, "tid": 80, "error": 1000, "deleted_reason": 200,
        "image_name": 120, "image_source": 40, "image_key": 120,
    }
    QZONE_POST_INT_FIELDS = ("tokens", "ugc_right", "deleted_at", "likes", "cmts")

    def add_qzone_post(self, trigger, text, nonce, status="drafted", mood="", image_name="", image_source="",
                       ugc_right=1, review="", tokens=0):
        """新增一条空间说说记录，返回 id；nonce 撞车（同一件事重复排程）返回 0。

        正文按全库约定加密存；ugc_right 是空间可见范围，默认 1=所有人可见（2026-10-23 改）。
        """
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO qzone_posts(created_at,trigger,mood,text,image_name,image_source,ugc_right,tid,"
                    "nonce,status,review,error,tokens,deleted_at,deleted_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), str(trigger or "")[:40], str(mood or "")[:40],
                     _enc(str(text or "")[:2000]), str(image_name or "")[:120], str(image_source or "")[:40],
                     int(ugc_right or 1), "", str(nonce or "")[:80], str(status or "drafted")[:20],
                     str(review or "")[:1900], "", int(tokens or 0), 0, ""))
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                # nonce 有 UNIQUE 约束：同一件事已经排过队，调用方按 0 处理即可，不算故障
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                _log("qzone", "说说去重：nonce 已存在 %r" % (str(nonce or "")[:80],))
                return 0
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                _log("qzone", "写入说说失败：%r" % (e,))
                raise

    def update_qzone_post(self, pid, **fields):
        """按白名单改说说字段，自动拼 SET；白名单外的键直接忽略，一个都没传就什么都不做。

        白名单只有：review/status/tid/error/tokens/deleted_at/deleted_reason/image_name/
        image_source/ugc_right（契约第 4 节），别的键不认。
        """
        sets, args = [], []
        for key, val in fields.items():
            if key in self.QZONE_POST_INT_FIELDS:
                try:
                    val = int(val or 0)
                except Exception:
                    val = 0
                sets.append("%s=?" % key)
                args.append(val)
            elif key in self.QZONE_POST_FIELDS:
                sets.append("%s=?" % key)
                args.append(str(val if val is not None else "")[:self.QZONE_POST_FIELDS[key]])
        if not sets:
            return
        args.append(int(pid))
        with self._lock:
            self.conn.execute("UPDATE qzone_posts SET %s WHERE id=?" % ", ".join(sets), args)
            self.conn.commit()

    def get_qzone_post(self, pid):
        """按 id 取一条说说；取不到返回 {}（调用方用 .get 取值就行）。"""
        try:
            with self._lock:
                r = self.conn.execute(
                    "SELECT id,created_at,trigger,mood,text,image_name,image_source,image_key,ugc_right,tid,nonce,"
                    "status,review,error,tokens,deleted_at,deleted_reason FROM qzone_posts WHERE id=?",
                    (int(pid or 0),)).fetchone()
        except Exception as e:
            _log("qzone", "读取说说失败：%r" % (e,))
            return {}
        if not r:
            return {}
        d = dict(r)
        d["text"] = _dec(d.get("text") or "")
        return d

    def list_qzone_posts(self, limit=10, since=0):
        """最近的说说，按 created_at 倒序；since 是时间戳（0=不限）。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT id,created_at,trigger,mood,text,image_name,image_source,image_key,ugc_right,tid,nonce,"
                    "status,review,error,tokens,deleted_at,deleted_reason FROM qzone_posts "
                    "WHERE created_at>=? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (int(since or 0), int(limit))).fetchall()
        except Exception as e:
            _log("qzone", "列出说说失败：%r" % (e,))
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["text"] = _dec(d.get("text") or "")
            out.append(d)
        return out

    def count_qzone_posts(self, since_ts):
        """since_ts 之后记了几条说说（每天条数配额用；已删的也算，额度已经用掉了）。"""
        try:
            with self._lock:
                r = self.conn.execute("SELECT COUNT(*) c FROM qzone_posts WHERE created_at>=?",
                                      (int(since_ts or 0),)).fetchone()
        except Exception as e:
            _log("qzone", "统计说说失败：%r" % (e,))
            return 0
        return int((r["c"] if r else 0) or 0)

    def count_qzone_published_posts(self, since_ts, include_manual=False):
        """直接统计全部已占用额度的帖，避免手动/失败记录挤出列表造成漏计。"""
        sql = "SELECT COUNT(*) FROM qzone_posts WHERE created_at>=? AND status IN ('posted','queued','deleted')"
        if not include_manual:
            sql += " AND LOWER(TRIM(COALESCE(trigger,'')))<>'manual'"
        with self._lock:
            return int(self.conn.execute(sql, (int(since_ts or 0),)).fetchone()[0])

    def recent_qzone_texts(self, limit=20):
        """最近发过的说说正文（纯文本列表，给查重看）。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT text FROM qzone_posts WHERE status IN ('posted','queued','deleted') "
                    "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception as e:
            _log("qzone", "读取说说正文失败：%r" % (e,))
            return []
        out = []
        for r in rows:
            s = str(_dec(r["text"]) or "").strip()
            if s:
                out.append(s)
        return out

    def add_qzone_comment(self, tid, comment_key, comment_id="", author_uin="", author_name="", is_self=0,
                          reply_to_key="", reply_to_name="", content="", created_at=0):
        """记一条空间评论：新插入返回 True，comment_key 已存在返回 False（下次轮询不重复回）。"""
        now = int(time.time())
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO qzone_comments(tid,comment_key,comment_id,author_uin,author_name,is_self,"
                    "reply_to_key,reply_to_name,content,created_at,seen_at,replied,reply_key,flagged) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'',0)",
                    (str(tid or "")[:80], str(comment_key or "")[:120], str(comment_id or "")[:120],
                     str(author_uin or "")[:40], _clean_name(author_name, 40), 1 if is_self else 0,
                     str(reply_to_key or "")[:120], _clean_name(reply_to_name, 40),
                     _enc(str(content or "")[:1000]), int(created_at or now), now))
                self.conn.commit()
                if getattr(self,'continuity_active',False) and not is_self:
                    self.self_record('qzone_comment','qzone_comment:'+str(comment_key),
                        {'text':str(content or '')[:1000],'tid':str(tid)},actor=int(author_uin or 0),
                        outcome='observed',ts=int(created_at or now))
                return True
        except sqlite3.IntegrityError:
            # comment_key 有 UNIQUE 约束：同一条评论只处理一次
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            _log("qzone", "写入评论失败：%r" % (e,))
            raise

    def list_qzone_comments(self, tid="", limit=50, only_unreplied=False):
        """评论列表（新的在前）；tid 为空=所有说说，only_unreplied=True 只看还没回的。"""
        sql = ("SELECT id,tid,comment_key,comment_id,author_uin,author_name,is_self,reply_to_key,"
               "reply_to_name,content,created_at,seen_at,replied,reply_key,flagged,"
               "reply_text,skip_reason "
               "FROM qzone_comments WHERE 1=1")
        args = []
        if str(tid or ""):
            sql += " AND tid=?"
            args.append(str(tid)[:80])
        if only_unreplied:
            sql += " AND replied=0"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        try:
            with self._lock:
                rows = self.conn.execute(sql, args).fetchall()
        except Exception as e:
            _log("qzone", "列出评论失败：%r" % (e,))
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["content"] = _dec(d.get("content") or "")
            d["reply_text"] = _dec(d.get("reply_text") or "")
            out.append(d)
        return out

    def mark_qzone_comment_replied(self, comment_key, reply_key=""):
        """标记这条评论已经回过（reply_key 记我们那条回评的 key，方便以后再删）。"""
        with self._lock:
            self.conn.execute("UPDATE qzone_comments SET replied=1, reply_key=? WHERE comment_key=?",
                              (str(reply_key or "")[:120], str(comment_key or "")[:120]))
            self.conn.commit()

    def flag_qzone_comment(self, comment_key, flagged=1):
        """给评论打标记（flagged=1 表示别再理它）。"""
        with self._lock:
            self.conn.execute("UPDATE qzone_comments SET flagged=? WHERE comment_key=?",
                              (1 if flagged else 0, str(comment_key or "")[:120]))
            self.conn.commit()

    # ---------- v0.44.0 空间：自动反馈 + 回评结果 ----------

    def update_qzone_post_stats(self, pid, likes=None, cmts=None):
        """回写这条说说在空间里的赞/评数：有互动的旧帖会被挑出来当正例。"""
        sets, args = [], []
        if likes is not None:
            sets.append("likes=?")
            args.append(int(likes or 0))
        if cmts is not None:
            sets.append("cmts=?")
            args.append(int(cmts or 0))
        if not sets:
            return
        args.append(int(pid or 0))
        try:
            with self._lock:
                self.conn.execute("UPDATE qzone_posts SET %s WHERE id=?" % ", ".join(sets), args)
                self.conn.commit()
        except Exception as e:
            _log("qzone", "回写赞评数失败：%r" % (e,))

    def set_qzone_comment_result(self, comment_key, replied, reply_text="", skip_reason=""):
        """回评结果落库：1=已回 / 2=主动跳过 / 3=尝试失败。

        以前失败是静默 continue，主人查不出「为什么这条没回」，现在一律留原因。
        """
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE qzone_comments SET replied=?, reply_text=?, skip_reason=? WHERE comment_key=?",
                    (int(replied or 0), _enc(str(reply_text or "")[:300]), str(skip_reason or "")[:120],
                     str(comment_key or "")[:120]))
                self.conn.commit()
        except Exception as e:
            _log("qzone", "写回评结果失败：%r" % (e,))

    def qzone_replied_authors(self, tid):
        """这条说说下已经成功回过的作者 QQ 集合：同一个人同一帖只回一次。"""
        out = set()
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT author_uin FROM qzone_comments WHERE tid=? AND replied=1 AND is_self=0",
                    (str(tid or "")[:80],)).fetchall()
        except Exception:
            return out
        for r in rows:
            u = str(r["author_uin"] or "").strip()
            if u:
                out.add(u)
        return out

    def known_names(self, limit=80):
        """最近聊过的人的名字：发公开说说前挡「把好友昵称写进去」。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT name FROM visible_people WHERE name<>'' ORDER BY last_seen DESC LIMIT ?",
                    (int(limit),)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            n = str(r["name"] or "").strip()
            if len(n) >= 2:
                out.append(n)
        return out

    def recent_qzone_replies(self, limit=10):
        """最近回过的评论正文：生成新回复时避免复读同一句话。"""
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT reply_text FROM qzone_comments WHERE replied=1 AND reply_text<>'' "
                    "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            s = str(_dec(r["reply_text"] or "") or "").strip()
            if s:
                out.append(s)
        return out
