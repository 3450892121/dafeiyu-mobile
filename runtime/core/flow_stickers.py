# -*- coding: utf-8 -*-
"""表情包选择/描述/分段与情绪兜底关键词。

从 main.py 拆出（task-1），task-3 补：
- parse_sticker_request：识别「发个表情包 / 有没有X表情 / 我要X的表情包」，否定句不触发；
- send_sticker：选图 + 发送 + 失败兜底（返回 None 前必须 log + store.add_log）；
- maybe_mood_sticker：模型没写 [表情:] 时按情绪配图，冷却 90s / 概率 0.8，连续 5 轮没发图强制一次；
- take_sticker_marker：从正文剥掉 [表情:…]；
- mood_sticker_keyword / _mood_sticker_gate 保持原签名（旧 notes 测试直接 import main 用）。
"""

import atexit, base64, hashlib, hmac, ipaddress, io, json, os, queue, random, re, shutil, socket, subprocess, tempfile, threading, time, urllib.parse, urllib.request, uuid, zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conversation_understanding import image_transform_request
from config import Config
from onebot import OneBot, text_seg, image_seg, face_seg
from model import ModelPool
from persona import Persona, emotion_nudge as persona_emotion_nudge
from store import Store, _clean_name
from stickers import Stickers
from sticker_index import StickerIndex, embed_texts
import sticker_diversity
import qq_faces
from websearch import search_with_fallback, format_for_prompt as websearch_format
from version import VERSION, BUILD
from music import search_first as music_search, music_seg
from safety import allow_outbound
from util import log, log_exc, acquire_single_instance
from common import DATA_DIR, VISION_SYS, _mime
import imgmatch


# —— 情绪兜底发本地表情包 ——
# 模型自己写 [表情:关键词] 时走原路；下面这套只管「它没写、但这一轮明显有情绪」，
# 由代码按情绪挑一张，关键词必须是人设里给过的那几个词（都要能进语义索引检索）。
_FACE_STICKER_KW = {
    "笑哭": "开心", "大笑": "开心", "呲牙": "开心", "偷笑": "开心", "调皮": "开心", "微笑": "开心",
    "得意": "得意", "酷": "得意",
    "白眼": "无语", "无语": "无语", "捂脸": "无语", "无奈": "无语", "嫌弃": "无语", "emo": "委屈",
    "委屈": "委屈", "快哭了": "想哭", "大哭": "想哭", "流泪": "想哭", "可怜": "想哭",
    "生气": "生气", "害羞": "害羞",
    "抱抱": "撒娇", "贴贴": "撒娇", "亲亲": "撒娇", "比心": "比心",
    "裂开": "震惊", "惊恐": "震惊", "惊讶": "震惊",
    "疑问": "疑惑", "问号脸": "疑惑", "咦": "疑惑",
    "困": "困", "睡": "困", "哈欠": "困",
    "吃瓜": "摸鱼", "摸鱼": "摸鱼", "托腮": "摸鱼",
    "鼓掌": "加油", "赞": "加油", "666": "加油",
}

_EMO_STICKER_RULES = [
    (re.compile(r"笑死|哈哈|好笑|笑翻|笑喷|乐死|笑不活"), "开心"),
    (re.compile(r"无语|服了|离谱|什么鬼|真是够了"), "无语"),
    (re.compile(r"想哭|哭了|泪目|破防|鼻子一酸"), "想哭"),
    (re.compile(r"委屈|被骂|凶我|欺负|可怜|难受"), "委屈"),
    (re.compile(r"气死|气炸|过分|凭什么|烦死|火大"), "生气"),
    (re.compile(r"夸我|得意|牛啊|牛逼|厉害|邀功|骄傲|嘿嘿"), "得意"),
    (re.compile(r"震惊|吓死|吓一跳|裂开|真的假的"), "震惊"),
    (re.compile(r"抱抱|蹭蹭|撒娇|贴贴|揉揉"), "撒娇"),
    (re.compile(r"害羞|脸红了|不好意思"), "害羞"),
    (re.compile(r"晚安|睡了|好困|困死|好累|累死|疲惫"), "困"),
    (re.compile(r"为啥|咋会|懵"), "疑惑"),
    (re.compile(r"加油|打气|冲鸭|你可以的"), "加油"),
]

# 斗图情绪词表：让模型只从这些词里挑，保证回击情绪能对上本地图库的标签/语义索引。
_BATTLE_MOODS = ("开心", "得意", "无语", "委屈", "生气", "震惊", "撒娇", "害羞", "困", "摸鱼", "疑惑", "加油", "想哭", "比心")

# 情绪配图频率（task-3 定的默认值）：想让人感觉得到，又不会每轮都甩图。
# 仍然允许管理页用 sticker_mood_chance / sticker_mood_cooldown_sec 覆盖。
_MOOD_CHANCE_DEFAULT = 1.0   # v0.20.1 用户要求：有情绪就发，不再按概率卡
_MOOD_COOLDOWN_DEFAULT = 0   # 不再设冷却；只保留「一条一张 / 不连发同一张」
_MOOD_FORCE_EVERY_DEFAULT = 5

# 明确要图：句子里出现这些名词才算「表情包请求」，避免把普通聊天当命令。
_REQ_NOUN = r"(?:表情包|表情|图(?:片)?)"
_KW_CH = r"[^，。！？!?；;、\s]"

