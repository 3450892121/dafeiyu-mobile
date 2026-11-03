# -*- coding: utf-8 -*-
"""QQ 自带表情（小黄脸 + 超级表情）表。

数据来源：NapCat 内置 sysface 表（napcat.mjs）；2026-11-03 对照现用 Shell 副本补齐，共 329 个。
发送走 OneBot 的 face 段：{"type":"face","data":{"id":"14"}}；
NapCat 会按 id 自己补 faceText / faceType / stickerId / packId，
所以本地只要「名字 → id」，不用抄动画参数。
"""
import re
import unicodedata

# (QSid, 名字, 是否超级表情/动画)
FACES = [
    (392, "龙年快乐", True),
    (393, "新年中龙", True),
    (364, "超级赞", True),
    (366, "芒狗", True),
    (362, "好兄弟", True),
    (397, "抛媚眼", True),
    (396, "狼狗", True),
    (360, "亲亲", True),
    (361, "狗狗笑哭", True),
    (363, "狗狗可怜", True),
    (365, "狗狗生气", True),
    (367, "狗狗疑问", True),
    (399, "tui", True),
    (398, "超级ok", True),
    (373, "忙", True),
    (370, "祝贺", True),
    (375, "超级鼓掌", True),
    (368, "奥特笑哭", True),
    (369, "彩虹", True),
    (371, "冒泡", True),
    (372, "气呼呼", True),
    (374, "波波流泪", True),
    (382, "emo", True),
    (383, "企鹅爱心", True),
    (401, "超级转圈", True),
    (400, "快乐", True),
    (380, "真棒", True),
    (381, "路过", True),
    (379, "企鹅流泪", True),
    (376, "跺脚", True),
    (378, "企鹅笑哭", True),
    (377, "嗨", True),
    (403, "出去玩", True),
    (402, "别说话", True),
    (390, "太头秃", True),
    (391, "太沧桑", True),
    (388, "太头疼", True),
    (389, "太赞了", True),
    (386, "呜呜呜", True),
    (385, "太气了", True),
    (384, "晚安", True),
    (387, "太好笑", True),
    (413, "摇起来", True),
    (405, "好运来", True),
    (404, "闪亮登场", True),
    (406, "姐是女王", True),
    (410, "么么哒", True),
    (411, "一起嗨", True),
    (407, "我听听", True),
    (408, "臭美", True),
    (412, "开心", True),
    (409, "送你花花", True),
    (394, "新年大龙", True),
    (14, "微笑", False),
    (1, "撇嘴", False),
    (2, "色", False),
    (3, "发呆", False),
    (4, "得意", False),
    (6, "害羞", False),
    (7, "闭嘴", False),
    (8, "睡", False),
    (9, "大哭", False),
    (5, "流泪", True),
    (10, "尴尬", False),
    (11, "发怒", False),
    (12, "调皮", False),
    (13, "呲牙", False),
    (15, "难过", False),
    (16, "酷", False),
    (96, "冷汗", False),
    (18, "抓狂", False),
    (19, "吐", False),
    (20, "偷笑", False),
    (21, "可爱", False),
    (22, "白眼", False),
    (23, "傲慢", False),
    (24, "饥饿", False),
    (25, "困", False),
    (26, "惊恐", False),
    (27, "流汗", False),
    (28, "憨笑", False),
    (29, "悠闲", False),
    (30, "奋斗", False),
    (31, "咒骂", False),
    (32, "疑问", False),
    (33, "嘘", False),
    (34, "晕", False),
    (35, "折磨", False),
    (36, "衰", False),
    (37, "骷髅", False),
    (38, "敲打", False),
    (39, "再见", False),
    (97, "擦汗", False),
    (98, "抠鼻", False),
    (99, "鼓掌", False),
    (100, "糗大了", False),
    (101, "坏笑", False),
    (102, "左哼哼", False),
    (103, "右哼哼", False),
    (104, "哈欠", False),
    (105, "鄙视", False),
    (106, "委屈", False),
    (107, "快哭了", False),
    (108, "阴险", False),
    (305, "右亲亲", False),
    (109, "左亲亲", False),
    (110, "吓", False),
    (111, "可怜", False),
    (172, "眨眼睛", False),
    (182, "笑哭", False),
    (179, "doge", False),
    (173, "泪奔", False),
    (174, "无奈", False),
    (212, "托腮", False),
    (175, "卖萌", False),
    (178, "斜眼笑", False),
    (177, "喷血", False),
    (176, "小纠结", False),
    (183, "我最美", False),
    (262, "脑阔疼", False),
    (263, "沧桑", False),
    (264, "捂脸", False),
    (265, "辣眼睛", False),
    (266, "哦哟", False),
    (267, "头秃", False),
    (268, "问号脸", False),
    (269, "暗中观察", False),
    (270, "emm", False),
    (271, "吃瓜", False),
    (272, "呵呵哒", False),
    (277, "汪汪", False),
    (307, "喵喵", False),
    (306, "牛气冲天", False),
    (281, "无眼笑", False),
    (282, "敬礼", False),
    (283, "狂笑", False),
    (284, "面无表情", False),
    (285, "摸鱼", False),
    (293, "摸锦鲤", False),
    (286, "魔鬼笑", False),
    (287, "哦", False),
    (289, "睁眼", False),
    (294, "期待", False),
    (297, "拜谢", False),
    (298, "元宝", False),
    (299, "牛啊", False),
    (300, "胖三斤", False),
    (323, "嫌弃", False),
    (332, "举牌牌", False),
    (336, "豹富", False),
    (353, "拜托", False),
    (355, "耶", False),
    (356, "666", False),
    (354, "尊嘟假嘟", False),
    (352, "咦", False),
    (357, "裂开", False),
    (334, "虎虎生威", False),
    (347, "大展宏兔", False),
    (303, "右拜年", False),
    (302, "左拜年", False),
    (295, "拿到红包", False),
    (311, "打call", True),
    (312, "变形", True),
    (314, "仔细分析", True),
    (317, "菜汪", True),
    (318, "崇拜", True),
    (319, "比心", True),
    (320, "庆祝", True),
    (324, "吃糖", True),
    (325, "惊吓", True),
    (337, "花朵脸", True),
    (338, "我想开了", True),
    (339, "舔屏", True),
    (341, "打招呼", True),
    (342, "酸Q", True),
    (343, "我方了", True),
    (344, "大怨种", True),
    (345, "红包多多", True),
    (346, "你真棒棒", True),
    (181, "戳一戳", True),
    (74, "太阳", True),
    (75, "月亮", True),
    (351, "敲敲", True),
    (349, "坚强", True),
    (350, "贴贴", True),
    (395, "略略略", True),
    (114, "篮球", True),
    (358, "骰子", True),
    (359, "包剪锤", True),
    (326, "生气", True),
    (53, "蛋糕", True),
    (49, "拥抱", False),
    (66, "爱心", False),
    (63, "玫瑰", False),
    (64, "凋谢", False),
    (187, "幽灵", False),
    (146, "爆筋", False),
    (116, "示爱", False),
    (67, "心碎", False),
    (60, "咖啡", False),
    (185, "羊驼", False),
    (137, "鞭炮", True),
    (333, "烟花", True),
    (76, "赞", False),
    (124, "OK", False),
    (118, "抱拳", False),
    (78, "握手", False),
    (119, "勾引", False),
    (79, "胜利", False),
    (120, "拳头", False),
    (121, "差劲", False),
    (77, "踩", False),
    (123, "NO", False),
    (201, "点赞", False),
    (273, "我酸了", False),
    (46, "猪头", False),
    (112, "菜刀", False),
    (56, "刀", False),
    (169, "手枪", False),
    (171, "茶", False),
    (59, "便便", False),
    (144, "喝彩", False),
    (147, "棒棒糖", False),
    (89, "西瓜", False),
    (148, "喝奶", False),
    (55, "炸弹", False),
    (41, "发抖", False),
    (125, "转圈", False),
    (42, "爱情", False),
    (43, "跳跳", False),
    (86, "怄火", False),
    (129, "挥手", False),
    (226, "拍桌", False),
    (85, "飞吻", False),
    (215, "糊脸", False),
    (214, "啵啵", False),
    (222, "抱抱", False),
    (203, "托脸", False),
    (235, "颤抖", False),
    (241, "生日快乐", False),
    (237, "偷看", False),
    (218, "舔一舔", False),
    (233, "掐一掐", False),
    (232, "佛系", False),
    (238, "扇脸", False),
    (217, "扯一扯", False),
    (225, "撩一撩", False),
    (230, "嘲讽", False),
    (194, "不开心", False),
    (210, "飙泪", False),
    (193, "大笑", False),
    (204, "吃", False),
    (200, "求求", False),
    (290, "敲开心", False),
    (224, "开枪", False),
    (229, "干杯", False),
    (221, "顶呱呱", False),
    (219, "蹭一蹭", False),
    (227, "拍手", False),
    (216, "拍头", False),
    (231, "哼", False),
    (244, "扔狗", False),
    (223, "暴击", False),
    (243, "甩头", False),
    (211, "我不看", False),
    (292, "让我康康", False),
    (240, "喷脸", False),
    (180, "惊喜", False),
    (122, "爱你", False),
    (202, "无聊", False),
    (278, "汗", False),
    (301, "好闪", False),
    (288, "请", False),
    (322, "拒绝", False),
    (198, "呃", False),
    (348, "福萝卜", False),
    (206, "害怕", False),
    (239, "原谅", False),
    (424, "续标识", True),
    (415, "划龙舟", True),
    (416, "中龙舟", True),
    (417, "大龙舟", True),
    (425, "求放过", True),
    (427, "偷感", True),
    (426, "玩火", True),
    (419, "火车", True),
    (420, "中火车", True),
    (421, "大火车", True),
    (429, "蛇年快乐", True),
    (430, "蛇身", True),
    (431, "蛇尾", True),
    (428, "收到", False),
    (422, "粽于等到你", True),
    (423, "复兴号", True),
    (432, "灵蛇献瑞", True),
    (458, "我吗", False),
    (459, "优雅", False),
    (460, "硬撑", False),
    (461, "宕机", False),
    (462, "无语", False),
    (463, "新年快乐", False),
    (464, "马上到", False),
    (465, "拆红包", False),
    (466, "羞羞哒", False),
    (467, "摇花手", False),
    (468, "失眠", False),
    (469, "坚毅", False),
    (470, "马到成功", False),
    (472, "心动", False),
    (474, "给你一拳", False),
    (475, "干饭", False),
    (476, "不是吧", False),
    (477, "你懂的", False),
    (478, "对的对的", False),
    (479, "不对不对", False),
    (480, "散味儿", False),
    (481, "学习", False),
    (482, "热化了", False),
    (483, "略", False),
    (484, "比爱心", False),
    (0, "惊讶", False),
    (450, "撇嘴", False),
    (451, "色", False),
    (452, "微笑", False),
    (453, "发呆", False),
    (454, "得意", False),
    (455, "害羞", False),
    (456, "闭嘴", False),
    (457, "睡", False),
]

