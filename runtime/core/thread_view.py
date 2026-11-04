# -*- coding: utf-8 -*-
"""群聊线程视图：把「谁回复了谁 / 谁在接哪条 / 现在谁在说」画成结构化上下文。

对应 2026-10-22 用户给的方向（Thread Graph / SpeakerMem 的工程层做法）：
- 用显式的 reply 引用链做确定性聚类，不指望模型从一条平铺流水里脑补关系；
- 每条消息保留发送人 QQ、昵称、回复目标；同一个人换群名片也认 QQ；
- 只把当前这条所在的那一小段线程交给模型，不把整个群历史压平塞进去。

纯只读：不写库、不调模型、不发消息。
"""

import json
import re
import time

_IMAGE_NOTE = re.compile(r'[（(](?:画面|附图|自己发的图|这位说话人发的图|图片描述)[:：][^）)]*[）)]')

def text_has_visual_target(text):
    text=str(text or '')
    if re.search(r'这句|那句|这段话|那段话|文字|聊天记录',text) and not re.search(r'图里|图上|图片|截图|照片',text):
        return False
    return bool(re.search(r'图|照片|表情|画面|[这那几两三]张|这个|那个|看上面|看刚才|前面那个',text))

def focus_history(history, text, media_target=False):
    """Hide unrelated auto-generated image annotations in this view only; retain source history."""
    if media_target or len(str(text or '').strip())<4 or text_has_visual_target(text):
        return list(history or [])
    return [dict(row,content=_IMAGE_NOTE.sub('',row['content'])) if isinstance(row.get('content'),str) else dict(row)
            for row in history or []]


def select_image_references(rows, timeline, text, uid=0):
    """Resolve explicit media references without silently picking another person's last image."""
    text=str(text or '')
    rows=list(rows or [])
    image_words=bool(re.search(r'图|照片|截图|[这那几两三一]张',text))
    if not image_words and re.search(r'这句|那句|这段话|那段话|文字|聊天|记录|他说|她说',text):
        return [], ''
    if not rows:return [], '未找到被指向的原图；不猜画面，需要时确认是哪条消息。' if image_words else ''
    if re.search(r'你(?:刚才|刚|之前|前面)?(?:发|贴|传)',text):
        return [], '指的是你自己发过的图片，不能用其他人最近的图替代；结合自己的发送记录，不明确时请对方引用。'
    if not image_words:
        latest=(timeline or [])[-1:] or []
        if not latest or str(latest[0].get('message_id'))!=str(rows[-1].get('message_id')):
            return [], ''
        # Text followed by a sticker often expresses a reaction to that text.
        if len(timeline or [])>1 and timeline[-2].get('sender_id')==rows[-1].get('sender_id'):
            body=str(timeline[-2].get('text') or '')
            if body and not body.startswith(('[图片]','[表情]')):return [], ''
    if re.search(r'我(?:刚才|刚|之前|前面)?(?:发|贴|传)',text):
        rows=[r for r in rows if int(r.get('sender_id') or 0)==int(uid or 0)]
    else:
        named={int(r.get('sender_id') or 0) for r in rows if len(str(r.get('sender_name') or ''))>=2 and str(r['sender_name']) in text}
        if len(named)==1:rows=[r for r in rows if int(r.get('sender_id') or 0) in named]
        elif len(named)>1:return [], '同名或多个发图者，无法唯一确认；需要时请对方引用目标消息。'
    if not rows:return [], '没有找到所指发图者的原图；不要换成别人的图。'
    plural=bool(re.search(r'两张|这两|那两|几张|对比|比较|哪个|哪张',text))
    if re.search(r'最后|最新|最上|最下面',text):
        return ([rows[0]] if '最上' in text else [rows[-1]]), ''
    if len({r.get('sender_id') for r in rows})>1 and not plural:
        return [], '近期有不同人发图，当前指代不唯一；先确认是哪张，不自行选最新一张。'
    return rows[-2:] if plural else rows[-1:], ''


