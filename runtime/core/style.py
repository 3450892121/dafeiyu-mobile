# -*- coding: utf-8 -*-
"""大肥鱼出站文风兜底：把模型写出来的“舞台动作 / 直接报情绪 / 口头声称发图”清掉。

纯函数、不联网、不读库，方便单测，也方便出站直接调：

    from style import clean
    text = clean(text)

或者分步调用：
    text = tidy(strip_sticker_claims(strip_stage_directions(text)))

口径：
- 只删“动作 / 舞台说明”，保留正常聊天括号（（不是吧）（笑）（真的）这类）；
- 中文全角括号（）和英文半角括号 () 都处理，支持句中、句末、连续多个和嵌套；
- 表情包由系统按情绪自动配，模型口头说“给你发了张表情包”属于违规，删掉；
- 所有函数只接收 / 返回字符串，不抛异常；空输入返回空字符串。
"""

import re

_OPEN_TO_CLOSE = {"（": "）", "(": ")"}
_CLOSE_TO_OPEN = {"）": "（", ")": "("}

# 正常聊天插入语：原样保留
_KEEP_PAREN = frozenset("""
不是吧 不是 真的 真的假的 对吧 是吧 好吧 行吧 好家伙 笑死 服了 离谱 救命 绝了 牛 我的天 天哪
啊 哦 嗯 诶 哎 唉 哈 咦 嘿 喂 呃 草 靠 啊这 这 对了 话说 总之 反正 顺便 说真的 讲真 讲道理
话说回来 有一说一 确实 确实如此 没问题 可以 好 行 嗯嗯 对对对 是是是 好好好 知道了 收到 随便
再说 算了 别 不要 别别别 不是不是 不不不 等等 等一下 慢着 停 打住 哈哈 哈哈哈 呵呵 嘿嘿 噗
嗷 嗷呜 哇 哇哦 呀 咯 嘞 啦 哟 唷 嗷嗷 笑 大笑 微笑 偷笑 坏笑 冷笑 干笑 苦笑
""".split())

# 纯情绪标签 / 表演提示：整段括号只有这些词时删掉
_PERFORMANCE_PAREN = frozenset("""
委屈 委屈巴巴 生气 愤怒 恼火 火大 不爽 开心 高兴 快乐 兴奋 激动 得意 骄傲 难过 伤心 悲伤
失落 沮丧 郁闷 烦 烦躁 烦死 害怕 紧张 焦虑 慌 心虚 内疚 后悔 害羞 尴尬 无语 无奈 震惊 惊讶
吃惊 疑惑 困惑 感动 心疼 嫉妒 吃醋 累 困 饿 满足 幸福 小声 低声 嘟囔 嘀咕 沉默 停顿 叹气
摊手 耸肩 翻白眼 扶额 叉腰 炸毛 挑眉 撇嘴 皱眉 咬牙 抿嘴 眨眼 脸红 脸烫 气鼓鼓 眼泪汪汪
眼圈红 垂头 丧气 得意洋洋 美滋滋 心累 心塞 破防 绷不住 抓狂 崩溃 难受 难绷 麻了 笑不出来
转身 转头 低头 抬头 点头 摇头 缩头 凑近 后退 伸手 收手 举手 挥手 摆手 扭身 侧身 歪头 垂眼
抬眼 瞪眼 抱臂 拍桌 拍手 挠头 跺脚 别过脸 转过身 低下头 抬起头 点点头 摇摇头 叹了口气
""".split())