# 提示词里给模型挑的常用名（全表太长，只放常用的；写全表里的名字也认）
PROMPT_NAMES = [
    "微笑",
    "撇嘴",
    "色",
    "得意",
    "害羞",
    "流泪",
    "呲牙",
    "偷笑",
    "调皮",
    "酷",
    "白眼",
    "捂脸",
    "笑哭",
    "doge",
    "裂开",
    "666",
    "赞",
    "鼓掌",
    "比心",
    "亲亲",
    "抱抱",
    "委屈",
    "快哭了",
    "大哭",
    "可怜",
    "无奈",
    "托腮",
    "吃瓜",
    "摸鱼",
    "emo",
    "嫌弃",
    "生气",
    "疑问",
    "问号脸",
    "困",
    "睡",
    "贴贴",
    "尊嘟假嘟",
    "咦",
    "耶",
    "狂笑",
    "暗中观察",
    "略略略",
    "无语",
    "哦",
    "哈欠",
    "爱心",
    "晕",
    "衰",
    "太赞了",
    "猪头",
    "比心",
]

# 模型容易写的近义说法（值必须是 FACES 里有的名字）
_ALIAS = {
    "狗头": "doge",
    "哭": "流泪",
    "哭泣": "流泪",
    "憨": "憨笑",
    "棒": "赞",
    "强": "赞",
    "牛": "牛啊",
    "汗": "流汗",
    "离谱": "无语",
    "害羞脸": "害羞",
    "6": "666",
    "比心心": "比心",
    "猪": "猪头",
    "捂一下脸": "捂脸",
    "捂嘴笑": "偷笑",
    "点赞": "赞",
    "惊讶脸": "惊讶",
}

