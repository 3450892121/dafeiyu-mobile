# -*- coding: utf-8 -*-
"""大肥鱼公共常量与通用助手（从 main.py 原样搬出，行为不变）。"""

import json
import os
import re
import time

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BOT_DIR))
BOT_HOME = os.environ.get("DAFEIYU_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DafeiyuBot")
DATA_DIR = os.path.join(BOT_HOME, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
VISION_SYS = "你是图片/表情包理解助手。用中文简短说明画面、文字和情绪；看不清就直说，不要编造。"
FILES_DIR = os.path.join(DATA_DIR, "files")
ASR_DIR = os.path.join(BOT_HOME, "asr")
# 服务器版：ffmpeg 用系统自带的（apt install ffmpeg），找不到就留空，语音功能会自己报错
def _which(name):
    import shutil
    return shutil.which(name) or ""

FFMPEG = _which("ffmpeg")
ASR_EXE = "llama-funasr-sensevoice"
WEBHOOK_HOST = "127.0.0.1"
LOCAL_MEDIA_ROOTS = [
    os.path.realpath(os.path.join(BOT_HOME, 'Napcat', 'opt', 'QQ', 'resources', 'app', 'app_launcher', 'napcat', 'cache')),
    os.path.realpath(os.path.join(BOT_HOME, 'Napcat', 'opt', 'QQ', 'resources', 'app', 'app_launcher', 'napcat', 'temp')),
    os.path.realpath(os.path.join(BOT_HOME, "napcat", "cache")),
    os.path.realpath(os.path.join(BOT_HOME, "napcat", "temp")),
    # v0.64.1：NapCat 自己下载的图片/文件落在 QQ 的 NapCat 临时目录（get_file 返回的就是这个
    # 本地路径）；以前不认这里，文件就读不出来、引用文件只能看到名字。
    os.path.realpath(os.path.join(os.path.expanduser("~"), ".config", "QQ", "NapCat", "cache")),
    os.path.realpath(os.path.join(os.path.expanduser("~"), ".config", "QQ", "NapCat", "temp")),
]
MAX_TEXT_CHUNK = 1200
MAX_TEXT_BYTES = 4000
MAX_TEXT_PARTS = 8
LONG_DOC_THRESHOLD = 200
WEEK = ["一", "二", "三", "四", "五", "六", "日"]

def _daypart(hour):
    """v0.56.0：把 0~23 点换成时段词，让模型不容易把早上说成中午。"""
    h = int(hour)
    for a, b, name in ((0, 5, "凌晨"), (5, 9, "早上"), (9, 11, "上午"), (11, 13, "中午"),
                       (13, 17, "下午"), (17, 19, "傍晚"), (19, 23, "晚上"), (23, 24, "深夜")):
        if a <= h < b:
            return name
    return "深夜"


def now_line():
    t = time.localtime()
    return "现在是 %04d-%02d-%02d 周%s %s %02d:%02d（北京时间）" % (
        t.tm_year, t.tm_mon, t.tm_mday, WEEK[t.tm_wday], _daypart(t.tm_hour), t.tm_hour, t.tm_min)

def parse_other_bots(raw):
    """把配置里的「名字:QQ,名字:QQ」解析成 {QQ: 名字}。"""
    out = {}
    for part in re.split(r"[,，;；\s]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        nm, sep, qq = part.partition(":")
        if not sep:
            nm, sep, qq = part.partition("：")
        q = _as_int(qq)
        if q:
            out[q] = (nm or str(q)).strip()
    return out




def message_targets_bot(row, self_qq, other_bots=""):
    """一条消息是不是在跟本鲸说：@了本鲸 / 引用了本鲸 / 正文叫了本鲸的名字。

    只在“别人说的话”（direction=in）上判断。返回 (bool, 原因)。
    """
    if not row:
        return False, "没有这条消息"
    d = dict(row)
    if str(d.get("direction") or "") != "in":
        return False, "不是别人说的话"
    self_qq = _as_int(self_qq)
    other = parse_other_bots(other_bots)
    segs = []
    try:
        ev = json.loads(d.get("raw") or "{}")
        segs = ev.get("message") or []
    except Exception:
        segs = []
    at_self = False
    at_other = []
    for s in segs:
        if not isinstance(s, dict) or s.get("type") != "at":
            continue
        q = _as_int((s.get("data") or {}).get("qq"))
        if self_qq and q == self_qq:
            at_self = True
        elif q in other:
            at_other.append("%s(QQ%d)" % (other[q], q))
    text = str(d.get("text") or "")
    name_hit = bool(re.search(r"(?:大肥鱼|本鲸)", text))
    quote_self = False
    try:
        for a in json.loads(d.get("attachments") or "[]"):
            if isinstance(a, dict) and a.get("type") == "quote" and (a.get("is_bot") or a.get("is_self")):
                quote_self = True
    except Exception:
        pass
    if at_other and not at_self and not name_hit and not quote_self:
        return False, "这条是在@%s，不是本鲸" % "、".join(at_other)
    if at_self or quote_self or name_hit:
        return True, "at_self=%s quote_self=%s name=%s" % (at_self, quote_self, name_hit)
    return False, "没@本鲸、没引用本鲸、也没叫本鲸名字"


def group_at_kind(addr_reason):
    """把群门控给的原因翻成触发方式：at / name / quote / 空。"""
    r = str(addr_reason or "")
    if r.startswith("被@了自己"):
        return "at"
    if r.startswith("引用了本鲸"):
        return "quote"
    if r.startswith("正文直接叫了") or r.startswith("名字判断："):
        return "name"
    return ""


def bot_identity_note(self_qq, bot_name="蓝色大肥鱼", other_bots=""):
    """分享/记忆整理共用：先把「谁是你」说清楚，防止把同名号/别的 AI 的话当成自己说的。"""
    qq = _as_int(self_qq)
    name = str(bot_name or "蓝色大肥鱼").strip() or "蓝色大肥鱼"
    who = ("你是 QQ 机器人「%s」（QQ%d）。" % (name, qq)) if qq else ("你是 QQ 机器人「%s」。" % name)
    note = (who + "聊天记录中的「本机器人（你）」只是出站发言的角色标签，不是名字、自称或昵称；"
            "发言归属按 QQ 与入站/出站角色核对，只有你自己的出站记录才是你说的；"
            "群里可能还有别的号也叫「大肥鱼」或「蓝色大肥鱼」，那不是你——"
            "他们说的话、他们被怼的事，都不算你的遭遇。")
    names = list(dict.fromkeys(parse_other_bots(other_bots).values()))
    if names:
        note += ("另外群里还有别的 AI 号：%s——名字像、说话也像，但他们不是你；"
                 "骂他们、夸他们、跟他们打情骂俏都不算你的事，转述前先看清是谁对谁。" % "、".join(names))
    return note


def estimate_tokens(text):
    if not text:
        return 0
    return int(len(text) / 1.8) + 1

def _mime(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"GIF":
        return "image/gif"
    if b[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"

def _as_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0

def _int_list(v):
    out = []
    for x in v or []:
        n = _as_int(x)
        if n > 0:
            out.append(n)
    return out
