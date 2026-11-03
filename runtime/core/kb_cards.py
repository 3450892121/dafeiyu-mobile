# -*- coding: utf-8 -*-
"""知识库卡片 v0.61.0：把看过的视频/文件整理成"能查能用"的卡片。

为什么单独一个模块：
  以前的视频笔记 = 人设口气的感想 + 一段转写节选，检索出来全是"我印象最深的是…"和乱码，
  分类也没有（千本樱MV、游戏 bug、DSH 插件都堆在一起）。
  现在入库前先过一遍：
    1. 只留"以后用得上"的具体知识（方法/步骤/参数/工具名/结论/坑），不要感想、不要复述剧情；
    2. 归到一个固定分类（分类名固定，面板才好按类看）；
    3. 值不值得存打分，纯娱乐/游戏实况/纯歌词 MV 直接跳过，不往库里塞垃圾；
    4. 一句话 + 2~5 条要点 + 标签，检索和模型注入都用这份卡片头，不再拿转写原文去顶。
"""
import json
import re

# 固定分类（不要随手加；面板按这个顺序展示）
TOPICS = ("AI与工具", "电脑与网络", "学习与效率", "生活与美食",
          "音乐与影视", "游戏", "健康", "人际与情绪", "我的资料", "其他")

MIN_WORTH = 3          # 低于这个分不存进知识库
MAX_POINTS = 5

_VIDEO_PROMPT = """你在给机器人整理"知识库卡片"。素材是它刚看完的一个视频（语音转写可能不准，画面描述有限）。
目标：把视频里真正能学、以后能查的东西写成一张卡片。

硬规则：
1. 分类只能是其中一个：{topics}
2. 标题不超过 22 字，说清讲的是什么；不要写"视频/分享/合集/盘点/教程"这类空词。
3. 一句话不超过 60 字，说清这条视频的核心内容或结论。
4. 要点 2~5 条，每条不超过 50 字，必须是以后用得上的具体知识：方法、步骤、参数、工具名、结论、要注意的坑。
   - 不要写"我印象最深""很好看""挺有意思"这种感受；
   - 不要复述视频剧情、不要写 UP 主的故事、不要写广告；
   - 转写乱、听不清的宁可少写，绝对不要编。
5. 标签 2~6 个短词（例如：AI 绘图、插件、参数、面试）。
6. 值得存 0~5 分：纯娱乐、段子、游戏实况、纯歌词/纯 MV、吃播探店、没讲清楚内容的，打 0~2 分。

只输出一行 JSON：
{{"topic":"…","title":"…","summary":"…","points":["…","…"],"tags":["…"],"worth":0}}

素材：
标题：{title}
时长：约 {minutes} 分钟
转写（可能不准）：
{transcript}

画面描述（按时间，只作参考，别照念）：
{notes}
"""

_MATERIAL_PROMPT = """你在给机器人整理"知识库卡片"，素材是主人发给它的资料（可能是笔记、作业、说明书、表格导出）。
目标：整理成以后能快速查到的卡片。

硬规则：
1. 分类只能是其中一个：{topics}（主人自己的资料优先选"我的资料"）。
2. 标题不超过 22 字，说清这份资料是什么。
3. 一句话不超过 60 字，说清这份资料讲什么、什么时候能用上。
4. 要点 2~5 条，每条不超过 50 字：关键定义、结论、数据、步骤、注意点。
5. 标签 2~6 个短词。
6. 只写资料里有的，不要编；资料读不全就少写几条。
7. 值得存固定给 4（主人主动发来的资料默认有用）。

只输出一行 JSON：
{{"topic":"…","title":"…","summary":"…","points":["…"],"tags":["…"],"worth":4}}

素材（文件名：{title}）：
{text}
"""


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s[:n]


def _json_obj(text):
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _norm_card(obj, min_worth, topics):
    """把模型返回的 JSON 收干净：分类兜底、长度截断、要点去重、够不够格入库。"""
    topic = _clip(obj.get("topic"), 12)
    if topic not in topics:
        topic = "其他"
    title = _clip(obj.get("title"), 30)
    summary = _clip(obj.get("summary"), 90)
    points = []
    for x in (obj.get("points") or [])[:MAX_POINTS]:
        t = _clip(x, 70)
        if len(t) < 6:
            continue
        if t in points:
            continue
        points.append(t)
    tags = []
    for x in (obj.get("tags") or [])[:8]:
        t = _clip(x, 16)
        if t and t not in tags:
            tags.append(t)
    try:
        worth = max(0, min(5, int(float(obj.get("worth") or 0))))
    except Exception:
        worth = 0
    if not title or not summary or not points:
        return None
    if worth < min_worth:
        return None
    return {"topic": topic, "title": title, "summary": summary, "points": points,
            "tags": tags, "worth": worth}


def build_video_card(chat, sys_p, title, transcript, notes, minutes=0, min_worth=MIN_WORTH):
    """看片后整理卡片；chat(sys_p, prompt) 返回模型文本。返回卡片 dict 或 None（不存）。"""
    prompt = _VIDEO_PROMPT.format(
        topics="、".join(TOPICS), title=_clip(title, 80), minutes=int(minutes or 0),
        transcript=(transcript or "（没有可用的人声转写）")[:5000],
        notes="\n".join(notes[:14]) or "（没有画面描述）")
    try:
        raw = chat(sys_p, prompt)
    except Exception:
        return None
    return _norm_card(_json_obj(raw), min_worth, TOPICS)


def build_material_card(chat, sys_p, title, text, min_worth=0):
    """主人发来的资料整理成卡片；资料再短也至少给一句话 + 一条要点。"""
    prompt = _MATERIAL_PROMPT.format(
        topics="、".join(TOPICS), title=_clip(title, 60), text=(text or "")[:5000])
    try:
        raw = chat(sys_p, prompt)
    except Exception:
        return None
    return _norm_card(_json_obj(raw), min_worth, TOPICS)


def card_text(card, source="", extra=""):
    """卡片正文（进 kb_chunks 的就是这段，不含转写原文）。"""
    lines = []
    if card.get("title"):
        lines.append("标题：" + card["title"])
    if card.get("topic"):
        lines.append("分类：" + card["topic"])
    if card.get("summary"):
        lines.append("一句话：" + card["summary"])
    pts = card.get("points") or []
    if pts:
        lines.append("要点：")
        for i, p in enumerate(pts, 1):
            lines.append("%d. %s" % (i, p))
    if card.get("tags"):
        lines.append("标签：" + "、".join(card["tags"]))
    if source:
        lines.append("来源：" + source)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def doc_name(card):
    return ("【%s】%s" % (card.get("topic") or "其他", card.get("title") or "资料"))[:80]