_BY_NAME = {}
_BY_ID = {}
for _id, _name, _ani in FACES:
    _BY_ID[_id] = _name
    # 重名时优先动画版（超级表情更像真人）
    if _name not in _BY_NAME:
        _BY_NAME[_name] = (_id, _ani)
    elif _ani and not _BY_NAME[_name][1]:
        _BY_NAME[_name] = (_id, _ani)


# 模型也会用 QQ 常见写法 [偷笑]。简写必须完整命中表名或别名，
# 不能用 resolve 的包含匹配，否则 [参考答案] 等普通正文也可能被当成表情。
# 单个数字别名 [6] 保留给编号；正式表情名 [666] 仍可识别。
_SHORT_NAMES = sorted(set(_BY_NAME) | {n for n in _ALIAS if not n.isdigit()},
                      key=lambda n: (-len(n), n))
MARKER_RE = re.compile(
    r"\[(?:(?:脸|小黄脸|超级表情|QQ表情)(?:[:：]\s*([^\]\n]{1,16}))?|(" +
    "|".join(re.escape(n) for n in _SHORT_NAMES) + r"))\]", re.I)


def _name_key(name):
    return unicodedata.normalize("NFKC", str(name or "")).strip().lstrip("/").strip().casefold()


_BY_KEY = {_name_key(name): value[0] for name, value in _BY_NAME.items()}
_BY_KEY.update({_name_key(alias): _BY_NAME[name][0] for alias, name in _ALIAS.items()})
_NUMERIC_NAMES = {_name_key(n) for n in _BY_NAME if n.isdigit()}
# 配对括号逐个识别；普通方括号、链接、未知名称均不靠包含匹配猜成表情。
_TOKEN_RE = re.compile(r"\[([^\[\]\r\n]*)\]|［([^［］\r\n]*)］|【([^【】\r\n]*)】")
_LITERAL_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\r\n]*`")
_FACE_PREFIX_RE = re.compile(r"^(?:QQ表情|小黄脸|超级表情|脸)(?:(?:\s*:\s*|\s+)(.*))?$", re.I)
_STICKER_PREFIX_RE = re.compile(r"^(?:表情包|表情)(?:(?:\s*:\s*|\s+)(.*))?$", re.I)
_CQ_FACE_RE = re.compile(r"^CQ:face\s*,\s*id\s*=\s*([0-9]{1,9})(?:\s*,.*)?$", re.I)
_DANGLING_RE = re.compile(
    r"[\[［【][ \t]*(?:QQ表情|小黄脸|超级表情|脸|表情包|表情)[ \t]*(?:[:：][^\[\]［］【】\r\n]*)?$", re.I)


def _markers(text):
    """Yield (start, end, kind, value); an unknown explicit face has value None."""
    literals = [(m.start(), m.end()) for m in _LITERAL_RE.finditer(text)]
    for match in _TOKEN_RE.finditer(text):
        if any(start <= match.start() < end for start, end in literals):
            continue
        if text[match.end():].startswith('('):
            continue  # Markdown 链接/图片的标签是正文，不能当表情改掉。
        body = unicodedata.normalize("NFKC", next(v for v in match.groups() if v is not None)).strip()
        if body.casefold().startswith('cq:'):
            cq = _CQ_FACE_RE.fullmatch(body)
            fid = int(cq.group(1)) if cq else None
            yield match.start(), match.end(), "face", fid if fid in _BY_ID else None
            continue  # 只认已知 face ID，不执行模型写出的 image/file/record CQ 指令。
        explicit = _FACE_PREFIX_RE.fullmatch(body)
        if explicit:
            yield match.start(), match.end(), "face", resolve(explicit.group(1))
            continue
        sticker = _STICKER_PREFIX_RE.fullmatch(body)
        if sticker:
            yield match.start(), match.end(), "sticker", str(sticker.group(1) or "").strip()[:128]
            continue
        key = _name_key(body)
        fid = resolve(body)
        if fid is not None and (not key.isdigit() or key in _NUMERIC_NAMES):
            yield match.start(), match.end(), "face", fid
    dangling = _DANGLING_RE.search(text)
    if dangling and not any(start <= dangling.start() < end for start, end in literals):
        yield dangling.start(), dangling.end(), "face", None


def normalize(text):
    """Canonicalize known faces, drop explicit invalid/image markers, keep ordinary text."""
    raw = str(text or "")
    out, pos = [], 0
    for start, end, kind, value in _markers(raw):
        out.append(raw[pos:start])
        if kind == "face" and value is not None:
            out.append(label(value))
        pos = end
    return "".join(out) + raw[pos:]


def take_sticker_marker(text):
    """Extract one nonempty image keyword and remove every explicit image directive."""
    raw = str(text or "")
    out, pos, keyword = [], 0, ""
    for start, end, kind, value in _markers(raw):
        if kind != "sticker":
            continue
        out.append(raw[pos:start])
        pos = end
        if not keyword and value:
            keyword = value
    return ("".join(out) + raw[pos:]).strip(), keyword


def first_name(text):
    for _start, _end, kind, value in _markers(str(text or "")):
        if kind == "face" and value is not None:
            return name_of(value)
    return ""


def safe_cut(text, cut):
    """Retreat a proposed text boundary to the beginning of a complete marker."""
    for start, end, _kind, _value in _markers(str(text or "")):
        if start < cut < end:
            return start
    return cut


def names():
    return sorted(_BY_NAME.keys())


def resolve(name):
    """确切表名/已知别名 → face id（含 0）；未知名字不猜，不凭子串选错表情。"""
    return _BY_KEY.get(_name_key(name))


# 模型爱打的颜文字 → 表里对得上的表情名（替代它本来想表达的那个表情，不是硬塞）
_EMOTICONS = [
    ("T_T", "流泪"), ("ToT", "流泪"), ("T﹏T", "流泪"),
    ("qwq", "委屈"), ("QAQ", "委屈"),
    ("-_-", "无语"), ("-_-||", "无语"),
    ("^_^", "微笑"), ("^<_<", "调皮"), ("^_<", "调皮"),
    ("(￣▽￣)", "得意"),
]


# 先看这句话本身在表达什么（比光看颜文字准），再退回按颜文字配
_CONTEXT_RULES = [
    (re.compile(r"哈哈|笑死|好笑|笑翻|笑喷"), "笑哭"),
    (re.compile(r"气死|气炸|过分|服了|离谱|无所谓|无语"), "无语"),
    (re.compile(r"委屈|难受|欺负|可怜"), "委屈"),
    (re.compile(r"厉害|得意|夸你|牛"), "得意"),
    (re.compile(r"吓|慌|怕"), "惊恐"),
]

# 同一个表情连着用会显得机械，撞车时换个意思相近的
_ALT = {
    "无语": ["白眼", "无奈"],
    "笑哭": ["大笑"],
    "委屈": ["快哭了"],
    "得意": ["骄傲"],
    "惊恐": ["惊吓"],
}


def emoticon_to_face(text, avoid=""):
    """把句子里的一个颜文字换成对得上的 [脸:…]；没有颜文字就原样返回。

    优先按句子表达的情绪挑（有“哈哈”就配笑哭），挑不到才按颜文字配（-_− 配无语）。
    avoid 传入上一张用过的表情名，避免同一张连发。
    """
    raw = str(text or "")
    if not any(sym in raw for sym, _n in _EMOTICONS):
        return raw
    name = ""
    for rx, cand in _CONTEXT_RULES:
        if rx.search(raw):
            name = cand
            break
    if not name:
        for sym, cand in _EMOTICONS:
            if sym in raw:
                name = cand
                break
    if avoid and name == avoid:
        for alt in _ALT.get(name, []):
            if resolve(alt) is not None:
                name = alt
                break
    for sym, _cand in _EMOTICONS:
        if sym in raw:
            return raw.replace(sym, "[脸:%s]" % name, 1)
    return raw


def name_of(fid):
    if fid is None or isinstance(fid, bool):
        return ""
    try:
        return _BY_ID.get(int(str(fid).strip()), "")
    except (TypeError, ValueError):
        return ""


def label(fid):
    """写进历史用的标记，让模型记得自己发过哪个脸。"""
    n = name_of(fid)
    return "[脸:%s]" % n if n else ""


def split(text):
    """正文 → [("text", 文本), ("face", id)]，按出现顺序切。

    支持 [脸:名字] 与确切已知的 [名字]；未知显式标记丢弃，普通方括号正文保留。

    拆成纯函数放这里，自测不用把整个大脑拉起来。
    """
    parts = []
    raw = str(text or "")
    pos = 0
    for start, end, kind, value in _markers(raw):
        seg = raw[pos:start]
        if seg.strip():
            parts.append(("text", seg))
        if kind == "face" and value is not None:
            parts.append(("face", value))
        pos = end
    tail = raw[pos:]
    if tail.strip():
        parts.append(("text", tail))
    return parts


def strip(text):
    """去掉表情及图片指令（进文档、TTS、记账时用），保留普通方括号。"""
    raw = str(text or "")
    out, pos = [], 0
    for start, end, _kind, _value in _markers(raw):
        out.append(raw[pos:start])
        pos = end
    return ("".join(out) + raw[pos:]).strip()
