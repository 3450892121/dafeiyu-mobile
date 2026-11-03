# -*- coding: utf-8 -*-
"""v0.56.0 出站护栏：系统提示片段 / 内部注记 / 余额金额 / 死亡意象 / 文本化表情。

只在“发出去之前”用，不改人设、不改语气。三层：
- scrub(text)：能就地修的（把「（表情：害羞）」换成 [脸:害羞]）先修；
- check(text)：不能发的直接拦（内部设定、内部注记、金额、死亡殡葬词）；
- time_issue(text)：说的时间和现在矛盾（例如早上七点说“都快中午了”）。
外部统一入口是 flow_reply._send()，任何发送路径都过这一道。
"""
import re
import time
import hashlib
import threading
import base64
import urllib.parse

_PRIVATE_RULE_FINGERPRINTS = set()
_PRIVATE_RULE_LOCK = threading.Lock()


def _compact_private(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', str(text).lower())


def register_private_rules(text):
    """Remember hashes of instruction excerpts, not chats, secrets or full prompts."""
    values = set()
    for line in str(text or '').splitlines():
        compact = _compact_private(line)
        if len(compact)<36 or '{' in line or '"target_id"' in line:
            continue
        for i in range(len(compact)-31):
            values.add(hashlib.sha256(compact[i:i+32].encode()).digest())
    with _PRIVATE_RULE_LOCK:
        if len(_PRIVATE_RULE_FINGERPRINTS)+len(values)<=24000:
            _PRIVATE_RULE_FINGERPRINTS.update(values)


def _private_rule_copy(text):
    compact = _compact_private(text)
    if len(compact)<32:
        return False
    with _PRIVATE_RULE_LOCK:
        return any(hashlib.sha256(compact[i:i+32].encode()).digest() in _PRIVATE_RULE_FINGERPRINTS
                   for i in range(len(compact)-31))


def content_allowed(bot, text, internal=False):
    """Common last gate for text, speech input, generated docs and public posts."""
    from safety import allow_outbound
    if not isinstance(text,str) or not text.strip() or len(text)>200000:
        return False
    cfg=getattr(bot,'cfg',{})
    keys=[]
    try:
        fn=getattr(bot,'_outbound_keys',None)
        if fn:
            keys.extend(fn())
        for key in ('api_key','onebot_token','admin_token','cover_token','voice_token'):
            value=cfg.get(key)
            if isinstance(value,str) and value:
                keys.append(value)
        models=getattr(cfg,'models',None)
        if models:
            keys.extend(m.get('api_key') for m in models() if m.get('api_key'))
        def secrets(value):
            if isinstance(value,dict):
                for name,item in value.items():
                    if re.search(r'api[_-]?key|token$|secret|password',str(name),re.I) and isinstance(item,str) and item:
                        keys.append(item)
                    elif isinstance(item,(dict,list)):
                        secrets(item)
            elif isinstance(value,list):
                for item in value:secrets(item)
        secrets(getattr(cfg,'data',cfg))
    except Exception:
        return False
    variants=list(keys)
    for key in keys:
        if len(str(key))>=8:
            raw=str(key).encode()
            variants.extend((base64.b64encode(raw).decode(),base64.urlsafe_b64encode(raw).decode(),raw.hex(),urllib.parse.quote(str(key),safe='')))
    if not allow_outbound(text,keys=variants)[0]:
        return False
    compact=re.sub(r'\s+','',text)
    if any(len(str(key))>=12 and re.sub(r'\s+','',str(key)) in compact for key in keys):
        return False
    if internal:
        return True
    if (re.search(r'#\s*同一个人的跨场合接续资料|scene_events|scene_sources', text) or
            re.search(r'"(?:scene_ref|earlier_ref|person_continuity|scene_continuity)"\s*:', text)):
        return False
    if (re.search(r'#\s*没说出口的心事|inner_impressions', text) or
            re.search(r'"feeling"\s*:[\s\S]*"(?:shareable|share_kind|ts)"\s*:|'
                      r'"ts"\s*:[\s\S]*"feeling"\s*:', text)):
        return False
    if re.search(r'"(?:experience_ids|counter_ids|provided_ids|source_fingerprint|topic_key)"\s*:|'
                 r'#\s*(?:我目前的样子|事实、权限与输出边界)|self_current|self_experiences',text):
        return False
    if re.search(r'"stance"\s*:[\s\S]*"status"\s*:',text):
        return False
    if re.search(r'/(?:opt|home/dafeiyu|var/backups)/|127\.0\.0\.1:\d+|Traceback \(most recent call last\)',text):
        return False
    return check(text)['action']=='ok'

# 整条就是一句括号里的内部备注：（这条没 @ 我，装死摸鱼中）
NOTE_ONLY_RE = re.compile(r"^\s*[（(][^（）()]{0,80}[)）]\s*$")
NOTE_WORDS_RE = re.compile(r"(没\s*@\s*我|没艾特|装死|摸鱼中|跳过|不接这|待命|等待回复|内部)")

# 模型把系统提示/内部要求复述出来
INTERNAL_RE = re.compile(
    r"(先带刺|别用旧称|回一句\s*[:：]|(?:我的|你的|内部的?|完整的?)\s*(?:系统提示|提示词|人设)|"
    r"(?:^|\n)\s*#?\s*(?:系统提示|内部提示|系统要求)\s*[:：]|以下是你的人设|"
    r"按以下格式|不要复述|这条指令|你的设定是)")

# 有时模型把决定怎样回复的自言自语混进正文；组合匹配以免拦普通的「别追问」。
REPLY_PLANNING_RE = re.compile(
    r"<\s*/?\s*(?:think|thinking|analysis|reasoning)\b[^>]*>|"
    r"[（(【\[]\s*(?:不用|不要|别)(?:列点|分点|列举|写太长)[^）)】\]\n]{0,50}[）)】\]]|"
    r"(?:其实|这里|本轮|这次)(?:就|只)(?:用|要|需)?(?:一句话|一两句|简短回复).{0,35}"
    r"(?:不要|不用|别|避免)(?:再|过多|太多|反复)?(?:追问|提问|解释|展开)|"
    r"(?:回复|回答|输出)(?:要求|策略|思路|规划)\s*[:：]|"
    r"(?:可以|应该|不妨|先|要不要).{0,15}(?:问|说|提).{0,60}"
    r"(?:别|不要|不许|不能).{0,12}(?:追问|翻旧账|提旧事)", re.S | re.I)

# 账户余额/金额：机器人不该知道，也不该说
MONEY_RE = re.compile(r"(余额|赠金|充值|账户|账单|扣费|欠费|人民币)")

# 死亡/殡葬意象：任何出站消息都不许有（笑死、忙死这类不在词表里）
DEATH_RE = re.compile(
    r"(坟头|坟墓|墓地|墓碑|殡仪|殡|停尸|灵堂|太平间|棺材|烧纸|头七|陪葬|"
    r"躺尸|上吊|自缢|跳楼|卧轨|割腕|自杀|轻生|遗书|天台|房梁)")

# 模型把表情写成文字：（表情：害羞）
FACE_TEXT_RE = re.compile(r"[（(]\s*表情\s*[:：]\s*([^（）()]{1,12})\s*[)）]")
BRACKET_FACE_RE = re.compile(r"\[表情\s*[:：]\s*([^\]\n]{1,12})\]")

# 括号里写给自己看的“配图 / 表情包”备注：（附图可自动配）（配图：系统自动配）
# 这是内部注记，不该出现在聊天里；整段括号删掉。正常聊天括号（真的）（不是吧）不受影响。
_IMG_NOUN = r"(?:附图|配图|图片|图|表情包|贴图|动图|照片)"
_IMG_NOTE_WORD_RE = re.compile(
    r"(?:可自动|会自动|自动配|自动发|系统配|系统发|由系统|系统会|"
    r"不用写|无需写|不需要写|替你配|替你发)")
_IMG_RECORD_RE = re.compile(
    r"^(?:自己发的图|这位说话人发的图|(?:你|我)?(?:同时|还|又|另外)?(?:发|甩|丢|扔|贴|附)(?:了)?(?:一|两|几|这|那)?"
    r"(?:张|个|条|幅)?(?:的)?" + _IMG_NOUN + r"|" + _IMG_NOUN + r")\s*(?:[:：]|$)")
# store._render_attachment_notes 的归属标签；不把普通的「画面：…」回答当泄漏。
HISTORY_IMAGE_NOTE_RE = re.compile(r"(?:自己发的图|这位说话人发的图)\s*[:：]")
_IMAGE_NOTE_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]"}
_UNFINISHED_IMAGE_NOTE_RE = re.compile(
    r"[（(【\[]\s*(?:自己发的图|这位说话人发的图)\s*[:：][\s\S]*$")