# 「有没有X表情 / 我要X的表情包 / 发个X表情包」；kw 允许空（随便来一张）。
_REQ_PATTERNS = [
    re.compile(r"(?:有没有|有木有)\s*(?:一?[个张])?\s*(?P<kw>" + _KW_CH + r"{0,20}?)\s*的?\s*" + _REQ_NOUN),
    re.compile(r"(?:我要|我想要|想要)\s*(?:一?[个张])?\s*(?P<kw>" + _KW_CH + r"{0,20}?)\s*的?\s*" + _REQ_NOUN),
    re.compile(r"(?:(?:给|帮)\s*我?\s*)?(?:发|来|整|甩)\s*(?:我)?\s*(?:一?[个张])?\s*(?P<kw>" + _KW_CH + r"{0,20}?)\s*的?\s*" + _REQ_NOUN),
    # “给个表情包 / 给我个表情包”——没有「发/来」动词的口语说法
    re.compile(r"给\s*(?:我)?\s*(?:一?[个张])\s*(?P<kw>" + _KW_CH + r"{0,20}?)\s*的?\s*" + _REQ_NOUN),
]

# 否定句：别发 / 不用发图 / 不要表情包 / 不发了。注意「要不要」不是否定。
# 先剥掉“别忘了发个表情包”里的“别忘”，这种其实是提醒发。
_NEG_FORGOT_RE = re.compile(r"(?:别|不要|甭)\s*忘(?:了|记)?")
_NEG_RES = [
    re.compile(r"(?<![要])不要\s*(?:再)?\s*(?:发|来|整|甩|给|弄|要|想)[^，。！？!?\n]{0,10}?(?:表情包|表情|图(?:片)?)"),
    re.compile(r"(?:不用|不必|没必要|用不着|无需)\s*(?:再)?\s*(?:发|来|整|甩|给|弄)[^，。！？!?\n]{0,10}?(?:表情包|表情|图(?:片)?)"),
    re.compile(r"(?:别|甭|勿|不许|不准|先别|别再)\s*(?:再)?\s*(?:发|来|整|甩|给|弄)[^，。！？!?\n]{0,10}?(?:表情包|表情|图(?:片)?)"),
    re.compile(r"(?:不要|不用|别|甭|勿|不必)[^，。！？!?\n]{0,4}?(?:表情包|表情)"),
    re.compile(r"(?<![要])不(?:再)?\s*(?:发|来|整)\s*(?:了|啦|表情包|表情|图(?:片)?)"),
    re.compile(r"(?<![要])(?:别|甭|不用|不要|不必)\s*(?:再)?\s*(?:发|来)\s*(?:了|啦)"),
    re.compile(r"(?<!有)没有(?:什么)?\s*(?:表情包|表情|图(?:片)?)"),
]

_KW_PREFIX_RE = re.compile(r"^(?:请|麻烦)?(?:给我|帮我|帮忙|发个|发张|来个|整一个|甩个|发一个|来一个|一张|一个|个|张|点|些|的|这个|那个|这种|那种|这些|那些|随便|任意)+")
_KW_SUFFIX_RE = re.compile(r"(?:的|吧|呗|啊|呀|哦|嘛|呢|啦|了|哈|一下|就行|就好)+$")
_KW_FILLERS = {
    "", "个", "张", "一张", "一个", "点", "些", "的", "这个", "那个", "这种", "那种",
    "这些", "那些", "一样", "随便", "任意", "你", "我", "他", "她", "它",
    "表情", "表情包", "图", "图片", "照片", "发", "来", "整", "甩", "给我", "帮我", "一下",
}


def _clean_sticker_kw(raw):
    """把“发个 开心的 表情包”里抠出来的关键词洗干净；没关键词就回空串。"""
    # v0.57.2：把残留的方括号也剥掉（以前会抠出「陪我上班[」这种）
    s = str(raw or "").strip().strip(" 的“”\"'‘’《》「」[]（）()")
    s = _KW_PREFIX_RE.sub("", s).strip()
    s = _KW_SUFFIX_RE.sub("", s).strip().strip(" 的“”\"'‘’《》「」")
    if s in _KW_FILLERS:
        return ""
    return s


# v0.40.0：区分「让我发一张」和「在聊表情包这件事」。
# 现场四条真实误判（2026-10-23 纳西妲群）：
#   「怎么确保表情包发的准确和发送表情包的频率这些」→ 被抠出关键词"准确和发送"；
#   「选图是有模型自己决定要不要发，发什么图吗」→ "什么"；
#   「模型知道谁发了什么表情包」→ "了什么"；
#   「发出去的是什么表情包」→ "出"。全都直接回了「喏。」+图。
_ASK_STRONG_RE = re.compile(r"(要不要|是不是|能不能|可不可以|会不会|怎么|为什么|为啥|如何|什么|哪[张个些]|谁|多少)")
_STICKER_META_RE = re.compile(r"(模型|方案|逻辑|功能|概率|频率|阈值|标签|向量|召回|上下文|接口|代码|实现|测试|设置|配置|参数)")
_IMPERATIVE_RE = re.compile(r"^[\s@\S]{0,12}?(?:能|可以)?\s*(?:给我|帮我|帮忙|麻烦|请)?\s*(?:发|来|整|甩)")


