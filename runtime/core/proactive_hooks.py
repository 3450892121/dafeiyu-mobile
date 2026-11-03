# -*- coding: utf-8 -*-
"""主动聊天的「由头」：先有真实素材，再决定开不开口。

只用本地库，不额外调模型。由头分四类：

- thread：私聊里他上次说的话（2 天内强、7 天内中）
- memory：人物记忆里他提过的事（3 天内强、30 天内中，更早的弱）
- group ：他在允许群里最近说的话（1 天内强、7 天内中）
- time   ：时间场合（饭点/深夜/周末，弱）

强度决定谁能用（在 flow_proactive 里筛）：
hot（24 小时内聊过）强/中/弱都行；warm（一周内）强/中；cold（更久）只要强；
连续两次没回也只要强——没由头就宁可不说。

每条由头带一个稳定的 key（消息/记忆 id），flow_proactive 会记住 24 小时内
用过的 key；生成之后还会做一次「话题重叠」检查，避免换个说法又问同一件事。
"""

import re
import private_contact

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"

HOOK_LABELS = {
    "thread": "上次没聊完的事",
    "memory": "他提过的事",
    "group": "群里说过的话",
    "time": "时间场合",
}

STRENGTH_RANK = {STRONG: 0, MEDIUM: 1, WEAK: 2}
MAX_PER_SOURCE = 2
MAX_HOOKS = 6

_NOISE = re.compile(r"^(\[[^\]]{0,12}\]\s*)+$")
_PUNCT = re.compile(r"[^0-9a-z\u4e00-\u9fa5]+")
# 只算「有信息量的字」，去掉语气词/代词/虚词；不然两句话都带「你我他」会误判
_STOP = set("的了呢吗啊吧哦呀哈你我他她它这那就都也还很太又再才刚把被和跟与及或是有没在不太会说着过给对从到")


def clean_text(text, n=90):
    s = " ".join(str(text or "").split())
    return s[:n]


def is_noise(text):
    """纯 [图片]/[表情] 这种占位不算由头。"""
    t = str(text or "").strip()
    if len(t) < 6:
        return True
    return bool(_NOISE.match(t))


def _content_chars(text):
    s = _PUNCT.sub("", str(text or "").lower())
    return {c for c in s if c not in _STOP}