# 历史归属由 response_to 附件提供；程序尾注不是机器人曾说过的话。
_RESPONSE_NOTE_START = r"(?:(?:此|本)(?:条)?|这(?:条|次))回复的?接话对象\s*[:：]"
RESPONSE_NOTE_RE = re.compile(_RESPONSE_NOTE_START)
_UNFINISHED_RESPONSE_NOTE_RE = re.compile(r"[（(【\[]\s*" + _RESPONSE_NOTE_START + r"[\s\S]*$")
_SHORT_RESPONSE_NOTE_START = r"(?:回复的?)?接话对象\s*[:：]"
_RESPONSE_NOTE_PREFIX_RE = re.compile(_RESPONSE_NOTE_START + "|" + _SHORT_RESPONSE_NOTE_START)
_RESPONSE_NOTE_ID_RE = re.compile(r"QQ\s*[:：]?\s*\d{3,15}|对应来话\s*#", re.I)
_TRUNCATED_RESPONSE_NOTE_RE = re.compile(
    r"[（(【\[]\s*(?:(?:此|本)(?:条)?|这(?:条|次))回复的?接话对(?:象)?\s*[:：]?\s*$")


def _response_note_start(text):
    """Short labels need a second program marker; ordinary addressee discussion stays."""
    text = str(text or '')
    if re.match(r"\s*" + _RESPONSE_NOTE_START, text):
        return True
    match = re.match(r"\s*" + _SHORT_RESPONSE_NOTE_START, text)
    if not match:
        return False
    tail = re.split(r"[。！？!\n]", text[match.end():match.end() + 200], maxsplit=1)[0]
    return bool(_RESPONSE_NOTE_ID_RE.search(tail))