# 身体部位（要跟动作词同时出现才判成动作，避免把“手机”“脸书”这类词误删）
_BODY_RE = re.compile(
    r"(?:尾巴|尾鳍|鲸尾|尾尖|耳朵|耳尖|耳根|耳垂|脸颊|脸上|脸蛋|脸|眼睛|眼角|眼眶|眼|嘴角|嘴唇|嘴|"
    r"牙齿|呆毛|头发|头皮|头|手|手指|手心|手背|肩膀|肩|手臂|胳膊|腰|背|腿|脚|脚尖|胸口|心口|心里|"
    r"心|心跳|嗓子|喉咙|声音|语气|语速|呼吸|气息|鼻子|鼻尖|泪|汗|眉头|眉|身子|身|身体)"
)
_ACTION_WORD_RE = re.compile(
    r"(?:甩|摇|晃|拍|翘|僵|炸|烫|红|白|黑|青|沉|紧|松|软|硬|亮|暗|湿|干|麻|酸|疼|痛|抖|颤|缩|垂|"
    r"竖|立|绷|垮|抽|抿|咬|张|闭|睁|眯|眨|转|低|抬|扬|笑|哭|变|小|大|轻|重|快|慢|停|顿|滞|凉|热|"
    r"冰|火|挂|痒|冒|加|速|放|缓|压|提|挑|皱|鼓|点|歪|侧|探|靠|挪|伸|收|举|挥|摆|扭|摊|耸)"
)

# 动作短语：动作词 + 常见动作尾巴（“拍了拍”“别过脸”“叹了口气”）
_ACTION_PHRASE_RE = re.compile(
    r"(?:甩|摇|晃|拍|翘|叉|扶|捂|别过|扭过|转过|低|抬|探|凑近|后退|蹭|抱|搂|圈|揉|戳|揪|捏|挠|踢|"
    r"跺|捶|拽|扯|挑|撇|咬|舔|咽|吸|叹|咳|眯|睁|瞪|瞟|瞥|翻|眨|涨|抖|哆嗦|僵|愣|怔|呆|结巴|嘟囔|"
    r"嘀咕|呲|龇|抽泣|哽咽|咕哝|歪|侧|垂|扬|压|提|抿|皱|鼓|缩|蜷|靠|挪|迈|跨|蹲|瘫|趴|躺|坐|站|跳|"
    r"跑|走|冲|扑|躲|闪|退|伸|收|举|挥|摆|扭|顿|摊|耸)"
    r"(?:了|着|地|得|的|一下|一会|一会儿|起来|下去|过去|过来|住|开|上|下|头|脸|手|眼|嘴|脚|腰|肩|"
    r"背|身|声音|语气|方向|半步|一步|几口|口气|一声|两声|额|眉|唇|腮|吼|气)"
)

# “小声嘟囔 / 默默看着 / 轻轻拍了拍”这类状语 + 动作
_SOFT_ACTION_RE = re.compile(
    r"(?:小声|低声|轻声|悄悄|默默|幽幽|狠狠|轻轻|慢慢|微微|偷偷|暗暗|忍不住|下意识|不由自主)"
    r"(?:地|的)?"
    r"(?:说|道|嘟囔|嘀咕|笑|哭|叹|问|答|念|哼|嗯|看|瞟|瞥|点|摇|抬|低|缩|退|蹭|抱|靠|挪|走|站|坐|"
    r"趴|躺|拍|捂|扶|叉|眨|瞪|翻|撇|抿|咬|呼|吸|喘|咳|抖|颤|僵|愣|怔|呆)"
)

# 英文舞台说明（（smiles）（tail wagging）这类）
_EN_ACTION_RE = re.compile(
    r"\b(?:tail|tails|ear|ears|eartip|face|faces|eye|eyes|hand|hands|head|shoulder|shoulders|voice|"
    r"breath|sigh|sighs|sighed|smile|smiles|smiled|grin|grins|grinned|laugh|laughs|laughed|chuckle|"
    r"chuckles|chuckled|shrug|shrugs|shrugged|nod|nods|nodded|shake|shakes|shook|tilt|tilts|tilted|"
    r"lean|leans|leaned|hug|hugs|hugged|pat|pats|patted|wave|waves|waved|wag|wags|wagged|flick|flicks|"
    r"flicked|blink|blinks|blinked|stare|stares|stared|glare|glares|glared|huff|huffs|huffed|pout|"
    r"pouts|pouted|mutter|mutters|muttered|mumble|mumbles|mumbled|whisper|whispers|whispered|pause|"
    r"pauses|paused|stomp|stomps|stomped|giggle|giggles|giggled|snort|snorts|snorted|cough|coughs|"
    r"coughed|gasp|gasps|gasped|frown|frowns|frowned)\b",
    re.I,
)