def topic_overlap(a, b):
    """两句话的话题重合度（只算有信息量的字）。0~1，越大越像在说同一件事。"""
    sa, sb = _content_chars(a), _content_chars(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


def _hook(kind, key, strength, ts, material, hint):
    return {"type": kind, "key": key, "strength": strength, "src_ts": int(ts or 0),
            "material": material, "hint": hint}


def _thread_hooks(store, cand, now):
    """私聊里他上次说的话：最像「接着上次的话说」。"""
    out = []
    try:
        rows = store.thread_rows(cand["conv"], 24)
    except Exception:
        rows = []
    for r in reversed(rows):
        if str(r.get("direction") or "") != "in":
            continue
        text = clean_text(r.get("text"))
        if private_contact.boundary(text, now)[0]:
            continue  # 晚安/忙碌/不打扰是收尾，不是可以追问的“未完话题”。
        if is_noise(text):
            continue
        ts = int(r.get("ts") or 0)
        age = now - ts if ts else 10 ** 9
        if age > 7 * 24 * 3600:
            continue
        strength = STRONG if age <= 2 * 24 * 3600 else MEDIUM
        out.append(_hook("thread", "thread:%s" % (r.get("id") or ts), strength, ts,
                         "他上次说：「%s」" % text,
                         "接着这件事问后续，或者说说你自己的想法；别原样复述他的话。"))
        if len(out) >= MAX_PER_SOURCE:
            break
    return out


def _memory_hooks(store, cand, now):
    """人物记忆里他提过的事：隔了几天也能自然想起来。"""
    out = []
    try:
        mems = store.list_person_memories(cand.get("uid") or 0, 40)
    except Exception:
        mems = []
    for m in mems or []:
        text = clean_text(m.get("text"))
        if is_noise(text) or len(text) < 8:
            continue
        created = int(m.get("created") or 0)
        age = now - created if created else 10 ** 9
        if age <= 3 * 24 * 3600:
            strength = STRONG
        elif age <= 30 * 24 * 3600:
            strength = MEDIUM
        else:
            strength = WEAK
        out.append(_hook("memory", "mem:%s" % m.get("id"), strength, created,
                         "他提过：「%s」" % text,
                         "关心一下这件事的进展，说一句你自己的看法；别背履历、别一口气问好几个问题。"))
        if len(out) >= MAX_PER_SOURCE:
            break
    return out


def _group_hooks(store, cand, now, allow_groups):
    """他在允许群里最近说的话：私聊里接着说，最像「刚看见」。"""
    out = []
    try:
        rows = store.person_recent_messages(cand.get("uid") or 0, limit=12,
                                            since=now - 7 * 24 * 3600,
                                            exclude_conv=cand["conv"])
    except Exception:
        rows = []
    allowed = set(int(x) for x in (allow_groups or []) if int(x or 0) > 0)
    for r in rows or []:
        conv = str(r.get("conv") or "")
        if not conv.startswith("g:"):
            continue
        if allowed:
            try:
                if int(conv[2:] or 0) not in allowed:
                    continue
            except Exception:
                continue
        text = clean_text(r.get("text"))
        if is_noise(text):
            continue
        ts = int(r.get("ts") or 0)
        age = now - ts if ts else 10 ** 9
        if age > 7 * 24 * 3600:
            continue
        strength = STRONG if age <= 24 * 3600 else MEDIUM
        _who = str(r.get("sender_name") or "").strip()[:16] or "他"
        out.append(_hook("group", "grp:%s" % (r.get("id") or ts), strength, ts,
                         "「%s」刚在群里说：「%s」" % (_who, text),
                         "你也在群里看到了，私聊接着这个话头；别说「我刚看到」这种客套。"))
        if len(out) >= MAX_PER_SOURCE:
            break
    return out


def _time_hook(conv, now, hour, weekday, day):
    """时间场合：最弱的由头，只给最近常聊的人。"""
    label = ""
    if 11 <= hour < 14:
        label = "到饭点了"
    elif 17 <= hour < 20:
        label = "晚饭点"
    elif hour >= 22 or hour < 2:
        label = "这么晚了"
    elif weekday >= 5 and 9 <= hour < 22:
        label = "周末"
    if not label:
        return None
    return _hook("time", "time:%s:%s:%s" % (conv, label, day), WEAK, now,
                 "现在是%s" % label,
                 "随口说一句你此刻在干嘛，或者问问他今天怎么过；别问「在吗」。")


def build_hooks(store, cand, now, day, hour, weekday, allow_groups=None, used_keys=None,
                group_hooks=True):
    """返回按强弱、新旧排好序的候选由头；used_keys 里的 24 小时内不要再排。

    v0.56.0：私聊主动默认不再拿“他刚在群里说…”当由头（以前 84% 都是复述群聊）。
    """
    used = set(str(x) for x in (used_keys or []))
    hooks = []
    hooks += _thread_hooks(store, cand, now)
    hooks += _memory_hooks(store, cand, now)
    if group_hooks:
        hooks += _group_hooks(store, cand, now, allow_groups)
    t = _time_hook(cand["conv"], now, int(hour or 0), int(weekday or 0), str(day or ""))
    if t:
        hooks.append(t)
    out = []
    seen = set()
    for h in hooks:
        k = str(h.get("key") or "")
        if not k or k in used or k in seen:
            continue
        seen.add(k)
        out.append(h)
    out.sort(key=lambda x: (STRENGTH_RANK.get(x["strength"], 9), -int(x.get("src_ts") or 0)))
    return out[:MAX_HOOKS]