def contains_response_notes(text):
    """Inspect the actual payload, including bare/short or clearly truncated labels."""
    text = str(text or '')
    return bool(_TRUNCATED_RESPONSE_NOTE_RE.search(text) or any(
        _response_note_start(text[match.start():]) for match in _RESPONSE_NOTE_PREFIX_RE.finditer(text)))

_DAYPARTS = ((0, 5, "凌晨"), (5, 9, "早上"), (9, 11, "上午"), (11, 13, "中午"),
             (13, 17, "下午"), (17, 19, "傍晚"), (19, 23, "晚上"), (23, 24, "深夜"))
_WEEK = "一二三四五六日"
_CLAIM_RE = re.compile(r"(?:都|快|已经|现在|这么)?\s*(凌晨|早上|清晨|上午|中午|下午|傍晚|晚上|夜里|半夜|深夜)\s*(?:了|啦)?")
_WEEK_CLAIM_RE = re.compile(r"今天(?:是)?(?:周|星期)([一二三四五六日天])")


def daypart(hour=None):
    """把 0~23 点换成时段词。"""
    h = int(time.localtime().tm_hour if hour is None else hour)
    for a, b, name in _DAYPARTS:
        if a <= h < b:
            return name
    return "深夜"


def now_text(ts=None):
    """标准时间行：现在是 2026-10-26 周一 早上 07:03（北京时间）。"""
    t = time.localtime(ts)
    return "现在是 %04d-%02d-%02d 周%s %s %02d:%02d（北京时间）" % (
        t.tm_year, t.tm_mon, t.tm_mday, _WEEK[t.tm_wday], daypart(t.tm_hour), t.tm_hour, t.tm_min)