_IMAGE_NOUN = r"(?:表情包|表情|动图|梗图|贴图|图片|照片|图)"
_SEND_VERB = r"(?:发|甩|丢|扔|递|塞|贴|来)"

# 口头发图声称：只认“发你了 / 发了图 / 给你发 / 甩你一张”这类完成或指向性说法，
# 不碰“发表情包很好玩”“我会发图”这种普通陈述。
_STICKER_CLAIM_RE = re.compile(
    r"(?:"
    # 我给你发了张表情包 / 我甩了你一张图
    r"(?:我|本鲸大人|人家|咱)[^，。！？\n]{0,8}?" + _SEND_VERB +
    r"(?:了|过|给你|你|出去|过去)"
    r"(?:一|两|几|这|那)?(?:张|个|条|幅)?(?:的)?" + _IMAGE_NOUN +
    r")|(?:"
    # 给你发了张表情包 / 马上给你来一张图
    r"(?:给|向|对)你" + _SEND_VERB + r"(?:了|过)?"
    r"(?:一|两|几|这|那)?(?:张|个|条|幅)?(?:的)?" + _IMAGE_NOUN +
    r")|(?:"
    # 发了张表情包 / 甩了张图
    + _SEND_VERB + r"(?:了|过)"
    r"(?:一|两|几|这|那)?(?:张|个|条|幅)?(?:的)?" + _IMAGE_NOUN +
    r")|(?:"
    # 发你一张图 / 甩你一张表情包
    + _SEND_VERB + r"(?:给你|你)"
    r"(?:一|两|几|这|那)?(?:张|个|条|幅)?(?:的)?" + _IMAGE_NOUN +
    r")|(?:"
    # 表情包发你了 / 图已经发过去了
    + _IMAGE_NOUN + r"(?:已经)?(?:发|甩|丢|扔|递|塞|贴)(?:给)?你?(?:了|出去|过去)"
    r")|(?:"
    # 发张图给你看看
    + _SEND_VERB + r"(?:一|两|几)?(?:张|个|条|幅)?(?:的)?" + _IMAGE_NOUN + r"(?:给|向)你"
    r")|(?:"
    # 发图了 / 甩表情包了（后面必须是断句或标点，别把“发图了不起”当成声称）
    + _SEND_VERB + _IMAGE_NOUN + r"了(?=$|[，。！？、；：\s~～…）)])"
    r")",
    re.I,
)

_CLAIM_VERB_RE = re.compile(_SEND_VERB)
_CLAIM_NEGATION_RE = re.compile(
    r"(?:不|没|未|别|甭|无需|无须|禁止)"
    r"(?:再|要|用|必|需|许|会|想|能|打算|准备|应该|有|曾|给|向|对|帮|替|把|我|你|他|她|它|您|咱|本鲸|"
    r"了|还|已经|继续|就是|是|说|说过|以为|觉得|认定|当成|表示|意味着|让){0,10}$")
_CLAIM_CONDITION_RE = re.compile(
    r"(?:如果|要是|假如|假设|若|等到|等我|等你|要不要|能不能|可不可以|能否|是否)[^，。！？!?；;\n]{0,20}$")


def _sticker_claim_matches(text):
    """A denial, conditional or question is not a claim that an image was sent."""
    for match in _STICKER_CLAIM_RE.finditer(text):
        verb = _CLAIM_VERB_RE.search(match.group(0))
        verb_at = match.start() + (verb.start() if verb else 0)
        # Scope the qualifier to this clause; “别急，图发你了” is still a claim.
        before = re.split(r"[，。！？!?；;\n]", text[:verb_at])[-1].strip()
        if _CLAIM_NEGATION_RE.search(before) or _CLAIM_CONDITION_RE.search(before):
            continue
        yield match

# 方括号表情包标记：[表情:…] / [表情包: …] 都是模型不该写的内部标记；
# 真实发图由代码做，出站一律删掉（[脸:…] 不受影响）。
_BRACKET_STICKER_MARK_RE = re.compile(r"\[(?:表情包|表情)(?:\s*[:：]\s*[^\]\n]*)?\]")