def _sticker_request_is_command(text):
    """这句到底是不是在让机器人发表情包。

    - 句首就是 发/来/整/甩（可带 能/可以/给我/帮我）且不超过 12 字 → 是（含「能发表情包吗」）；
    - 含「什么/哪/谁/怎么/为什么/要不要/是不是」→ 不是（在聊这件事或在问）；
    - 聊到模型/方案/参数/召回这些功能词、且不是短祈使句 → 不是；
    - 其余 12 字以内的短句算命令，长句不算。
    """
    s = str(text or "").strip()
    if not s:
        return False
    imperative = bool(_IMPERATIVE_RE.match(s))
    short = len(s) <= 12
    if _ASK_STRONG_RE.search(s):
        return False
    # v0.57.2：带「功能/模型/设置/参数」这类词的句子是在聊这件事，不是要图——
    # 以前「这功能能发表情包吗」因为「短祈使」被放行，会真去发表情包。
    if _STICKER_META_RE.search(s):
        return False
    if re.search(r"[？?]", s) or re.search(r"吗\s*[\U0001F440~～]?$", s):
        return bool(imperative and short)
    return short or imperative


def parse_sticker_request(text):
    """识别“发个表情包 / 来张图 / 有没有X表情 / 我要X的表情包”。

    返回 None 表示不是表情包请求（含否定句）；返回 {"kw": "..."} 表示明确要图。
    kw 为空串表示“随便来一张”，由调用方走随机/语境选图。
    """
    raw = str(text or "").strip()
    if image_transform_request(raw):
        return None
    if not raw:
        return None
    # v0.57.2：QQ 自带的黄脸表情在正文里是字面「[表情]」，会和「发表情包」的「表情」撞车——
    # 现场「@四个机器人 都来陪我上班😁」被当成「要陪我上班的表情包」，只回了句“图库里没找到”。
    # 先把这类标记从判断串里去掉（只影响判断，不改原文）。
    raw = re.sub(r"\[(?:表情包|表情|图片|语音)\]", "", raw)
    raw = re.sub(r"\[文件[^\]]*\]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    # “别忘发个表情包”= 提醒发，不是否定
    probe = _NEG_FORGOT_RE.sub("", raw)
    for rx in _NEG_RES:
        if rx.search(probe):
            return None
    for rx in _REQ_PATTERNS:
        m = rx.search(raw)
        if not m:
            continue
        if not _sticker_request_is_command(raw):
            return None
        return {"kw": _clean_sticker_kw(m.group("kw"))}
    return None


def take_sticker_marker(reply_text):
    """从模型回复里剥掉 [表情:关键词]，返回 (clean_text, kw)。没有标记时 kw 为空串。"""
    return qq_faces.take_sticker_marker(reply_text)


def mood_sticker_keyword(text, reply):
    """这一轮明显有情绪时，给一个能配本地表情包的关键词；猜不到返回空串。

    优先级：回复里模型自己用的 [脸:名字]（最能代表它当时的心情）→ 对方那句话的情绪词 → 回复里的情绪词。
    只回一个词；发不发还要过「冷却 + 概率」，避免变成「为了发而发」。
    """
    r = str(reply or "")
    name = qq_faces.first_name(r)
    if name:
        kw = _FACE_STICKER_KW.get(name)
        if kw:
            return kw
    for blob in (str(text or ""), r):
        for rx, kw in _EMO_STICKER_RULES:
            if rx.search(blob):
                return kw
    return ""


def _mood_sticker_gate(cfg, last_at, now=None, rnd=None):
    """情绪兜底的放行判断。v0.20.1 起：默认概率 1.0、不设冷却（都不限制频率）。

    cfg 里显式写 0 仍可关闭概率口子；冷却配置大于 0 才按冷却挡。
    """
    now = time.time() if now is None else now
    rnd = random.random if rnd is None else rnd
    raw = cfg.get("sticker_mood_chance")
    chance = _MOOD_CHANCE_DEFAULT if raw is None else float(raw)
    if chance <= 0:
        return False
    raw_cd = cfg.get("sticker_mood_cooldown_sec")
    cd = _MOOD_COOLDOWN_DEFAULT if raw_cd is None else float(raw_cd)
    if cd and now - float(last_at or 0) < cd:
        return False
    return rnd() < chance


def _sticker_item_by_file(brain, file):
    """按文件名在 stickers.items 里找元数据；不依赖实例方法（旧测试的 FakeBrain 也能用）。"""
    stickers = getattr(brain, "stickers", None)
    for it in getattr(stickers, "items", []) or []:
        if it.get("file") == file:
            return it
    return None


def _sticker_find_exact(brain, kw, exclude=None):
    """只在备注/标签/场景里做字面命中（确定“图库里真的有这个词”）。"""
    k = str(kw or "").strip()
    stickers = getattr(brain, "stickers", None)
    if not k or not stickers:
        return None
    exclude = exclude or set()
    low = k.casefold()
    matches = []
    for it in stickers.items:
        if it.get("file") in exclude:
            continue
        blob = " ".join([str(it.get("note") or ""), str(it.get("scene") or ""),
                         " ".join(it.get("tags") or [])]).casefold()
        if low in blob and it.get("file") and os.path.isfile(stickers.path(it)):
            matches.append((1.0, it))
    if not matches:
        return None
    from sticker_engine import recent_usage
    return sticker_diversity.rank(matches, recent_usage(brain), limit=1)[0][1]


def _sticker_make_result(brain, item, requested="", match="random", matched=True, score=None):
    """统一返回给 Lead 的结构：file/note/desc + 命中信息（matched 供“没找到那张”兜底话术）。"""
    desc = ""
    desc_fn = getattr(brain, "_sticker_desc", None)
    if callable(desc_fn):
        try:
            desc = desc_fn(item)
        except Exception as e:
            log("sticker", "生成描述失败 %r" % (e,))
    out = dict(item)
    out["note"] = str(item.get("note") or "")
    out["desc"] = str(desc or out["note"] or "")
    out["requested"] = str(requested or "")
    out["match"] = str(match or "random")  # exact / semantic / fallback / random
    out["matched"] = bool(matched)        # 只有“图库字面命中”或本来就没指定关键词才为 True
    if score is not None:
        try:
            out["score"] = round(float(score), 4)
        except Exception:
            pass
    return out


def _select_sticker_impl(brain, conv, keyword="", context_text="", use_index=True, avoid_files=None):
    """纯选图（不发送、不看冷却）：字面命中 → 语义索引 → 关键词/情绪兜底 → 随机。

    永远避开上一张（同会话）；明确关键词找不到时退一张情绪相近/随机的。
    写成模块函数是为了 notes 里用「Bot._pick_sticker(fake, ...)」的旧测试也能跑。
    """
    stickers = getattr(brain, "stickers", None)
    if not stickers or not len(stickers):
        return None
    kw = str(keyword or "").strip()
    last_map = getattr(brain, "last_sticker", None)
    exclude = set()
    if isinstance(last_map, dict):
        last = last_map.get(conv)
        if last:
            exclude.add(last)
    # 连续斗图时只避开上一张不够：本地图库常有两张最相近，会来回换。
    # 同会话避开最近 10 张（以前只避 3 张，几轮之后又绕回同一张）。
    recent_map = getattr(brain, "recent_stickers", None)
    if isinstance(recent_map, dict):
        for f in (recent_map.get(conv) or [])[-10:]:
            if f:
                exclude.add(f)
    try:
        exclude.update(brain.store.recent_sticker_files(conv, 10))
    except (AttributeError, TypeError):
        pass
    from common import DATA_DIR
    aliases = sticker_diversity.cached_aliases(DATA_DIR)
    for f in list(exclude):
        exclude.update(aliases.get(f) or [])
    # 斗图专用：对方刚发的那张如果在图库里（转发/另存过也能认出来），必须排除。
    # 以前没这一步，机器人会把对方刚发的图原样发回去，看着像复读。
    for f in (avoid_files or []):
        if f:
            exclude.add(f)

    # 1) 备注/标签/场景字面命中：最可信，也最省一次向量请求
    if kw:
        it = _sticker_find_exact(brain, kw, exclude=exclude)
        if it:
            return _sticker_make_result(brain, it, requested=kw, match="exact", matched=True)

    # 2) 语义索引：字面没有时按意思找（关键词优先，情绪路径用上下文优先）
    idx = getattr(brain, "sticker_index", None)
    if use_index and idx:
        try:
            idx.maybe_reload()
        except Exception:
            pass
        if getattr(idx, "ok", False):
            if kw:
                q = (kw + " " + str(context_text or "")).strip()[:600]
            else:
                q = str(context_text or "").strip()[:600]
            if q:
                try:
                    hits = idx.search(q, 12)
                except Exception as e:
                    hits = []
                    log("sticker", "语义找图失败 %r" % (e,))
                candidates = []
                for score, f in hits:
                    if f in exclude:
                        continue
                    it = _sticker_item_by_file(brain, f)
                    if it and os.path.isfile(stickers.path(it)):
                        candidates.append((score, it))
                if candidates:
                    from sticker_engine import recent_usage
                    ranked = sticker_diversity.rank(candidates, recent_usage(brain), limit=1)
                    if ranked:
                        score, it = ranked[0]
                        return _sticker_make_result(brain, it, requested=kw, match="semantic",
                                                    matched=not bool(kw), score=score)

    # 3) 兜底：明确关键词找不到时，先用情绪相近的词，再不行随机
    fb_kw = ""
    if kw and context_text:
        fb_kw = mood_sticker_keyword(context_text, "") or ""
    try:
        it = stickers.pick(fb_kw or kw, exclude)
    except Exception as e:
        log("sticker", "选图失败 %r" % (e,))
        it = None
    if not it:
        # 实在全被排除（图库太小）才允许重复一张，总比不发强
        try:
            it = stickers.pick("", exclude)
        except Exception:
            it = None
    if not it:
        try:
            it = stickers.pick("", set())
        except Exception:
            it = None
    if not it or not it.get("file"):
        return None
    try:
        if not os.path.isfile(stickers.path(it)):
            return None
    except Exception:
        return None
    return _sticker_make_result(brain, it, requested=kw,
                                match=("fallback" if kw else "random"),
                                matched=not bool(kw))


# v0.54.15：表情包纠错。`这张不对/不要这种表情包` → 挪回收站；`这张可以` → 放出来。
# 宽松版给「已经在对本鲸说话」的消息用（私聊 / 群里 @ 了它）；
# 严格版要求正文里真的提到「表情」，给群里不 @ 时的自动识别用，避免把「这个不对」误当成表情包。
_STICKER_BAD_RE = re.compile(
    r"(?:这张|这个|刚才那张|刚才那个|发的那张|发的这个|上张|这张图|这个图)"
    r"\s*(?:表情包|表情|图|张)?\s*(?:我|有点|太|真的|真|很|好|实在)?\s*(?:不对|不行|不好|难看|别发|换一张|不要|不想要|丑|讨厌|恶心|不喜欢)"
    r"|(?:不要|别发|别再发|不许发|不想看|别整)\s*(?:这种|这张|这个|这类|那样的?)\s*(?:表情包|表情|图)"
    r"|(?:这种|这张|这个|这类)\s*(?:表情包|表情)\s*(?:不要|别发|不好|难看|丑|恶心|不行|换一张)"
    r"|^(?:换一张|换一个|换张图|不要这张|别发这个)[。！!～~\s]*$"
)
_STICKER_GOOD_RE = re.compile(
    r"(?:这张|刚才那张|上张|这个表情|这张表情包)\s*(?:表情包|表情|图|张)?\s*"
    r"(?:可以|不错|留着|挺好|还行|要得)"
)


class StickerMixin(object):
    def _sticker_meta(self):
        return os.path.join(DATA_DIR, "stickers", "stickers.jsonl")

    def _sticker_bad_hit(self, text, strict=False):
        s = str(text or "")
        if strict and "表情" not in s:
            return False
        return bool(_STICKER_BAD_RE.search(s))

    def _sticker_good_hit(self, text):
        return bool(_STICKER_GOOD_RE.search(str(text or "")))

    def _self_sticker_recent(self, conv, secs=300):
        """本鲸刚在这个会话发过表情包、之后还没人插话（群里主人不 @ 纠错时用）。"""
        try:
            st = self.store.last_sticker(conv) if hasattr(self.store, "last_sticker") else None
            if not st:
                return False
            ts = int(st.get("ts") or 0)
            if not ts or time.time() - ts > int(secs):
                return False
            last = self.store.last_message(conv) or {}
            return (str(last.get("direction") or "") == "out"
                    and int(last.get("ts") or 0) >= ts - 2)
        except Exception:
            return False

    def _sticker_trash(self, file):
        """把这张表情包挪进回收站，并从 stickers.jsonl 删掉这一行（v0.54.15）。"""
        name = os.path.basename(str(file or "").replace("\\", "/"))
        if not name:
            return False
        sd = os.path.dirname(self._sticker_meta())
        img = os.path.join(sd, "images", name)
        rec = os.path.join(sd, "recycle", name)
        ok = False
        try:
            if os.path.isfile(img):
                os.makedirs(os.path.dirname(rec), exist_ok=True)
                os.replace(img, rec)
                ok = True
        except Exception as e:
            log("sticker", "挪回收站失败 %r" % (e,))
        try:
            meta = self._sticker_meta()
            if os.path.isfile(meta):
                rows, hit = [], False
                with open(meta, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if not s:
                            continue
                        try:
                            row = json.loads(s)
                        except Exception:
                            continue
                        fname = os.path.basename(str((row or {}).get("file") or "").replace("\\", "/"))
                        if fname == name:
                            hit = True
                            continue
                        rows.append(row)
                if hit:
                    tmp = meta + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        for row in rows:
                            f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    os.replace(tmp, meta)
                    ok = True
        except Exception as e:
            log("sticker", "图库清单移除失败 %r" % (e,))
        try:
            self.stickers.reload()
        except Exception:
            pass
        return ok

    @staticmethod
    def _is_sticker_seg(s):
        """QQ 桌面端收藏表情常以 image+[动画表情] 下发，不一定走 mface。"""
        if not isinstance(s, dict):
            return False
        t = s.get("type")
        if t == "mface":
            return True
        if t != "image":
            return False
        d = s.get("data") or {}
        if "动画表情" in str(d.get("summary") or ""):
            return True
        low = (str(d.get("file") or "") + " " + str(d.get("url") or "")).split("?")[0].lower().strip()
        return low.endswith(".gif") or low.endswith(".webp")

    def _text_segs(self, text):
        """把模型写的 [脸:名字] 切成 QQ 自带表情段，其余照旧是文本段。

        只认 qq_faces 表里的名字（含近义说法），认不出的标记直接丢掉，
        绝不让瞎编的 id 发出去。纯表情（正文只有标记）也能单独发。
        """
        out = []
        faces = []
        for kind, val in qq_faces.split(text):
            if kind == "text":
                out.append(text_seg(val))
            else:
                out.append(face_seg(val))
                faces.append(qq_faces.name_of(val) or str(val))
        if faces:
            log("face", "自带表情 %s" % "/".join(faces))
        return out

    def _split_sticker(self, text):
        """保留旧方法名（flow_reply 还在用），行为与 take_sticker_marker 一致。"""
        return take_sticker_marker(text)

    @staticmethod
    def _sticker_label(item):
        """把表情包的备注写进历史：机器人下次就知道自己当时发的是什么图。"""
        note = str((item or {}).get("desc") or (item or {}).get("note") or "").strip()
        if note:
            return "[表情包: " + note[:60].replace("\n", " ") + "]"
        tags = "、".join((item or {}).get("tags") or [])
        return "[表情包: " + tags[:30] + "]" if tags else "[表情包]"

    def _find_sticker_exact(self, kw, exclude=None):
        """只在备注/标签/场景里做字面命中（确定“图库里真的有这个词”）。"""
        k = str(kw or "").strip()
        if not k:
            return None
        exclude = exclude or set()
        low = k.casefold()
        for it in self.stickers.items:
            if it.get("file") in exclude:
                continue
            blob = " ".join([str(it.get("note") or ""), str(it.get("scene") or ""),
                             " ".join(it.get("tags") or [])]).casefold()
            if low in blob and it.get("file") and os.path.isfile(self.stickers.path(it)):
                return it
        return None

    def _find_sticker(self, kw):
        """按关键词找表情包：先查备注/标签/场景，再走语义索引。"""
        item = self._find_sticker_exact(kw)
        if item:
            return item
        if self.sticker_index and self.sticker_index.ok:
            try:
                for _, f in self.sticker_index.search(str(kw or "").strip(), 5):
                    it = self._sticker_by_file(f)
                    if it and os.path.isfile(self.stickers.path(it)):
                        return it
            except Exception as e:
                log("sticker", "按词找图失败 %r" % (e,))
        return None

    def _sticker_by_file(self, file):
        for it in self.stickers.items:
            if it.get("file") == file:
                return it
        return None

    def _sticker_desc(self, item):
        """发送前看一眼：视觉模型描述这张图，缓存；失败退回旧备注。"""
        f = str((item or {}).get("file") or "")
        if not f:
            return ""
        cache = getattr(self, "_desc_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._desc_cache = cache
        if f in cache:
            return cache[f]
        desc = ""
        try:
            with open(self.stickers.path(item), "rb") as fh:
                data = fh.read(8 * 1024 * 1024)
            if data:
                r = self.pool.vision(VISION_SYS, "这张表情包是什么画面？有文字就念出来，是什么情绪、适合什么场合？60 字内客观描述。", data, _mime(data))
                if r.ok and r.text.strip():
                    desc = r.text.strip()[:120]
        except Exception as e:
            log("sticker", "看图失败 %r" % (e,))
        if not desc:
            desc = str((item or {}).get("note") or "")[:120]
        if desc:
            cache[f] = desc
            try:
                path = getattr(self, "_desc_path", "")
                if path:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(cache, fh, ensure_ascii=False)
                    os.replace(tmp, path)
            except Exception:
                pass
        return desc

    def _sticker_result(self, item, requested="", match="random", matched=True, score=None):
        """统一返回给 Lead 的结构；具体实现见模块级 _sticker_make_result。"""
        return _sticker_make_result(self, item, requested=requested, match=match,
                                    matched=matched, score=score)

    def _select_sticker(self, conv, keyword="", context_text="", use_index=True, avoid_files=None):
        """纯选图（不发送、不看冷却）；具体实现见模块级 _select_sticker_impl。"""
        return _select_sticker_impl(self, conv, keyword=keyword, context_text=context_text,
                                    use_index=use_index, avoid_files=avoid_files)

    def _pick_sticker(self, conv, keyword, force=False, context_text="", mood=False):
        """旧入口（保留签名）：先过冷却/概率闸门，再交给 _select_sticker 选图。"""
        stickers = getattr(self, "stickers", None)
        if not stickers or not len(stickers):
            return None
        last_at_map = getattr(self, "last_sticker_at", None)
        last_at = last_at_map.get(conv, 0) if isinstance(last_at_map, dict) else 0
        cfg = getattr(self, "cfg", None) or {}
        if mood:
            # 情绪兜底：冷却 + 概率；默认 90 秒 / 0.8。
            if not _mood_sticker_gate(cfg, last_at):
                return None
        else:
            chance = cfg.get("sticker_chance")
            chance = 0.0 if chance is None else float(chance)
            want = force or bool(keyword) or random.random() < chance
            if not want:
                return None
            cd = float(cfg.get("sticker_cooldown_sec") or 0)
            if (not force) and cd and time.time() - float(last_at or 0) < cd:
                return None
        return _select_sticker_impl(self, conv, keyword=keyword, context_text=context_text,
                                    use_index=bool(keyword) or bool(context_text))

    def _battle_look(self, images):
        """斗图先看对方这张：下载一次，同时给出画面描述和「图库里同款文件」。

        avoid 里的文件是对方这张图在图库里的对应图片（转发/另存/缩放也能认出来），
        选图时必须排除，否则会把对方刚发的图原样发回去，看着像复读。
        """
        out = {"desc": "", "avoid": [], "mood": "", "reply_mood": ""}
        for img in (images or [])[:1]:
            d = img if isinstance(img, dict) else {}
            data = None
            try:
                data = self._fetch_image(d)
            except Exception as e:
                log("battle", "下载对方表情包失败 %r" % (e,))
            if data:
                try:
                    idx = imgmatch.load_index(os.path.dirname(self._sticker_meta()))
                    out["avoid"] = imgmatch.similar_files(
                        imgmatch.dhash_bytes(data), idx, max_distance=6, limit=8)
                except Exception as e:
                    log("battle", "表情包查重失败 %r" % (e,))
            desc = ""
            fn = getattr(self, "_describe", None)
            if callable(fn):
                try:
                    desc = str(fn(d, data=data) or "").strip()
                except Exception as e:
                    log("battle", "斗图看表情包失败 %r" % (e,))
            if not desc:
                sm = str(d.get("summary") or "").strip()
                if sm and sm not in ("动画表情", "表情"):
                    desc = sm
            out["desc"] = desc[:300]
            if desc:
                # 用户要求“允许用模型判断”：看懂对方情绪后回一张对位的图，
                # 不是只按画面描述检索（那样图库有同款就会回同款）。
                try:
                    out["mood"], out["reply_mood"] = self._battle_mood(desc)
                except Exception as e:
                    log("battle", "情绪判断失败 %r" % (e,))
            if out["desc"] or out["avoid"]:
                break
        return out

    def _battle_mood(self, desc):
        """斗图判断：对方这张图什么情绪、回一张什么情绪的图最接得住。

        多一次小模型调用（thinking=False，约 1 秒）；失败返回空串，调用方退回按画面描述检索。
        """
        text = str(desc or "").strip()
        if not text or not getattr(self, "pool", None):
            return "", ""
        choices = "、".join(_BATTLE_MOODS)
        prompt = ("对方在跟你斗图，刚发来一张表情包，画面：%s\n"
                  "判断两件事，都必须从下面这些词里挑一个：%s\n"
                  "1) 对方这张图是什么情绪；\n"
                  "2) 你回一张什么情绪的图最能接住（可以同情绪接梗，也可以反差吐槽）。\n"
                  "只输出一行，格式：对方=…；回击=…" % (text[:300], choices))
        r = self.pool.chat("你是斗图情绪判断助手。", [], prompt, max_tokens=40,
                           temperature=0.2, thinking=False)
        out = (r.text or "").strip() if r.ok else ""
        if not out:
            return "", ""
        mine = re.search(r"(?:对方|他)[=＝:：]\s*([^\s；;，,。]+)", out)
        back = re.search(r"(?:回击|你回|回复)[=＝:：]\s*([^\s；;，,。]+)", out)

        def pick(m):
            if not m:
                return ""
            w = m.group(1)
            for word in _BATTLE_MOODS:
                if word in w:
                    return word
            return ""
        return pick(mine), pick(back)

    def _battle_vision_note(self, images):
        """斗图时先看对方发来的表情包/图片，返回一句画面描述；失败返回空串。

        以前斗图分支直接拿「[图片]」当检索词，等于没看图：每次同一个输入，
        本地图库里两张最相近的来回换。这里最多看 1 张，失败再用 mface 的 summary 兜底。
        """
        for img in (images or [])[:1]:
            d = img if isinstance(img, dict) else {}
            desc = ""
            fn = getattr(self, "_describe", None)
            if callable(fn):
                try:
                    desc = str(fn(d) or "").strip()
                except Exception as e:
                    log("battle", "斗图看表情包失败 %r" % (e,))
            if desc:
                return desc[:300]
            sm = str(d.get("summary") or "").strip()
            if sm and sm not in ("动画表情", "表情"):
                return sm[:300]
        return ""

    # ---------- task-3 新接口：明确要图 / 情绪配图 ----------
    def send_sticker(self, conv, is_group, gid, uid, kw="", context_text="", force=True, avoid_files=None):
        """选图并发送。成功返回 item（file/desc/note/命中信息），失败返回 None。

        force=True（默认）表示明确请求不受冷却限制。发送结果只认 self._send 的返回值；
        返回 False / 抛异常都算失败：log + store.add_log，且绝不更新 last_sticker*。
        """
        cfg = getattr(self, "cfg", None) or {}
        if not bool(cfg.get("sticker_judge_enabled", True)):
            return None
        if not force:
            last_at_map = getattr(self, "last_sticker_at", None)
            last_at = last_at_map.get(conv, 0) if isinstance(last_at_map, dict) else 0
            cd = float(cfg.get("sticker_cooldown_sec") or 0)
            if cd and time.time() - float(last_at or 0) < cd:
                log("sticker", "send_sticker 冷却中 conv=%s kw=%s" % (conv, str(kw or "")[:20]))
                return None
        item = self._select_sticker(conv, kw, context_text,
                                    use_index=bool(str(kw or "").strip()) or bool(str(context_text or "").strip()),
                                    avoid_files=avoid_files)
        if not item:
            log("sticker", "没选到图 conv=%s kw=%s" % (conv, str(kw or "")[:20]))
            return None
        fname = os.path.basename(str(item.get("file") or "")) or "?"
        ok = False
        err = ""
        try:
            path = self.stickers.path(item)
            if not os.path.isfile(path):
                raise FileNotFoundError("sticker file missing")
            ok = bool(self._send(is_group, gid, uid, [image_seg(path)]))
        except Exception as e:
            err = repr(e)
            log("sticker", "发送异常 conv=%s file=%s %r" % (conv, fname, e))
        if not ok:
            detail = "conv=%s kw=%s file=%s err=%s" % (conv, str(kw or "")[:20], fname, err or "send returned false")
            log("sticker", "发送失败 " + detail)
            try:
                self.store.add_log("sticker_fail", detail, False)
            except Exception as e:
                log("sticker", "发送失败日志写库也失败 %r" % (e,))
            return None
        last_map = getattr(self, "last_sticker", None)
        if not isinstance(last_map, dict):
            last_map = {}
            self.last_sticker = last_map
        last_map[conv] = item.get("file")
        recent_map = getattr(self, "recent_stickers", None)
        if not isinstance(recent_map, dict):
            recent_map = {}
            self.recent_stickers = recent_map
        recent = list(recent_map.get(conv) or [])
        recent.append(item.get("file"))
        recent_map[conv] = recent[-10:]
        at_map = getattr(self, "last_sticker_at", None)
        if not isinstance(at_map, dict):
            at_map = {}
            self.last_sticker_at = at_map
        at_map[conv] = time.time()
        streak = getattr(self, "sticker_mood_streak", None)
        if isinstance(streak, dict):
            streak[conv] = 0
        log("sticker", "已发 conv=%s file=%s kw=%s match=%s" % (conv, fname, str(kw or "")[:20], item.get("match")))
        return item

    def send_sticker_item(self, conv, is_group, gid, uid, item):
        """v0.40.0：发送「判定链路已经挑好」的那张图，返回是否成功。

        和 send_sticker 的区别：不重新选图（图是判断/粗筛/看图一步步选出来的），
        只负责发出去 + 记账（最近发过 / 近期列表 / 时间 / 计数清零）。
        """
        if not bool((getattr(self, "cfg", None) or {}).get("sticker_judge_enabled", True)):
            return False
        if not item or not item.get("file"):
            return False
        fname = os.path.basename(str(item.get("file") or "")) or "?"
        ok = False
        err = ""
        try:
            path = self.stickers.path(item)
            if not os.path.isfile(path):
                raise FileNotFoundError("sticker file missing")
            ok = bool(self._send(is_group, gid, uid, [image_seg(path)]))
        except Exception as e:
            err = repr(e)
            log("sticker", "发送异常 conv=%s file=%s %r" % (conv, fname, e))
        if not ok:
            detail = "conv=%s file=%s err=%s" % (conv, fname, err or "send returned false")
            log("sticker", "发送失败 " + detail)
            try:
                self.store.add_log("sticker_fail", detail, False)
            except Exception:
                pass
            return False
        last_map = getattr(self, "last_sticker", None)
        if not isinstance(last_map, dict):
            last_map = {}
            self.last_sticker = last_map
        last_map[conv] = item.get("file")
        recent_map = getattr(self, "recent_stickers", None)
        if not isinstance(recent_map, dict):
            recent_map = {}
            self.recent_stickers = recent_map
        recent = list(recent_map.get(conv) or [])
        recent.append(item.get("file"))
        recent_map[conv] = recent[-10:]
        at_map = getattr(self, "last_sticker_at", None)
        if not isinstance(at_map, dict):
            at_map = {}
            self.last_sticker_at = at_map
        at_map[conv] = time.time()
        log("sticker", "已发(判定) conv=%s file=%s match=%s mood=%s" % (
            conv, fname, item.get("match"), item.get("mood_kw") or "-"))
        return True

    def _mood_streak_map(self):
        m = getattr(self, "sticker_mood_streak", None)
        if not isinstance(m, dict):
            m = {}
            self.sticker_mood_streak = m
        return m

    def maybe_mood_sticker(self, conv, text, reply, context_text=""):
        """模型没写 [表情:] 时的情绪配图：只做闸门 + 选图，不发送。返回 item 或 None。

        - v0.20.1 起：默认概率 1.0、无冷却（管理页可覆盖）；
        - 同会话连续 5 轮文字回复没发过图（由 send_sticker 成功后清零计数）时强制一张；
        - 返回的 item 带 mood_kw / kw / forced，调用方把它交给 send_sticker(force=True) 发。
        """
        rep = str(reply or "")
        if not rep.strip():
            return None
        stickers = getattr(self, "stickers", None)
        if not stickers or not len(stickers):
            return None
        cfg = getattr(self, "cfg", None) or {}
        raw_chance = cfg.get("sticker_mood_chance")
        chance = _MOOD_CHANCE_DEFAULT if raw_chance is None else float(raw_chance)
        if chance <= 0:
            return None
        streak_map = self._mood_streak_map()
        streak = int(streak_map.get(conv) or 0) + 1
        streak_map[conv] = streak
        raw_force = cfg.get("sticker_mood_force_every")
        force_every = _MOOD_FORCE_EVERY_DEFAULT if raw_force is None else int(raw_force)
        # 第 5 轮：不管有没有情绪都补一张，免得一天只发两三张
        if force_every > 0 and streak >= force_every:
            item = self._select_sticker(conv, "", context_text,
                                        use_index=bool(str(context_text or "").strip()))
            if not item:
                return None
            streak_map[conv] = 0  # 这一次强制算“用掉了”，失败也不连发
            item["mood_kw"] = ""
            item["kw"] = ""
            item["forced"] = True
            log("sticker", "连续 %d 轮没发图，强制配一张 conv=%s file=%s"
                % (streak, conv, item.get("file")))
            return item
        kw = mood_sticker_keyword(text, rep)
        if not kw:
            return None
        last_at_map = getattr(self, "last_sticker_at", None)
        last_at = last_at_map.get(conv, 0) if isinstance(last_at_map, dict) else 0
        if not _mood_sticker_gate(cfg, last_at):
            return None
        item = self._select_sticker(conv, kw, context_text,
                                    use_index=bool(kw) or bool(context_text))
        if not item:
            return None
        item["mood_kw"] = kw
        item["kw"] = kw
        item["forced"] = False
        log("sticker", "情绪配图 kw=%s conv=%s file=%s" % (kw, conv, item.get("file")))
        return item