def _paren_spans(text, pairs=None):
    """列出配对括号的 (start, end, inner)，end 不含；不配对的忽略。"""
    pairs = pairs or {"（": "）", "(": ")"}
    closes = {v: k for k, v in pairs.items()}
    stack = []
    out = []
    for i, ch in enumerate(text):
        if ch in pairs:
            stack.append((ch, i))
        elif ch in closes and stack and stack[-1][0] == closes[ch]:
            _, start = stack.pop()
            out.append((start, i + 1, text[start + 1:i]))
    return out


def strip_response_notes(text):
    """只清理有明确内部前缀的括号尾注，支持昵称和 QQ 号的嵌套括号。"""
    s = str(text or "")
    if not contains_response_notes(s):
        return s
    spans = [(start, end) for start, end, inner in _paren_spans(s, _IMAGE_NOTE_PAIRS)
             if _response_note_start(inner)]
    out, pos = [], 0
    for start, end in sorted(spans):
        if start < pos:
            continue
        out.append(s[pos:start])
        pos = end
    out.append(s[pos:])
    cleaned = _UNFINISHED_RESPONSE_NOTE_RE.sub("", "".join(out))
    # An unfinished short label is removable only with its second program marker.
    for match in re.finditer(r"[（(【\[]\s*(?:" + _SHORT_RESPONSE_NOTE_START + r")", cleaned):
        if _response_note_start(cleaned[match.start() + 1:]):
            cleaned = cleaned[:match.start()]
            break
    if cleaned == s:
        return s
    return re.sub(r"[，,、\s]+$", "", cleaned).strip()


def strip_image_notes(text):
    """删掉括号里写给自己看的配图 / 表情包备注；其余原样返回。

    （附图可自动配）→ 删；（配图：系统自动配）→ 删；
    （真的）（不是吧）（图是昨天拍的）→ 保留。
    """
    s = str(text or "")
    if not s or ("图" not in s and "表情" not in s and "照片" not in s):
        return s
    spans = []
    for start, end, inner in _paren_spans(s, _IMAGE_NOTE_PAIRS):
        core = inner.strip()
        if not core:
            continue
        if _IMG_RECORD_RE.match(core):
            spans.append((start, end))
            continue
        if re.search(_IMG_NOUN, core) and _IMG_NOTE_WORD_RE.search(core):
            spans.append((start, end))
    if not spans:
        # 长度压缩或模型输出截断可能丢掉右括号；只认明确的历史归属标签。
        trimmed = _UNFINISHED_IMAGE_NOTE_RE.sub("", s)
        return trimmed.rstrip() if trimmed != s else s
    spans.sort()
    out = []
    pos = 0
    for start, end in spans:
        if start < pos:
            continue
        out.append(s[pos:start])
        pos = end
    out.append(s[pos:])
    s = "".join(out)
    s = _UNFINISHED_IMAGE_NOTE_RE.sub("", s)
    s = re.sub(r"[（(]\s*[）)]", "", s)
    s = re.sub(r"[，,、]\s*([。！？!?])", r"\1", s)
    s = re.sub(r"[，,、\s]+$", "", s)
    return s.strip()


# v0.70.2：历史里给模型自己看的尾注「（这条是用语音说的）」，模型会照抄进回复——
# 2026-10-29 12:25 群聊真机：18 秒语音末尾真的念出了「这条是用语音说的」，用户截图问「发现什么问题了吗」。
# 这是程序标注，不是正文；出站前括号版和句尾版都剥掉，TTS 也念不到。
_VOICE_NOTE_TAIL_RE = re.compile(
    r"[，,；;\s]*(?:这|本)(?:条|句)(?:消息|回复)?\s*(?:(?:是|为)\s*)?(?:用|拿)?\s*"
    r"(?:语音|嘴)(?:条|消息)?\s*(?:说的|回复的|发的)[。！!]?\s*$"
)