# 括号里的“图片记录”：（图：xxx）（表情包：xxx）（同时发了一张表情包：xxx）
_IMAGE_RECORD_RE = re.compile(
    r"^(?:(?:你|我)?(?:同时|还|又|另外)?(?:发|甩|丢|扔|贴|附)(?:了)?(?:一|两|几|这|那)?(?:张|个|条|幅)?"
    r"(?:的)?(?:表情包|表情|图片|图|动图|照片)|(?:附?图|图片?|表情包|配图|附图))\s*[:：]"
)

# 直接报情绪：我很生气 / 我好难过 / 我有点慌 / 我开心死了
_EMOTION_WORDS = (
    "生气", "愤怒", "恼火", "火大", "不爽", "开心", "高兴", "快乐", "兴奋", "激动", "得意", "骄傲",
    "难过", "伤心", "悲伤", "失落", "沮丧", "郁闷", "烦躁", "烦恼", "害怕", "紧张", "焦虑", "心慌",
    "心虚", "内疚", "后悔", "害羞", "尴尬", "无语", "无奈", "震惊", "惊讶", "吃惊", "疑惑", "困惑",
    "感动", "心疼", "嫉妒", "吃醋", "委屈", "疲惫", "心累", "崩溃", "破防", "难受", "烦", "累", "困",
    "饿", "慌", "急", "懵", "爽", "酸", "气死", "烦死", "难过死", "开心死",
)
_EMO_ALT = "|".join(sorted(_EMOTION_WORDS, key=len, reverse=True))
_DIRECT_EMOTION_RE = re.compile(
    r"我(?:现在|真的|真|其实)?(?:很|好|太|特别|非常|超级|超|挺|蛮|有点|有些|有亿点|可|都|快|简直)+"
    r"(?:" + _EMO_ALT + r")"
    r"|我(?:现在|真的|真|快|简直|已经)?(?:" + _EMO_ALT + r")(?:了|死了|坏了|得慌|到不行|极了)"
    r"|我(?:不|没)(?:太)?(?:开心|高兴|舒服|好受|爽|满意)"
)


def _paren_spans(text):
    """产出配对括号的 (start, end, inner)，end 不含。支持（）和 ()，忽略不配对的。"""
    stack = []
    for i, ch in enumerate(text):
        if ch in _OPEN_TO_CLOSE:
            stack.append((ch, i))
        elif ch in _CLOSE_TO_OPEN:
            if stack and stack[-1][0] == _CLOSE_TO_OPEN[ch]:
                _open, start = stack.pop()
                yield start, i + 1, text[start + 1:i]
            # 不配对的右括号直接忽略，不吞正文


def _paren_core(inner):
    return str(inner or "").strip().strip("… .。!！?？~～、，,；;：:\t\r\n")


def _is_keep_paren(inner):
    core = _paren_core(inner)
    return bool(core) and core in _KEEP_PAREN


def _is_performance_paren(inner):
    core = _paren_core(inner)
    if not core:
        return False
    if core in _PERFORMANCE_PAREN:
        return True
    # “有点委屈”“非常生气”这类带程度词的情绪标签
    m = re.match(r"^(?:有点|有些|有亿点|很|好|超|超级|特别|非常|太|真|挺|蛮|贼|巨|最)(?:的)?(.+)$", core)
    if m and m.group(1) in _PERFORMANCE_PAREN:
        return True
    # 身体部位 + 动作/状态：尾巴甩、耳尖发烫、声音变小
    if _BODY_RE.search(inner) and _ACTION_WORD_RE.search(inner):
        return True
    # 明确动作短语：拍了拍、别过脸、叹了口气
    if _ACTION_PHRASE_RE.search(inner):
        return True
    # 状语 + 动作：小声嘟囔、默默看着
    if _SOFT_ACTION_RE.search(inner):
        return True
    # 英文舞台说明
    if _EN_ACTION_RE.search(inner):
        return True
    return False


def _is_stage_paren(inner, self_growth_enabled=False):
    if not str(inner or "").strip():
        return False
    if _is_keep_paren(inner):
        return False
    if self_growth_enabled:
        # 情绪表达不是虚构身体动作；成长模式保留“（有点难过）”等自然插入语。
        core = re.sub(r"^(?:有点|有些|有亿点|很|好|超|超级|特别|非常|太|真|挺|蛮|贼|巨|最)(?:的)?", "", _paren_core(inner))
        if core in _EMOTION_WORDS or core in {"委屈巴巴", "丧气", "得意洋洋", "美滋滋", "心塞", "难绷", "麻了", "笑不出来"}:
            return False
    return _is_performance_paren(inner)


