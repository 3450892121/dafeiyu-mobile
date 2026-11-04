# -*- coding: utf-8 -*-
"""v0.56.0 拒绝话术：每只机器人自己的口气，轮换、同一会话 24 小时不重复。

固定句只做兜底，能用人设说话的场合仍然交给模型；这里管的是
“以前写死一句系统味台词”的那几个地方。
"""
import re
import time

LINES = {
    "office": {
        "蓝色大肥鱼": [
            "这活儿只接主人的单，你使唤不动本鲸。",
            "先让主人点头，不然本鲸不动。",
            "你这话我没法接，找主人说去。",
            "本鲸只给主人干活，别的免谈。",
        ],
        "缇宝": [
            "这个得主人点头才行呀，我先记着。",
            "这个我不好自己做主，你问问主人？",
            "不是不帮你，得等主人说一声。",
            "我先记下，等主人发话。",
        ],
        "哦鲸鲸": [
            "想让老子干活，先问过主人。",
            "不是给你干的，找主人去。",
            "这活我不接，主人的话才管用。",
            "你说了不算，问主人。",
        ],
    },
    "qzone": {
        "蓝色大肥鱼": [
            "空间只有主人能让我发，你别打这个主意。",
            "这个得主人开口，本鲸不接私活。",
        ],
        "缇宝": [
            "空间这个只有主人能让我发呀。",
            "这个得主人点头，我不好自己来。",
        ],
        "哦鲸鲸": [
            "空间只有主人能让我发。",
            "这事找主人，别找我。",
        ],
    },
    "billing": {
        "蓝色大肥鱼": [
            "这事儿只跟主人报，你别打听了。",
            "用量是主人的事，我只跟他说。",
            "想知道？问主人去，本鲸不说。",
        ],
        "缇宝": [
            "这个是主人的事，我不好说呀。",
            "用量我只跟主人讲，你问问他？",
        ],
        "哦鲸鲸": [
            "数不给你报，问主人去。",
            "关你什么事，主人问我才说。",
        ],
    },
    "reminder_full": {
        "蓝色大肥鱼": [
            "手头三条还没喊完，等它们到点再说。",
            "排满了，先等这波过去。",
        ],
        "缇宝": [
            "手上还有三条没提醒完呢，等一等好不好。",
            "先等前面几条到点呀。",
        ],
        "哦鲸鲸": [
            "三条还没喊完，排队。",
            "满了，等会儿。",
        ],
    },
}

DEFAULT_LINES = {
    "office": ["这个操作需要管理员明确授权。", "请先让管理员确认这项操作。"],
    "qzone": ["发布空间需要管理员明确授权。", "这次发布需要先由管理员确认。"],
    "billing": ["用量与费用属于管理资料，我不能向其他人透露。", "这项管理资料只向已确认的管理员提供。"],
    "reminder_full": ["当前提醒数量已满，先等已有提醒完成。", "提醒暂时排满了，请稍后再试。"],
}


def _match(name):
    s = str(name or "")
    for key in ("蓝色大肥鱼", "缇宝", "哦鲸鲸"):
        if key in s:
            return key
    return "default"


def line(bot_name, kind, conv, store):
    """轮流取一句，最近 24 小时不重复。"""
    key = _match(bot_name)
    arr = (LINES.get(kind) or {}).get(key) or DEFAULT_LINES.get(kind) or ["这个请求目前无法执行。"]
    try:
        idx = int(store.get_setting("refuse:idx:%s:%s" % (key, kind)) or 0)
    except Exception:
        idx = 0
    try:
        last = str(store.get_setting("refuse:last:%s:%s" % (key, kind)) or "")
    except Exception:
        last = ""
    pick = arr[idx % len(arr)]
    if len(arr) > 1 and last and last.split(":")[0] == str(idx % len(arr)):
        try:
            if time.time() - int(last.split(":")[1] or 0) < 86400:
                idx += 1
                pick = arr[idx % len(arr)]
        except Exception:
            pass
    try:
        store.set_setting("refuse:idx:%s:%s" % (key, kind), str(idx + 1))
        store.set_setting("refuse:last:%s:%s" % (key, kind), "%d:%d" % (idx % len(arr), int(time.time())))
    except Exception:
        pass
    return pick


def reminder_ack(bot_name, when):
    """记下提醒后的确认话，按人设说。"""
    key = _match(bot_name)
    if key == "缇宝":
        return "好呀，%s我喊你。" % when
    if key == "哦鲸鲸":
        return "行，%s。到点喊你。" % when
    if key == "蓝色大肥鱼":
        return "行，%s。到点本鲸喊你。" % when
    return "已记下，%s到点提醒你。" % when