def strip_voice_notes(text):
    """删掉模型从历史里抄来的「（这条是用语音说的）」「这条是用语音说的。」这类标注。"""
    s = str(text or "")
    if not s or ("语音" not in s and "嘴" not in s):
        return s
    spans = []
    for start, end, inner in _paren_spans(s):
        core = inner.strip()
        if not core or len(core) > 20:
            continue
        if re.search(r"语音|用嘴", core) and re.search(r"这|本|条|句|发|说|回|消息", core):
            spans.append((start, end))
    if spans:
        spans.sort()
        out, pos = [], 0
        for start, end in spans:
            if start < pos:
                continue
            out.append(s[pos:start])
            pos = end
        out.append(s[pos:])
        s = "".join(out)
    s = _VOICE_NOTE_TAIL_RE.sub("", s)
    s = re.sub(r"[（(]\s*[）)]", "", s)
    s = re.sub(r"[，,、\s]+$", "", s)
    return s.strip()


MEDIA_TAG_RE = re.compile(r"[\[【（(]\s*(?:语音条?|语音回复|图片|照片|表情包|文件|视频|语音消息)\s*[\]】）)]")
LEADING_VOICE_NOTE_RE = re.compile(r"^\s*(?:[（(]\s*(?:用|这条是)?\s*语音(?:说|回的)?\s*[）)]|语音说[:：])\s*")