def _drop_spans(text, spans):
    if not spans:
        return text
    spans = sorted(spans, key=lambda x: (x[0], -(x[1] - x[0])))
    out = []
    pos = 0
    last_end = -1
    for start, end in spans:
        if start < last_end:
            continue
        out.append(text[pos:start])
        pos = end
        last_end = end
    out.append(text[pos:])
    return "".join(out)


def strip_stage_directions(text, self_growth_enabled=False):
    """删掉括号里的身体动作 / 舞台说明，保留正常聊天括号。"""
    s = str(text or "")
    if not s:
        return s
    spans = [(start, end) for start, end, inner in _paren_spans(s) if _is_stage_paren(inner, self_growth_enabled)]
    return _drop_spans(s, spans)


def strip_sticker_claims(text):
    """删掉“给你发了张表情包 / 发了图”这类口头声称；真实发图由代码做。"""
    s = str(text or "")
    if not s:
        return s
    # 先把括号里的发图声称 / 图片记录整段删掉（否则会留下空括号）
    spans = []
    for start, end, inner in _paren_spans(s):
        core = _paren_core(inner)
        if any(_sticker_claim_matches(inner)) or _IMAGE_RECORD_RE.match(core):
            spans.append((start, end))
    s = _drop_spans(s, spans)
    # 括号外正文里的声称直接删掉；方括号表情包标记也一并删
    s = _drop_spans(s, [(m.start(), m.end()) for m in _sticker_claim_matches(s)])
    return _BRACKET_STICKER_MARK_RE.sub("", s)


# 系统历史格式泄漏：[19:34] / [昵称 19:34] / （19:34） 出现在回复开头
_CTX_TIME_TAG_RE = re.compile(r"(?m)^\s*[\[【(（]\s*\d{1,2}:\d{2}\s*[\]】)）]\s*")
_CTX_NAME_TIME_TAG_RE = re.compile(r"(?m)^\s*\[[^\]\n]{1,20}\s+\d{1,2}:\d{2}\]\s*")

# 把话聊死的句子（只报告 + 出站兜底删整句；聊天里不该常出现）
_DEAD_END_RE = re.compile(
    r"(?:反正就那点事|也没啥好说的了|没什么好说的了|没啥好说的了|你开心就好|爱咋咋地|"
    r"随便你|行吧[，,]?\s*随便)"
)

# 玻璃心 / 把“哈哈”当嘲笑：命中就重写一次，不硬删（删了句子会不连贯）
_SULK_RE = re.compile(
    r"(?:就笑这么两下|你这是觉得.*讲笑话|本鲸大人讲笑话呢|笑这么两下就完了|行吧[，,]?\s*随便)"
)

# 2026-10-23 用户决定：**取消“出站不许出现大肥鱼/小肥鱼”的限制**——
# 同名机器人早就改名了，不需要再靠替换名字防触发。现在可以正常自称「大肥鱼」。
# 保留这个函数只为兼容旧调用（原样返回）。
def strip_self_name(text):
    """历史接口：不再替换账号名，原样返回。"""
    return str(text or "")

_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[）)]")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t\u3000]+([，。！？；：、,.!?;:])")
_LEADING_PUNCT_RE = re.compile(r"^[，,。.、；;：:\s]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t\u3000]+$", re.M)


def strip_context_prefix(text):
    """删掉回复开头的 [19:34] / [昵称 19:34] / （19:34）——那是系统历史格式，不是回复内容。"""
    s = str(text or "")
    if not s:
        return s
    s = _CTX_NAME_TIME_TAG_RE.sub("", s)
    return _CTX_TIME_TAG_RE.sub("", s)