def _clean_body(text, limit=160):
    s = str(text or "")
    # 旧数据里可能把引用说明拼进了正文，展示时切掉
    s = re.split(r"\n?\[引用上下文", s, maxsplit=1)[0]
    # 旧回复可能以 [19:34] 开头（历史格式泄漏），展示时也剥掉
    s = re.sub(r"^\s*\[\d{1,2}:\d{2}\]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _hhmm(ts):
    try:
        return time.strftime("%H:%M", time.localtime(int(ts or 0)))
    except Exception:
        return "--:--"


def _quote_att(row):
    for a in row.get("attachments") or []:
        if isinstance(a, dict) and a.get("type") == "quote":
            return a
    return None


def _label(qq, name):
    qq = int(qq or 0)
    if not qq:
        return name or "群友"
    return "%s(%d)" % (name or "群友", qq)


def _row_ats(row, self_id):
    """从原始事件里解出这条消息 @ 了谁：返回 (别人QQ列表, 是否@自己, 是否@全体)。"""
    raw = row.get("raw") or ""
    if not raw:
        return [], False, False
    try:
        obj = json.loads(raw)
    except Exception:
        return [], False, False
    segs = obj.get("message") if isinstance(obj, dict) else None
    if not isinstance(segs, list):
        return [], False, False
    sid = str(int(self_id or 0)) if int(self_id or 0) else ""
    others, self_at, at_all, seen = [], False, False, set()
    for s in segs:
        if not isinstance(s, dict) or s.get("type") != "at":
            continue
        q = str((s.get("data") or {}).get("qq") or "").strip()
        if not q:
            continue
        if q.lower() == "all":
            at_all = True
            continue
        if sid and q == sid:
            self_at = True
        elif q not in seen:
            seen.add(q)
            others.append(q)
    return others, self_at, at_all


def build_thread_block(store, identity, conv, current_uid, current_name, text,
                       quote=None, segs=None, limit=30, trigger_name=""):
    """返回一段给系统提示的「群聊线程」；没有历史就返回空串。"""
    try:
        rows = store.thread_rows(conv, limit)
    except Exception:
        return ""
    if not rows:
        return ""
    if len(str(text or '').strip())>=4 and not text_has_visual_target(text) and not (quote or {}).get('images'):
        rows=[dict(row,text=_IMAGE_NOTE.sub('',str(row.get('text') or ''))) for row in rows]
    try:
        self_id = int(getattr(identity, "self_id", 0) or 0) if identity is not None else 0
    except Exception:
        self_id = 0
    try:
        configured_name = (getattr(identity, "cfg", None) or {}).get("bot_name")
    except Exception:
        configured_name = ""
    try:
        bot_name = identity.bot_name() if identity is not None else configured_name
    except Exception:
        bot_name = configured_name
    bot_name = re.sub(r"[\r\n\t]+", " ", str(bot_name or configured_name or "机器人")).strip()[:80] or "机器人"
    bot_names = [bot_name]
    short_name = str(trigger_name or "").strip()
    if short_name and short_name not in bot_names:
        bot_names.append(short_name)

    def has_bot_name(body):
        return any(n and n in str(body or "") for n in bot_names)

    row_names = {}
    for r in rows:
        try:
            q = int(r.get("sender_id") or 0)
        except Exception:
            q = 0
        if q and r.get("sender_name"):
            row_names[q] = str(r.get("sender_name"))

    def nm(qq, fallback=""):
        try:
            name = identity.name(qq, fallback) if identity is not None else (fallback or "")
        except Exception:
            name = fallback or ""
        # people 表还没这个人时，用这段聊天里出现过的群名片兜底（比裸 QQ 号好认）
        try:
            if (not name or str(name) == str(int(qq or 0))) and fallback:
                name = fallback
            if (not name or str(name) == str(int(qq or 0))) and int(qq or 0) in row_names:
                name = row_names[int(qq)]
        except Exception:
            pass
        return name or str(qq or "群友")

    # 在场的人（最近发过言的，按最近一次出现排序）
    speakers = []
    seen = set()
    for r in reversed(rows):
        if r.get("direction") != "in":
            continue
        q = int(r.get("sender_id") or 0)
        if not q or q in seen:
            continue
        seen.add(q)
        speakers.append((q, nm(q, r.get("sender_name"))))
    speakers = list(reversed(speakers))[-8:]

    # 当前这条 @ 了谁
    at_items = []
    try:
        at_items = identity.mention_names(segs or []) if identity is not None else []
    except Exception:
        at_items = []

    # 线程：有引用就从被引用那条开始，没有就取最近 8 条
    mid2row = {}
    for r in rows:
        try:
            mid2row[int(r.get("message_id") or 0)] = r
        except Exception:
            pass
    start = None
    qid = 0
    if isinstance(quote, dict) and quote.get("id"):
        try:
            qid = int(quote.get("id"))
        except Exception:
            qid = 0
        hit = mid2row.get(qid)
        if hit is not None:
            start = rows.index(hit)
    if start is None:
        thread = rows[-8:]
        thread_title = "最近 8 条（没有明确引用，按时间）"
    else:
        thread = rows[start:]
        if len(thread) > 10:
            thread = [thread[0]] + thread[-9:]
        thread_title = "被引用的消息及其后的时间线（可能有无关插话，不代表同一话题）"
    linked={qid} if qid else set()
    for r in rows:
        qa=_quote_att(r) or {}
        if qa.get('id') and int(qa['id']) in linked and r.get('message_id'):
            linked.add(int(r['message_id']))

    lines = ["\n\n# 群聊时间线（引用关系单独标出；相邻消息不一定属于同一话题）"]
    if speakers:
        who = []
        for q, n in speakers:
            tag = n
            if q == int(current_uid or 0):
                tag += "←现在说话"
            who.append(_label(q, tag))
        lines.append("- 最近在场的人：" + "、".join(who))
    # @ 关系：说清是 @ 你、@ 别人，还是都有
    if at_items:
        self_at = any(x[2] for x in at_items)
        others = [(q, nm(q, n), is_self) for q, n, is_self in at_items if not is_self]
        parts = []
        if self_at:
            parts.append("你")
        if others:
            parts.append("、".join(_label(q, n) for q, n, _ in others))
        lines.append("- 这条消息 @ 了：" + "；".join(parts) +
                     ("（你也在被 @ 的人里）" if self_at and others else
                      ("（只 @ 了你）" if self_at else "（没有 @ 你）")))
    else:
        lines.append("- 这条消息没有 @ 任何人。")

    lines.append("- 当前话题线：" + thread_title)
    for r in thread:
        who = ("你（%s）" % bot_name) if r.get("direction") == "out" else _label(r.get("sender_id"), nm(r.get("sender_id"), r.get("sender_name")))
        # v0.45.0：昵称撞车的号（另一个大肥鱼）按 QQ 标出来，别让模型把它的发言当成自己。
        if r.get("direction") != "out":
            try:
                _q = int(r.get("sender_id") or 0)
            except Exception:
                _q = 0
            _nm = str(r.get("sender_name") or "")
            if _q and _q != self_id and any(n and n in _nm for n in bot_names):
                who += "（同名号，不是你）"
        body = _clean_body(r.get("text"))
        if not body:
            continue
        qa = _quote_att(r)
        extra = ""
        if qa:
            try:
                target = "你（机器人）" if qa.get("is_bot") or qa.get("is_self") else ("原作者未知" if not qa.get("who_qq") and (not qa.get("who_name") or qa.get("who_name") == "未知") else _label(qa.get("who_qq"), nm(qa.get("who_qq"), qa.get("who_name") or qa.get("name"))))
                extra = "（引用 %s 以前发的「%s」，不是本条发送者的新话）" % (target, _clean_body(qa.get("text"), 50))
            except Exception:
                extra = ""
        mark = "↩ " if qid and int(r.get("message_id") or 0) == qid else ""
        if qid:
            mark += '【当前引用链】' if int(r.get('message_id') or 0) in linked else '【旁支，不自动接续】'
        # 这条 @ 的是谁：@你 / @别人 / @全体，直接写在行尾，避免模型把别人的话当成叫它
        at_tag = ""
        try:
            others, self_at, at_all = _row_ats(r, self_id)
            if r.get("direction") == "in":
                if self_at and others:
                    at_tag = "（@了你，也@了%s）" % "、".join(nm(q) for q in others)
                elif self_at:
                    at_tag = "（@了你）"
                elif others:
                    at_tag = "（@了%s，不是对你）" % "、".join(nm(q) for q in others)
                elif at_all:
                    at_tag = "（@全体）"
        except Exception:
            at_tag = ""
        lines.append("- %s %s%s：%s%s%s" % (_hhmm(r.get("ts")), mark, who, body, extra, at_tag))
    lines.append("- 现在（%s）%s：%s" % (_hhmm(time.time()),
                                       _label(current_uid, nm(current_uid, current_name)),
                                       _clean_body(text, 200)))

    # 用户 2026-10-22 指定的场景：别人先 @ 别人说事，后面有人叫本鲸却没说要干嘛，
    # 模型会把前面那句 @ 别人的话当成对自己说的。这里显式标出“最近一条真正对你说的”。
    t = re.sub(r"\s+", "", str(text or ""))
    qtext = ""
    if isinstance(quote, dict) and not quote.get("error"):
        qtext = " ".join(str(quote.get("text") or "").split())[:200]
        if qtext in ("[图片]", "（内容为空）"):
            qtext = ""
    vague = (not t) or len(t) <= 3 or t in tuple(bot_names) + ("在吗", "在", "喂", "你", "？", "?", "叫我", "干嘛", "怎么了")
    if vague:
        last_i = None
        for i in range(len(rows) - 1, -1, -1):
            r = rows[i]
            if r.get("direction") != "in":
                continue
            if int(r.get('sender_id') or 0)!=int(current_uid or 0):
                continue
            others, self_at, at_all = _row_ats(r, self_id)
            qa = _quote_att(r)
            to_bot = bool(self_at or (qa and (qa.get("is_self") or qa.get("is_bot"))))
            if not to_bot and not others and not at_all and has_bot_name(_clean_body(r.get("text"), 200)):
                to_bot = True
            if to_bot:
                last_i = i
                break
        lines.append("")
        lines.append("# 叫你没说清楚时，先分清哪句是跟你说的")
        if last_i is not None:
            r = rows[last_i]
            lines.append("- 当前这个人最近明确对你说过的（可能已答过，不能当新要求）：%s %s：%s" % (
                _hhmm(r.get("ts")),
                _label(r.get("sender_id"), nm(r.get("sender_id"), r.get("sender_name"))),
                _clean_body(r.get("text"), 120)))
            others_n = 0
            for r2 in rows[last_i + 1:]:
                if r2.get("direction") != "in":
                    continue
                o2, s2, a2 = _row_ats(r2, self_id)
                qa2 = _quote_att(r2)
                to_bot2 = bool(s2 or (qa2 and (qa2.get("is_self") or qa2.get("is_bot"))))
                if not to_bot2 and not (not o2 and not a2 and has_bot_name(_clean_body(r2.get("text"), 200))):
                    others_n += 1
            if others_n:
                lines.append("- 在它之后有 %d 条不是对你说的（有的 @ 了别人，有的是接着别人的话），不是叫你。" % others_n)
        else:
            lines.append("- 上面没有一条明确是对你说的；别把别人互相对话当成叫你。")
        if qtext:
            lines.append("- 这条他只 @ 了你、没打字，但引用了：「%s」；被引用的内容就是这次要接的东西，"
                         "按它接话，别问「叫我干嘛」。" % qtext[:160])
        else:
            lines.append("- 这条你只是被叫到、没看懂要干嘛：不要拿上面别人 @ 别人的话当指令；"
                         "直接问一句「叫我干嘛 / 要我接哪句」就行，别硬接。")
    lines.append("- 读法：名字后面括号里是 QQ 号，同一个人换了群名片也认这个号；"
                 "QQ 号不是你的同名号是别的号，它做的事不是你做的；"
                 "「引用 X 以前发的…」标出旧消息原作者，不是引用者的新话；"
                 "「@了你」才是对你说的，「@了X，不是对你」是别人之间的话，别当指令；越靠下越新。"
                 "这条消息到底是不是在跟你说话，系统已经判定过了；没叫你就别插嘴。")
    return "\n".join(lines)