def scrub(text):
    """能就地修的：文本化表情换成 [脸:x]、括号里的配图备注删掉、工具调用残留删掉、去多余空行。"""
    # 回复方式是内部控制行，完整/截断标记均不能发到文字或被 TTS 念出。
    text = re.sub(r"\[\[\s*reply_mode\b[^\]\n]*(?:\]\]|\]|\n|$)", "", text, flags=re.I)
    # v0.66.0 真机踩到：模型把工具调用当正文发出来了
    # （<tool_call>notebook_write</tool_call> 后面跟一行 {"content": ...}），用户看到一串机器码。
    # 模型有时用 <tool_call>，有时用 <tool_use>，两种都要剥；空标签和带内容的都处理
    text = re.sub(r"<\s*tool_(?:call|use|result)\b[^>]*>.*?<\s*/\s*tool_(?:call|use|result)\s*>",
                  "", text, flags=re.S)
    text = re.sub(r"<\s*tool_(?:call|use|result)\b[^>]*>[\s\S]*$", "", text)
    text = re.sub(r"<\s*/?\s*tool_(?:call|use|result)\b[^>]*>", "", text)
    text = re.sub(r'(?m)^\s*\{\s*"(?:content|name|arguments|tool|parameters)"\s*:.*\}\s*$', "", text)
    s = strip_response_notes(text)
    s = strip_image_notes(s)
    s = strip_voice_notes(s)
    s = s.replace("（梦境或梦境引用，情节不作现实事实）", "")
    if not s:
        return s
    # v0.69.2：模型会照着历史里的「[语音] 原话」把标记抄进正文（真机截图：
    # 发出去变成「[语音] 这标题…」）。这些是内部标记，一律剥掉。
    s = MEDIA_TAG_RE.sub("", s)
    s = LEADING_VOICE_NOTE_RE.sub("", s)
    s = re.sub(r"^[\s，,、：:]+", "", s)
    # （表情：害羞）→ [脸:害羞]；名字太长/带标点的直接删
    def _face(m):
        name = (m.group(1) or "").strip()
        if 1 <= len(name) <= 6 and not re.search(r"[，。！？,!.?\s]", name):
            return "[脸:%s]" % name
        return ""
    s = FACE_TEXT_RE.sub(_face, s)
    s = BRACKET_FACE_RE.sub(lambda m: "[脸:%s]" % (m.group(1) or "").strip()[:6], s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def check(text):
    """能不能发。返回 {"action": "ok"|"drop", "reason": str}。"""
    # A boolean validator cannot approve an unsanitized payload merely because its
    # private copy was scrubbed. Callers that want to retain prose must clean it first.
    if contains_response_notes(text):
        return {"action": "drop", "reason": "内部回复归属注记"}
    s = scrub(text)
    if not s:
        return {"action": "drop", "reason": "空消息"}
    if _private_rule_copy(s):
        return {"action":"drop","reason":"泄露内部提示"}
    if NOTE_ONLY_RE.match(s) and NOTE_WORDS_RE.search(s):
        return {"action": "drop", "reason": "内部注记"}
    if INTERNAL_RE.search(s):
        return {"action": "drop", "reason": "泄露内部提示"}
    if REPLY_PLANNING_RE.search(s):
        return {"action": "drop", "reason": "泄露回复判断"}
    if RESPONSE_NOTE_RE.search(s):
        return {"action": "drop", "reason": "内部回复归属注记"}
    if HISTORY_IMAGE_NOTE_RE.search(s):
        return {"action": "drop", "reason": "内部图片注记"}
    if DEATH_RE.search(s):
        return {"action": "drop", "reason": "死亡殡葬词"}
    if MONEY_RE.search(s) and re.search(r"\d", s):
        return {"action": "drop", "reason": "账户金额"}
    return {"action": "ok", "reason": ""}


def rewrite_hint(reason):
    """让模型重说一遍时给的话。"""
    why = {
        "泄露内部提示": "别复述任何设定、要求、格式；只把正事用自己的口气说一遍。",
        "内部注记": "别写括号里的判断和备注；要说就直接说人话。",
        "泄露回复判断": "不要说出你在考虑如何回复、提醒、记忆或遵守什么规则；直接自然地接话。",
        "内部回复归属注记": "删掉接话对象、对应来话编号等程序注记，只说给对方的正文。",
        "内部图片注记": "删掉自己发的图、这位说话人发的图及后面的画面备注；只说你要对对方说的话。",
        "死亡殡葬词": "不许提死、坟、殡、停尸、天台、割腕这类词；换个说法。",
        "账户金额": "不许提余额、金额、账户、账单；说 token 数可以，钱一个字都别提。",
    }.get(str(reason or ""), "换个说法，短一点。")
    return ("你刚才那句话发不出去（%s）。%s 用你平时的口气重说一遍，10~40 字，只输出这一句。"
            % (reason or "不合适", why))


def time_issue(text, now=None):
    """回复里的时间和现在矛盾就返回原因，否则空串。只认明确说法，不误伤。"""
    s = str(text or "")
    if not s:
        return ""
    t = time.localtime(now)
    hour = int(t.tm_hour)
    cur = daypart(hour)
    for m in _CLAIM_RE.finditer(s):
        said = m.group(1)
        # 只处理“都中午了/快晚上了/现在已经凌晨”这类在描述当前的
        prefix = s[max(0, m.start() - 2):m.start()]
        if not re.search(r"(都|快|已经|现在|这么|又)", prefix) and not re.search(r"(了|啦)", s[m.start():m.end() + 1]):
            continue
        ok = False
        if said == "中午":
            ok = 10 <= hour < 14
        elif said in ("下午",):
            ok = 12 <= hour < 18
        elif said in ("傍晚",):
            ok = 16 <= hour < 20
        elif said in ("晚上", "夜里"):
            ok = 16 <= hour < 24 or hour < 2
        elif said in ("半夜", "深夜"):
            ok = hour >= 22 or hour < 2
        elif said == "凌晨":
            ok = hour < 6
        elif said in ("早上", "清晨"):
            ok = 4 <= hour < 11
        elif said == "上午":
            ok = 7 <= hour < 12
        else:
            continue
        if not ok:
            return "说的是「%s」，现在是%s" % (said, cur)
    m = _WEEK_CLAIM_RE.search(s)
    if m:
        said = m.group(1)
        if said == "天":
            said = "日"
        if said != _WEEK[t.tm_wday]:
            return "说的是「今天周%s」，实际周%s" % (said, _WEEK[t.tm_wday])
    if "周末" in s and t.tm_wday <= 3:
        # 只说“周末要好好休息/约了人”这种计划不算说错；说“快周末了/这周末都睡过头”才拦
        if not re.search(r"(打算|计划|安排|准备|约|去|玩|好好|一起|有空|想要?|能|可以|回来|见面|电影|吃饭|愉快|快乐)", s):
            return "说的是「周末」，实际周%s" % _WEEK[t.tm_wday]
    return ""


def strip_time_claims(text):
    """把时间和现在矛盾的那一小句删掉；删完没内容就返回空。"""
    s = str(text or "")
    if not s:
        return ""
    parts = re.split(r"(?<=[。！？!?\n])", s)
    keep = []
    for p in parts:
        if p.strip() and time_issue(p):
            continue
        keep.append(p)
    out = "".join(keep).strip()
    return out