def strip_dead_end(text):
    """删掉“反正就那点事 / 没啥好说的了 / 随便你”这类把话聊死的整句。

    只按句号/问号/感叹号/换行切；删完如果空了，保留原句（不硬造新话）。
    """
    s = str(text or "")
    if not s:
        return s
    parts = re.split(r"(?<=[。！？!?\n])", s)
    kept = [p for p in parts if p and not _DEAD_END_RE.search(p)]
    out = "".join(kept).strip()
    return out or s


def tidy(text):
    """去多余空格 / 空行、去句首多余标点；保留换行和 [脸:…] 标记。"""
    s = str(text or "")
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _TRAILING_WS_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = _EMPTY_PAREN_RE.sub("", s)
    s = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)
    lines = []
    for line in s.split("\n"):
        line = line.strip()
        line = _LEADING_PUNCT_RE.sub("", line)
        lines.append(line)
    s = "\n".join(lines)
    s = _MULTI_BLANK_RE.sub("\n\n", s)
    s = re.sub(r"[，,]\s*([。！？])", r"\1", s)
    s = re.sub(r"([。！？])\s*[，,]", r"\1", s)
    s = re.sub(r"([，,、；;：:])\1+", r"\1", s)
    return s.strip()


def style_violations(text, self_growth_enabled=False):
    """返回命中的文风问题列表：[{type, match, start}]，只报告不修改。

    type:
      - stage_direction：括号动作 / 舞台说明
      - sticker_claim：口头声称发了图 / 表情包
      - direct_emotion：直接说“我很X / 我X死了”
    """
    s = str(text or "")
    found = []
    if not s:
        return found
    for start, end, inner in _paren_spans(s):
        if _is_stage_paren(inner, self_growth_enabled):
            found.append({"type": "stage_direction", "match": s[start:end], "start": start})
        elif any(_sticker_claim_matches(inner)) or _IMAGE_RECORD_RE.match(_paren_core(inner)):
            found.append({"type": "sticker_claim", "match": s[start:end], "start": start})
    claimed_spans = [(v["start"], v["start"] + len(v["match"])) for v in found if v["type"] == "sticker_claim"]
    for m in _sticker_claim_matches(s):
        if any(a <= m.start() < b for a, b in claimed_spans):
            continue
        found.append({"type": "sticker_claim", "match": m.group(0), "start": m.start()})
    for m in _BRACKET_STICKER_MARK_RE.finditer(s):
        if any(a <= m.start() < b for a, b in claimed_spans):
            continue
        found.append({"type": "sticker_claim", "match": m.group(0), "start": m.start()})
    if not self_growth_enabled:
        for m in _DIRECT_EMOTION_RE.finditer(s):
            found.append({"type": "direct_emotion", "match": m.group(0), "start": m.start()})
    for m in _CTX_NAME_TIME_TAG_RE.finditer(s):
        found.append({"type": "context_prefix", "match": m.group(0), "start": m.start()})
    for m in _CTX_TIME_TAG_RE.finditer(s):
        found.append({"type": "context_prefix", "match": m.group(0), "start": m.start()})
    if not self_growth_enabled:
        for m in _DEAD_END_RE.finditer(s):
            found.append({"type": "dead_end", "match": m.group(0), "start": m.start()})
        for m in _SULK_RE.finditer(s):
            found.append({"type": "sulk", "match": m.group(0), "start": m.start()})
    found.sort(key=lambda x: x["start"])
    return found


def needs_rewrite(text, self_growth_enabled=False):
    """返回需要重写的原因（把话聊死 / 玻璃心 / 阴阳怪气）；不需要返回空串。"""
    if self_growth_enabled:
        return ""
    s = str(text or "")
    if not s:
        return ""
    if _SULK_RE.search(s):
        return "玻璃心/把对方的笑当成嘲笑"
    if _DEAD_END_RE.search(s):
        return "把话聊死"
    return ""


def clean(text, keep_self_name=False, self_growth_enabled=False):
    """出站一条龙：去历史前缀 → 去括号动作 → 去发图声称 → 去聊死句 → 收尾整理。

    名字不再替换（2026-10-23 起）；keep_self_name 只为兼容旧调用保留。
    """
    s = strip_context_prefix(text)
    s = strip_sticker_claims(strip_stage_directions(s, self_growth_enabled))
    if not self_growth_enabled:
        s = strip_dead_end(s)
    return tidy(s)
