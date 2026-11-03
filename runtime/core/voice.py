# -*- coding: utf-8 -*-
"""v0.67.0 语音回复：电脑跑 Breeze TTS，服务器这边只发一个 HTTP 请求。

三层权限（优先级从高到低）：
  ① 总开关 voice_enabled / 暂停 voice_paused —— 只有主人（面板）
  ② 会话授权 voice_groups / voice_privates —— 只有主人（面板勾选，或在会话里 @ 它说「开启语音模式」）
  ③ 个人偏好 voice_user_prefs              —— 每个人自己（在会话里 @ 它说「语音回复」/「文字回复」）
  ④ 静音时段 voice_off_periods             —— 只有主人（面板，可加多段）
优先级：暂停 > 静音时段 > 会话授权 > 个人偏好 > 字数/时长/排队。

任何一步不满足都直接回落文字，绝不重试、绝不挡聊天主链路。
"""
import json
import math
import os
import random
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
import guard
import qq_faces

from common import MEDIA_DIR
from onebot import text_seg, record_seg
from util import log, clean_time_windows, window_hit, window_label

# 合成出来的语音条临时落盘位置（只给 NapCat 读，一天一清）
VOICE_TMP = os.path.join(MEDIA_DIR, "voice")
MODEL_ID = "breeze"

# 音色定稿（2026-10-28）：Breeze TTS 2 Q8 + 参考音「近似A-软萌」
DEFAULT_INSTRUCT = "用软萌小女孩的语气说：自然、甜软、带一点鼻音，像随口接话，不夹、不念稿。"
DEFAULT_REF_TEXT = "在呢在呢，主人这一嗓子喊得本鲸大人尾巴都甩起来了。咋啦，又想我啦？"

_lock = threading.Lock()
_inflight = {"n": 0}
# 最后一次真正合成的时间；大脑每分钟看一次，空闲超过 voice_idle_unload_min 就卸载模型
_last_use = {"t": time.time()}
_warm_lock = threading.Lock()
_warm = {"running": False, "t": 0.0}
# 静音时段的进出状态：只用来「进/出时报一次日志」，不然每分钟刷一行
_off_state = {"seen": False, "fail_at": 0.0}
# 暂停的进出状态：出暂停时也主动预热一次（v0.70.1），不然恢复后第一条还是文字
_pause_state = {"seen": False}

# 聊天线程只读后台状态，不为探活等电脑；断线/显存失败后冷却，避免每条消息重试。
_runtime_lock = threading.Lock()
_runtime = {}
REPLY_BUDGET_SEC = 12.0
FAIL_COOLDOWN_SEC = 60.0
STATUS_TTL_SEC = 30.0
READINESS_LOG_SEC = 60.0
CHOICE_RE = re.compile(r"\[\[\s*reply_mode\s*:\s*(voice|text)\s*\]\]", re.I)
CHOICE_NOTE_RE = re.compile(r"\[\[\s*reply_mode\b[^\]\n]*(?:\]\]|\]|\n|$)", re.I)
AUTO_CHOICE_PROMPT = (
    "\n\n# 本轮输出协议（JSON / json，只供程序解析）\n"
    '最终回答只输出一个 JSON 对象，格式为 {"reply_mode":"voice","text":"我在呢。"}。'
    'reply_mode 只能是 "voice" 或 "text"；text 只写直接对对方说的回复正文。'
    "不要输出代码围栏、对象前后的说明或其他字段。程序只会发送或合成 text 字段，"
    "人设里不要标记、只说正文的要求只约束 text，不是让你省略 JSON 包装。"
    "历史里的助手消息已经由程序去掉了包装，只展示当时的正文，不是本轮返回格式的示例；"
    "即使历史都是纯文字，本轮仍须返回包含 reply_mode 和 text 的 JSON 对象。"
    "工具调用本身照常；工具后的最终回答也必须输出上述 JSON 对象，不输出选择理由。\n"
    "现在可以尝试语音。日常短句闲聊、接梗和情绪回应时，优先像平常说话那样亲口接话；"
    "想表达语气时也可以多用语音。可以连续几轮用语音，不为凑多样性而改成文字，也不用刻意交替。"
    "代码、链接、复杂数字、列表清单、需要复制或仔细核对以及较长的解释用文字；"
    "对方明确要文字时用文字。仍由你结合本轮内容决定，不解释选择。\n"
    "选语音时用短句口语、自然停顿，一段约40字，不写列表、Markdown、颜文字、括号动作或mua；"
    "别拖尾音、别主动撒娇，也别说自己发了语音。选文字时正常打字。"
    "历史句尾的语音备注是程序记号，不要照抄。"
)


def strip_choice(text):
    return CHOICE_NOTE_RE.sub("", str(text or "")).strip()


def _choice_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate choice field")
        obj[key] = value
    return obj


def _json_choice(raw):
    """识别协议对象，只取字符串 text；畸形对象不作为聊天正文出站。None 表示旧纯文本。"""
    source = raw
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", source, re.I)
    if fenced:
        source = fenced.group(1).strip()
    looks_json = source.startswith(("{", "[")) or re.search(r'["\']reply_mode["\']\s*:', source)
    if not looks_json:
        return None
    try:
        obj = json.loads(source, object_pairs_hook=_choice_object)
    except (TypeError, ValueError):
        return "", False, "invalid"
    if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
        return "", False, "invalid"
    body = strip_choice(obj["text"])
    if not body:
        return "", False, "invalid"
    choice = obj.get("reply_mode")
    if choice not in ("voice", "text"):
        return body, False, "invalid"
    return body, choice == "voice", choice


def parse_choice(text, mode):
    """纯解析：只返回正文、语音选择和格式分类，不记录原始输出。"""
    raw = str(text or "").strip()
    if mode != "auto":
        # Only autonomous turns requested a JSON envelope. A text-only user may
        # legitimately ask for a JSON document, which must remain their answer.
        body = strip_choice(raw)
        return body, bool(body and mode == "voice"), mode
    # 先识别完整 JSON 包装，不能把 text 字段内部的旧控制行误当成外层协议。
    envelope = _json_choice(raw) if raw.startswith(("{", "```")) else None
    if envelope is not None:
        body, selected, choice = envelope
    else:
        tags = list(CHOICE_RE.finditer(raw))
        valid = len(tags) == 1 and tags[0].start() == 0
        choice = (tags[0].group(1).lower() if valid else
                  "invalid" if tags or CHOICE_NOTE_RE.search(raw) else "missing")
        body = strip_choice(raw)
        selected = valid and choice == "voice"
        nested = _json_choice(body)
        if nested is not None:
            body, nested_selected, nested_choice = nested
            # 网关若把新旧两种包装叠起来，必须选择一致，且依然只发送提取后的正文。
            selected = bool(valid and selected and nested_selected)
            choice = choice if valid and choice == nested_choice else "invalid"
    chosen = bool(body and choice != "invalid" and (mode == "voice" or (mode == "auto" and selected)))
    return body, chosen, choice


def consume_choice(text, mode):
    """JSON 只提取 text；非法/缺失方式只文字，畸形包装失败关闭。"""
    body, chosen, choice = parse_choice(text, mode)
    if mode == "auto":
        # 只记分类，便于区分模型主动选文字与格式失败；不落正文或控制行原文。
        log("voice", "自主选择 choice=%s" % choice)
    return body, chosen


def recover_text_reply(pool, result, system, history, prompt_text, images=None):
    """封套失败后只在同线路恢复一次文字，不重下工具，也不使用思考正文。"""
    model_id = str(getattr(result, "model_id", "") or "")
    if not model_id:
        log("voice", "文字恢复 skipped=no-model")
        return result, ""
    recovery_system = str(system or "").replace(AUTO_CHOICE_PROMPT, "") + (
        "\n\n# 本轮回复格式恢复\n"
        "刚才的回答格式没有可发送的正文。现在直接用自己的语气回答当前这句话，"
        "只输出给对方看的正文，不输出语音选择、JSON封套、控制行或格式说明。"
        "已有工具结果仍是资料；不要再次调用工具，也不要声称完成没有发生的操作。"
    )
    try:
        recovered = pool.chat(recovery_system, history, prompt_text, images=images,
                              model_ids=[model_id], thinking=False, max_tokens=1024,
                              timeout_budget=10, max_attempts=1)
    except Exception as exc:
        log("voice", "文字恢复 failed=%s" % type(exc).__name__)
        return result, ""
    # 即使服务商无视 tools 的缺省值返回工具调用，也不在恢复路径执行。
    if not getattr(recovered, "ok", False) or getattr(recovered, "tool_calls", None):
        log("voice", "文字恢复 failed=%s" % (
            "tool-call" if getattr(recovered, "tool_calls", None) else "model"))
        return recovered, ""
    body, _, label = parse_choice(getattr(recovered, "text", ""), "auto")
    log("voice", "文字恢复 ok=%s format=%s chars=%d" % (bool(body), label, len(body)))
    return recovered, body


def _runtime_state(cfg):
    # 调用方持有 _runtime_lock。按端点隔离，面板改地址后不复用旧探活结果。
    return _runtime.setdefault(_base(cfg), {"checked": 0.0, "checking": False,
        "ok": False, "loaded": False, "blocked_until": 0.0})


def _reply_failed(cfg):
    with _runtime_lock:
        state = _runtime_state(cfg)
        state.update(ok=False, loaded=False, blocked_until=time.monotonic() + FAIL_COOLDOWN_SEC)


def ready_for_reply(cfg):
    """零网络等待：已确认就绪可边用边刷新；未知/离线/卸载/冷却先文字。

    普通消息常隔几十秒才来，不能每过30秒就把已加载的模型当作未就绪。
    此处只决定是否允许选择语音，真正合成前仍由 reply_synth 实时复查。
    """
    if not _base(cfg):
        return False
    now = time.monotonic()
    with _runtime_lock:
        state = _runtime_state(cfg)
        cooling = now < state["blocked_until"]
        fresh = state["checked"] > 0 and now - state["checked"] < STATUS_TTL_SEC
        ready = not cooling and state["checked"] > 0 and state["ok"] and state["loaded"]
        start = (not cooling and (not fresh or now - state["checked"] >= 5.0)
                 and not state["checking"])
        if start:
            state["checking"] = True
        if cooling:
            reason = "cooldown"
        elif ready:
            reason = "ready" if fresh else "stale-ready"
        elif state["ok"] and not state["loaded"]:
            reason = "unloaded"
        elif state["checked"] > 0:
            reason = "offline"
        else:
            reason = "unknown"
        note = state.get("readiness_note") != reason or now - state.get("readiness_note_at", 0) >= READINESS_LOG_SEC
        if note:
            state.update(readiness_note=reason, readiness_note_at=now)
    if note:
        log("voice", "就绪判定 state=%s action=%s" % (reason, "allow-choice" if ready else "text"))
    if start:
        def _probe():
            try:
                st = status(cfg, timeout=0.6)
                with _runtime_lock:
                    state.update(checked=time.monotonic(), ok=bool(st["ok"]), loaded=bool(st["loaded"]))
                if st["ok"] and not st["loaded"] and not off_now(cfg)[0] and not _cover_busy():
                    warm_async(cfg)
            except Exception:
                _reply_failed(cfg)
            finally:
                with _runtime_lock:
                    state["checking"] = False
        threading.Thread(target=_probe, daemon=True).start()
    return bool(ready)


def reply_mode(cfg, conv, is_group, uid):
    if not will_speak(cfg, conv, is_group, uid) or not ready_for_reply(cfg):
        return "text"
    if bool(cfg.get("voice_auto_choose", False)):
        return "auto"
    return "voice"

# 口令一律「整句命中」才算，免得正常说话被当成命令；容忍空格、标点、句尾语气词。
_TAIL = r"[\s，。！!？?~～、,.…]*(?:吧|啊|呀|哈|嘛|哦)?[\s，。！!？?~～、,.…]*$"


def _norm(text):
    t = str(text or "").strip()
    t = re.sub(r"^@\S+\s*", "", t)
    t = re.sub(r"\s+", "", t)
    return t


_CMD_OWNER_ON = re.compile(r"^(?:开启|打开|启用|恢复|打开一下|开启一下|开)语音模式" + _TAIL)
_CMD_OWNER_OFF = re.compile(r"^(?:关闭|关掉|停用|暂停|关闭一下|关)语音模式" + _TAIL)
_CMD_OWNER_STATUS = re.compile(r"^语音模式(?:状态|开了吗|开了没)?" + _TAIL)
_CMD_PREF_ON = re.compile(r"^(?:(?:要|用|改|换成|开启|打开)语音回复|语音回复)" + _TAIL)
_CMD_PREF_OFF = re.compile(r"^(?:(?:要|用|改|换成|开启|打开)文字回复|文字回复)" + _TAIL)


def parse_command(text):
    """整句命中才算口令。返回 owner_on/owner_off/owner_status/pref_on/pref_off 或 None。"""
    t = _norm(text)
    if not t or len(t) > 12:
        return None
    if _CMD_OWNER_ON.match(t):
        return "owner_on"
    if _CMD_OWNER_OFF.match(t):
        return "owner_off"
    if _CMD_OWNER_STATUS.match(t):
        return "owner_status"
    if _CMD_PREF_ON.match(t):
        return "pref_on"
    if _CMD_PREF_OFF.match(t):
        return "pref_off"
    return None


def _as_int(v):
    try:
        n = int(str(v).strip())
    except Exception:
        return 0
    return n if n > 0 else 0


def conv_id(conv):
    """"g:123" / "p:456" → 123 / 456；认不出来就是 0。"""
    try:
        return _as_int(str(conv or "").split(":", 1)[1])
    except Exception:
        return 0


def _id_list(cfg, key):
    out = []
    for x in (cfg.get(key) or []):
        n = _as_int(x)
        if n and n not in out:
            out.append(n)
    return out


def _id_add(cfg, key, val):
    n = _as_int(val)
    ids = _id_list(cfg, key)
    if not n:
        return ids
    if n not in ids:
        ids.append(n)
        cfg.set(key, ids)
    return ids


def _id_del(cfg, key, val):
    n = _as_int(val)
    ids = [x for x in _id_list(cfg, key) if x != n]
    cfg.set(key, ids)
    return ids


def off_now(cfg, ts=None):
    """现在是不是「不语音」时段。返回 (在不在, 命中的那一段或 None)。

    段内一律发文字、不预热、模型也不留在显存（idle_tick 每分钟主动卸一次）。
    配置坏了就当「不在时段」——宁可发语音，也不能因为这个把聊天卡住。
    """
    try:
        for w in clean_time_windows(cfg.get("voice_off_periods") or []):
            if w.get("on") and window_hit(w, ts):
                return True, w
    except Exception:
        return False, None
    return False, None


def off_line(w):
    """日志和回执里怎么说这一段：`静音时段 23:30-07:00（每天）`。"""
    try:
        return "静音时段 %s" % window_label(w)
    except Exception:
        return "静音时段"


def authorized(cfg, conv, is_group):
    """② 这个会话准不准发语音（总开关 + 暂停 + 授权表）。"""
    if not bool(cfg.get("voice_enabled", False)):
        return False
    if bool(cfg.get("voice_paused", False)):
        return False
    want = conv_id(conv)
    if not want:
        return False
    key = "voice_groups" if is_group else "voice_privates"
    return want in _id_list(cfg, key)


def _load_map(cfg, key):
    """{"g:123":[qq...], "p:456":[qq...]} → 干净版（只留正整数 QQ）。"""
    raw = cfg.get(key) or {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, list):
                ids = [_as_int(x) for x in v if _as_int(x)]
                if ids:
                    out[str(k)] = ids
    return out


def _map_has(cfg, key, conv, uid):
    uid = _as_int(uid)
    if not uid:
        return False
    return uid in (_load_map(cfg, key).get(conv) or [])


def default_wants(cfg, is_group):
    """没表态时按会话类型取默认：**群聊和私聊都默认收语音**（面板可关）。"""
    if is_group:
        return bool(cfg.get("voice_group_default", True))
    return bool(cfg.get("voice_private_default", True))


def user_wants(cfg, conv, uid, is_group=False):
    """③ 这个人要不要在这个会话收语音：明确表态优先，没表态按群/私聊默认。"""
    if _map_has(cfg, "voice_user_text", conv, uid):
        return False
    if _map_has(cfg, "voice_user_prefs", conv, uid):
        return True
    if bool(cfg.get("voice_auto_choose", False)):
        return True
    return default_wants(cfg, is_group)


def set_pref(cfg, conv, uid, on):
    """改一个人的表态：要语音进 prefs、要文字进 text，两边互斥。返回改完是「要语音」吗。"""
    uid = _as_int(uid)
    if not uid:
        return False
    want = _load_map(cfg, "voice_user_prefs")
    text = _load_map(cfg, "voice_user_text")
    w = [x for x in (want.get(conv) or []) if x != uid]
    t = [x for x in (text.get(conv) or []) if x != uid]
    if on:
        w.append(uid)
    else:
        t.append(uid)
    for d, arr in ((want, w), (text, t)):
        if arr:
            d[conv] = arr
        else:
            d.pop(conv, None)
    cfg.set("voice_user_prefs", want)
    cfg.set("voice_user_text", text)
    return bool(on)


def prefs_count(cfg, conv, is_group=False):
    """这个会话里明确表态的人数：(要语音, 要文字)。默认值不算人。"""
    return (len(_load_map(cfg, "voice_user_prefs").get(conv) or []),
            len(_load_map(cfg, "voice_user_text").get(conv) or []))


def _base(cfg):
    return str(cfg.get("voice_tts_url") or "").strip().rstrip("/")


def _get_json(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(200000).decode("utf-8", "replace"))


def status(cfg, timeout=3.0):
    """电脑侧 TTS 状态：{"ok": 活不活, "loaded": 模型在不在显存, "version": 版本}。"""
    out = {"ok": False, "loaded": False, "version": ""}
    base = _base(cfg)
    if not base:
        return out
    try:
        h = _get_json(base + "/health", timeout)
        out["ok"] = str(h.get("status") or "") == "ok"
        out["version"] = str(h.get("version") or "")
    except Exception:
        return out
    try:
        m = _get_json(base + "/v1/models", timeout)
        for it in (m.get("data") or []):
            if isinstance(it, dict) and str(it.get("id") or "") == MODEL_ID:
                out["loaded"] = bool(it.get("loaded"))
                break
    except Exception:
        pass
    return out


_DROP_RES = (
    re.compile(r"\[脸:[^\]]*\]"),
    re.compile(r"\[CQ:[^\]]*\]"),
    re.compile(r"\[(?:表情|表情包|图片|语音|文件|视频|链接)[^\]]*\]"),
    re.compile(r"<[^<>]{1,40}>"),
    re.compile(r"https?://\S+"),
)


def clean_for_tts(text):
    """念之前先把不该念的东西去掉：表情标记、CQ 码、链接、Markdown 符号。"""
    t = qq_faces.strip(strip_choice(text))
    # v0.70.2：历史尾注「（这条是用语音说的）」曾被模型照抄进正文，被 TTS 原样念出来
    # （2026-10-29 12:25 真机）。合成前再剥一遍，双保险。
    try:
        import guard
        t = guard.strip_voice_notes(t)
        t = guard.strip_response_notes(t)
    except Exception:
        pass
    for rx in _DROP_RES:
        t = rx.sub("", t)
    t = re.sub(r"[*#`_~>|]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:400]


def wav_seconds(raw):
    """从 wav 头算时长（秒）；算不出来返回 0。"""
    try:
        if not raw or len(raw) < 44 or raw[:4] != b"RIFF":
            return 0.0
        i = 12
        rate = 0
        ch = 1
        bits = 16
        total = 0
        while i + 8 <= len(raw):
            cid = raw[i:i + 4]
            size = struct.unpack("<I", raw[i + 4:i + 8])[0]
            if cid == b"fmt " and size >= 14:
                ch = struct.unpack("<H", raw[i + 10:i + 12])[0] or 1
                rate = struct.unpack("<I", raw[i + 12:i + 16])[0]
                if size >= 16:
                    bits = struct.unpack("<H", raw[i + 22:i + 24])[0] or 16
            elif cid == b"data":
                total = size
                break
            i += 8 + size + (size & 1)
        if rate and ch and bits and total:
            return total / float(rate * ch * (bits // 8))
    except Exception:
        pass
    return 0.0


def _pcm_from_wav(raw):
    """拆出 (采样率, 声道, 位深, PCM 数据)；认不出来返回 None。"""
    try:
        if len(raw) < 44 or raw[:4] != b"RIFF":
            return None
        i = 12
        rate = ch = bits = 0
        data = b""
        while i + 8 <= len(raw):
            cid = raw[i:i + 4]
            size = struct.unpack("<I", raw[i + 4:i + 8])[0]
            body = raw[i + 8:i + 8 + size]
            if cid == b"fmt " and size >= 16:
                ch = struct.unpack("<H", body[2:4])[0] or 1
                rate = struct.unpack("<I", body[4:8])[0]
                bits = struct.unpack("<H", body[14:16])[0] or 16
            elif cid == b"data":
                data = body
                break
            i += 8 + size + (size & 1)
        if rate and ch and bits and data:
            return (rate, ch, bits, data)
    except Exception:
        pass
    return None


def _wav_bytes(rate, ch, bits, pcm):
    n = len(pcm)
    hdr = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * (bits // 8),
                         ch * (bits // 8), bits)
           + b"data" + struct.pack("<I", n))
    return hdr + pcm


def _resample_pcm(data, ratio):
    """线性插值重采样：ratio>1 = 变快变高（音高与语速一起上去）。"""
    n = len(data) // 2
    if n <= 1 or ratio <= 0:
        return data
    s = struct.unpack("<%dh" % n, data[:n * 2])
    m = int(n / ratio)
    out = []
    for k in range(m):
        pos = k * ratio
        i0 = int(pos)
        frac = pos - i0
        v = s[i0] * (1.0 - frac) + s[min(i0 + 1, n - 1)] * frac
        out.append(max(-32768, min(32767, int(v))))
    return struct.pack("<%dh" % m, *out)


def _highshelf(samples, rate, f0, gain_db):
    """RBJ 高架滤波器：f0 以上抬升/衰减 gain_db（负值＝削气声）。"""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / rate
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / 1.0 - 1.0) + 2.0)
    b0 = A * ((A + 1) + (A - 1) * cw + 2 * math.sqrt(A) * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cw)
    b2 = A * ((A + 1) + (A - 1) * cw - 2 * math.sqrt(A) * alpha)
    a0 = (A + 1) - (A - 1) * cw + 2 * math.sqrt(A) * alpha
    a1 = 2 * ((A - 1) - (A + 1) * cw)
    a2 = (A + 1) - (A - 1) * cw - 2 * math.sqrt(A) * alpha
    b0, b1, b2, a0, a1, a2 = b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0
    out = []
    x1 = x2 = y1 = y2 = 0.0
    for v in samples:
        y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, v
        y2, y1 = y1, y
        out.append(max(-32768, min(32767, int(y))))
    return out


def tune_wav(raw, cfg=None):
    """C4 后处理（主人 2026-10-28 选定）：整条升调加速 + 削气声 + 提亮咬字。

    作用：音更高、语速更快、尾音更短、气声更少——听起来更「孩子气」、少一点「腻」。
    参数都在面板可调，调成 1.0 / 0 / 0 就是完全关掉。
    """
    try:
        ratio = float(cfg.get("voice_tune_ratio", 1.06) if cfg is not None else 1.06)
        breath = float(cfg.get("voice_breath_cut_db", -7.0) if cfg is not None else -7.0)
        clarity = float(cfg.get("voice_clarity_db", 1.5) if cfg is not None else 1.5)
    except Exception:
        ratio, breath, clarity = 1.06, -7.0, 1.5
    if abs(ratio - 1.0) < 0.005 and abs(breath) < 0.1 and abs(clarity) < 0.1:
        return raw
    info = _pcm_from_wav(raw)
    if not info:
        return raw
    rate, ch, bits, pcm = info
    if bits != 16 or ch != 1:
        return raw
    try:
        data = _resample_pcm(pcm, ratio) if abs(ratio - 1.0) >= 0.005 else pcm
        n = len(data) // 2
        if n < 2:
            return raw
        s = list(struct.unpack("<%dh" % n, data[:n * 2]))
        if abs(breath) >= 0.1:
            s = _highshelf(s, rate, 6000.0, breath)
        if abs(clarity) >= 0.1:
            s = _highshelf(s, rate, 2500.0, clarity)
        data = struct.pack("<%dh" % n, *s)
        return _wav_bytes(rate, 1, 16, data)
    except Exception as e:
        log("voice", "后处理失败，用原始音频 %r" % (e,))
        return raw


def tune_and_pad(raw, cfg=None):
    """合成结果的统一出口：C4 后处理 → 头尾补静音。面板试听也走这个。"""
    return pad_wav(tune_wav(raw, cfg), cfg)


def pad_wav(raw, cfg=None):
    """给合成结果加一点开头/结尾留白 + 首尾淡入淡出。

    2026-10-28 主人反馈：「开头和结尾太急，像突然被掐断」。模型本身是第一个音素就出声、
    最后一个音素就停，QQ 播完立刻断——加 200ms 头、350ms 尾 + 12ms 淡入淡出就自然多了。
    """
    try:
        lead = int((cfg.get("voice_lead_pad_ms", 220) if cfg is not None else 220) or 0)
        tail = int((cfg.get("voice_tail_pad_ms", 350) if cfg is not None else 350) or 0)
        fade = int((cfg.get("voice_fade_ms", 12) if cfg is not None else 12) or 0)
    except Exception:
        lead, tail, fade = 220, 350, 12
    info = _pcm_from_wav(raw)
    if not info:
        return raw
    rate, ch, bits, pcm = info
    if bits != 16:
        return raw
    frame = ch * 2
    data = bytearray(pcm)
    n = len(data) // frame
    if n <= 0:
        return raw
    f = max(1, int(rate * fade / 1000.0)) if fade > 0 else 0
    if f:
        for k in range(min(f, n)):
            g = k / float(f)
            off = k * frame
            for c in range(ch):
                s = struct.unpack_from("<h", data, off + c * 2)[0]
                struct.pack_into("<h", data, off + c * 2, int(s * g))
            off2 = (n - 1 - k) * frame
            for c in range(ch):
                s = struct.unpack_from("<h", data, off2 + c * 2)[0]
                struct.pack_into("<h", data, off2 + c * 2, int(s * g))
    lead_b = b"\x00\x00" * ch * max(0, int(rate * lead / 1000.0))
    tail_b = b"\x00\x00" * ch * max(0, int(rate * tail / 1000.0))
    return _wav_bytes(rate, ch, bits, lead_b + bytes(data) + tail_b)


def synth(cfg, text, timeout=None):
    """调电脑侧 /v1/audio/speech 合成。返回 (wav_bytes, 错误说明)。"""
    if not guard.content_allowed(SimpleNamespace(cfg=cfg),str(text or '')):
        return None,'内部信息或凭证，未合成'
    base = _base(cfg)
    if not base:
        return None, "没有配 voice_tts_url"
    body_text = clean_for_tts(text)
    if not body_text:
        return None, "文本清完是空的"
    try:
        tp = float(timeout if timeout else (cfg.get("voice_timeout_sec") or 60))
    except Exception:
        tp = 60.0
    payload = {
        "model": MODEL_ID,
        "input": body_text,
        "response_format": "wav",
        "seed": _as_int(cfg.get("voice_seed")) or 10000,
        "instruct": str(cfg.get("voice_instruct") or "").strip() or DEFAULT_INSTRUCT,
    }
    ref = str(cfg.get("voice_ref_path") or "").strip()
    if ref:
        payload["voice_ref"] = ref
    payload["reference_text"] = str(cfg.get("voice_ref_text") or "").strip() or DEFAULT_REF_TEXT
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/audio/speech", data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=tp) as resp:
            raw = resp.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        return None, "HTTP %s %s" % (e.code, detail[:200])
    except Exception as e:
        return None, repr(e)[:200]
    if not raw or raw[:4] != b"RIFF":
        return None, "返回的不是 wav：%s" % (raw[:60],)
    # 头尾留白 + 淡入淡出：不然听起来像一张嘴就被掐断；顺带做 C4 后处理（升调/削气声）
    return tune_and_pad(raw, cfg), ""


def save_wav(raw):
    """把合成结果写到 NapCat 读得到的地方，顺手清掉一天前的。"""
    os.makedirs(VOICE_TMP, exist_ok=True)
    name = "v%d_%04d.wav" % (int(time.time() * 1000), random.randint(0, 9999))
    path = os.path.join(VOICE_TMP, name)
    with open(path, "wb") as f:
        f.write(raw)
    try:
        now = time.time()
        for fn in os.listdir(VOICE_TMP):
            fp = os.path.join(VOICE_TMP, fn)
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > 86400:
                os.remove(fp)
    except Exception:
        pass
    return path


def unload(cfg, timeout=30):
    """让电脑那边把模型从显存里卸掉（需要 --ui-management）。返回 (ok, 说明)。"""
    base = _base(cfg)
    if not base:
        return False, "没有配 voice_tts_url"
    body = json.dumps({"id": MODEL_ID}).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/models/unload", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(200).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return False, "HTTP %s %s" % (e.code, detail[:120])
    except Exception as e:
        return False, repr(e)[:160]
    # 主动卸载后立刻使聊天缓存失效，不能拿刚才的 loaded=True 去触发冷合成。
    with _runtime_lock:
        _runtime_state(cfg).update(loaded=False, checked=0.0)
    return True, raw[:120]


def _idle_min(cfg):
    """面板「模型在显存里保留多久」（分钟）：<=0 表示一直留着。"""
    try:
        return int(cfg.get("voice_idle_unload_min") or 0)
    except Exception:
        return 10


def _keep_warm(cfg):
    """醒来/恢复后要不要主动把模型拉回显存：只给「一直留着」的人拉。

    voice_idle_unload_min>0 的人本来就选了「不用就还显存」，出段时冷启动是他们
    自己的口径，别偷偷把显存占回去。
    """
    try:
        if not bool(cfg.get("voice_enabled", False)):
            return False
        if bool(cfg.get("voice_paused", False)):
            return False
        return _idle_min(cfg) <= 0
    except Exception:
        return False


def idle_tick(cfg):
    """每分钟一次：暂停了、进了静音时段、或者空闲超过设定分钟数，就把显存还回去。

    电脑那边的 --idle-unload-ms 只是 2 小时的兜底（大脑挂了才会用到），真正的
    手刹在这里，所以面板改数字立刻生效、不用重启任何东西。

    v0.70.1：静音时段结束 / 暂停结束时，「模型一直留着」的那档会主动预热一次——
    否则他醒来或恢复后的第一条，一定因为模型不在显存而回落成文字。
    """
    off, w = off_now(cfg)
    ended = False
    if off and not _off_state["seen"]:
        _off_state["seen"] = True
        log("voice", "%s 开始 -> 这段时间只发文字，模型不留在显存" % off_line(w))
    elif not off and _off_state["seen"]:
        _off_state["seen"] = False
        ended = True
        log("voice", "静音时段结束 -> 恢复语音")
    paused = bool(cfg.get("voice_paused", False))
    resumed = False
    if paused and not _pause_state["seen"]:
        _pause_state["seen"] = True
    elif not paused and _pause_state["seen"]:
        _pause_state["seen"] = False
        resumed = True
    st = status(cfg, timeout=3.0)
    if (ended or resumed) and st["ok"] and not st["loaded"] and _keep_warm(cfg):
        warm_async(cfg)
        log("voice", "%s -> 后台预热模型" % ("静音时段结束" if ended else "暂停结束"))
    if not st["ok"] or not st["loaded"]:
        return
    if off:
        ok, msg = unload(cfg)
        if ok:
            _last_use["t"] = time.time()
            log("voice", "%s -> 已卸载模型" % off_line(w))
        elif time.time() - (_off_state["fail_at"] or 0) > 600:
            _off_state["fail_at"] = time.time()
            log("voice", "%s -> 卸载模型失败 %s（10 分钟内不再报）" % (off_line(w), msg))
        return
    if bool(cfg.get("voice_paused", False)):
        ok, msg = unload(cfg)
        log("voice", "暂停中 -> 卸载模型 %s %s" % ("ok" if ok else "失败", msg))
        if ok:
            _last_use["t"] = time.time()
        return
    idle_min = _idle_min(cfg)
    if idle_min <= 0:
        return
    last = _last_use["t"] or time.time()
    if (time.time() - last) > idle_min * 60:
        ok, msg = unload(cfg)
        log("voice", "空闲 %d 分钟 -> 卸载模型 %s %s" % (idle_min, "ok" if ok else "失败", msg))
        if ok:
            _last_use["t"] = time.time()


def warm_async(cfg):
    """模型不在显存里时后台预热一次，别让这一条消息干等十几秒。静音时段不预热。"""
    if (not bool(cfg.get("voice_enabled", False)) or off_now(cfg)[0]
            or bool(cfg.get("voice_paused", False)) or _cover_busy()):
        return
    with _runtime_lock:
        if time.monotonic() < _runtime_state(cfg)["blocked_until"]:
            return
    with _warm_lock:
        if _warm["running"]:
            return
        _warm["running"] = True

    def _run():
        try:
            raw, err = synth(cfg, "嗯。", timeout=240)
            log("voice", "预热 %s" % ("完成" if raw else ("失败 " + str(err))))
            if raw:
                # 预热过程中可能刚到静音时间/主人点了暂停；完成后也要把显存还回去。
                if (off_now(cfg)[0] or bool(cfg.get("voice_paused", False))
                        or not bool(cfg.get("voice_enabled", False))):
                    unload(cfg, timeout=3)
                    return
                # v0.70.1：预热也算「最近一次真用过」，否则刚预热完下一秒就被空闲计时卸掉
                _last_use["t"] = time.time()
                with _runtime_lock:
                    _runtime_state(cfg).update(ok=True, loaded=True, checked=time.monotonic())
            else:
                _reply_failed(cfg)
        except Exception as e:
            _reply_failed(cfg)
            log("voice", "预热异常 %r" % (e,))
        finally:
            with _warm_lock:
                _warm["running"] = False
                _warm["t"] = time.time()

    threading.Thread(target=_run, daemon=True).start()


def _cover_busy():
    """v0.71.0：翻唱任务跑着的时候，一律文字（别和换声抢显存）。"""
    try:
        import cover
        return cover.busy()
    except Exception:
        return False


def will_speak(cfg, conv, is_group, uid):
    """这一轮大概会不会发语音（只看权限、偏心和静音时段，不发任何请求）。

    用来决定「第一个气泡要不要再加随机回复延迟」——合成本身就要几秒，
    再叠 2~4 秒延迟，群里就是 7~10 秒才见到那条语音；静音时段则要按普通文字回合算。
    """
    try:
        if _cover_busy():
            return False
        if not (authorized(cfg, conv, is_group) and user_wants(cfg, conv, uid, is_group)):
            return False
        return not off_now(cfg)[0]
    except Exception:
        return False


def reply_synth(cfg, text):
    """聊天合成最多等12秒；超时结果丢弃，工人结束前占住名额，绝不补发或排队。"""
    with _lock:
        if _inflight["n"]:
            return None, "语音正在忙，直接文字"
        _inflight["n"] += 1
    done = threading.Event()
    result = [None, "合成超时"]
    try:
        budget = min(REPLY_BUDGET_SEC, max(0.1, float(cfg.get("voice_timeout_sec") or 60)))
    except (TypeError, ValueError):
        budget = REPLY_BUDGET_SEC

    def _run():
        try:
            # 缓存只是生成回复时的参考。合成前在工人线程核对一次，处理定时/外部卸载竞态；
            # 未加载时绝不让这条消息等待冷启动（即使旧配置曾允许阻塞冷启动）。
            st = status(cfg, timeout=0.6)
            if not st["ok"]:
                result[:] = [None, "电脑侧离线"]
                _reply_failed(cfg)
                return
            if not st["loaded"]:
                with _runtime_lock:
                    _runtime_state(cfg).update(ok=True, loaded=False, checked=time.monotonic())
                warm_async(cfg)
                result[:] = [None, "模型已卸载，先文字、后台预热"]
                return
            with _runtime_lock:
                cooling = time.monotonic() < _runtime_state(cfg)["blocked_until"]
            if cooling:
                result[:] = [None, "语音失败冷却中，已改文字"]
                return
            if not bool(cfg.get("voice_enabled", False)) or bool(cfg.get("voice_paused", False)) or off_now(cfg)[0] or _cover_busy():
                result[:] = [None, "已进入文字时段或暂停"]
                return
            result[:] = synth(cfg, strip_choice(text), timeout=budget)
            if not result[0]:
                _reply_failed(cfg)
        except Exception as e:
            result[:] = [None, type(e).__name__]
            _reply_failed(cfg)
        finally:
            with _lock:
                _inflight["n"] = max(0, _inflight["n"] - 1)
            done.set()
    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(budget):
        _reply_failed(cfg)
        return None, "合成超时，已改文字"
    return tuple(result)


def bubble_audio(cfg, conv, is_group, uid, text):
    """第一条气泡要不要改语音：返回 wav 文件路径；None = 照旧发文字。"""
    if _cover_busy():
        log("voice", "回落 原因=翻唱进行中")
        return None
    if not authorized(cfg, conv, is_group):
        return None
    if not user_wants(cfg, conv, uid, is_group):
        return None
    off, w = off_now(cfg)
    if off:
        log("voice", "回落 原因=%s conv=%s" % (off_line(w), conv))
        return None
    body = clean_for_tts(text)
    if not body:
        return None
    try:
        max_chars = int(cfg.get("voice_max_chars") or 120)
    except Exception:
        max_chars = 120
    if max_chars > 0 and len(body) > max_chars:
        log("voice", "回落 原因=文本过长 %d>%d conv=%s" % (len(body), max_chars, conv))
        return None
    if not ready_for_reply(cfg):
        log("voice", "回落 原因=语音未就绪或失败冷却，直接文字 conv=%s" % conv)
        return None
    try:
        t0 = time.time()
        raw, err = reply_synth(cfg, body)
        if not raw:
            log("voice", "回落 原因=合成失败 %s conv=%s" % (err, conv))
            return None
        with _runtime_lock:
            cooling = time.monotonic() < _runtime_state(cfg)["blocked_until"]
        if (off_now(cfg)[0] or not authorized(cfg, conv, is_group)
                or not user_wants(cfg, conv, uid, is_group) or _cover_busy() or cooling):
            log("voice", "回落 原因=合成期间权限/偏好/静音/冷却状态变化 conv=%s" % conv)
            return None
        sec = wav_seconds(raw)
        try:
            amax = int(cfg.get("voice_max_audio_sec") or 40)
        except Exception:
            amax = 40
        if amax > 0 and sec > amax:
            log("voice", "回落 原因=音频过长 %.1fs>%ds conv=%s" % (sec, amax, conv))
            return None
        path = save_wav(raw)
        _last_use["t"] = time.time()
        log("voice", "合成 ok 音频=%.1fs 用时=%.1fs conv=%s" % (sec, time.time() - t0, conv))
        return path
    except Exception as e:
        _reply_failed(cfg)
        log("voice", "回落 原因=%s conv=%s" % (type(e).__name__, conv))
        return None


def _status_line(cfg, conv, is_group):
    if not bool(cfg.get("voice_enabled", False)):
        return "语音现在是关着的（总开关没开）。"
    if bool(cfg.get("voice_paused", False)):
        return "语音现在是暂停的（打游戏用的那个开关）。"
    if not authorized(cfg, conv, is_group):
        return "这里还没开语音，得主人先开。"
    offh, w = off_now(cfg)
    if offh:
        return "现在是%s：这段时间我只打字（模型也不占显存），过了这段就恢复语音。" % off_line(w)
    if bool(cfg.get("voice_auto_choose", False)):
        return "这里可以发语音，我会按聊天自己选语音或文字；明确要文字的人只收文字。电脑或语音没准备好时就打字。"
    on, off = prefs_count(cfg, conv, is_group)
    if default_wants(cfg, is_group):
        tail = "（%d 人改成了文字）" % off if off else "（没人改成文字）"
        return "这里开了语音：默认都收语音%s。" % tail
    return "这里开了语音：%d 个人选了收语音%s。" % (on, "，%d 人明确要文字" % off if off else "")


def ledger_note(mid, conv, text, sender=""):
    """v0.69.4：把这句语音的原文写进共享账本（别的机器人读它，比 ASR 准）。"""
    try:
        import voice_ledger
        return voice_ledger.write_note(mid, conv, text, sender)
    except Exception:
        return False


def _voice_ack(bot, conv, is_group, gid, uid, name, kind):
    """用模型自己的话回一句口令回执，并合成成语音条。返回 (wav路径或None, 说的话)。

    kind="off"：对方刚说只要文字，回一句告别的语音；kind="on"：对方要语音，回一句答应。
    合成失败就返回 (None, 文字)，上层退回文字照发。
    """
    cfg = bot.cfg
    offh, w = off_now(cfg)
    if offh:
        # 静音时段不动模型：口令回执退回文字（上层有现成的文字回执）
        log("voice", "%s，口令回执不用语音" % off_line(w))
        return None, ""
    if _cover_busy() or not ready_for_reply(cfg):
        return None, ""
    try:
        bot_name = cfg.get("bot_name") or "大肥鱼"
        if kind == "off":
            task = ("对方刚说了以后别发语音、他只要文字。用你平时的口气回一句（15 字以内）："
                    "答应他，再说一句以后就打字聊。")
            user = "对方：以后别发语音了，我只要文字。"
        else:
            task = ("对方刚说了想要语音回复。用你平时的口气回一句（15 字以内）："
                    "答应他，再说一句以后就用声音回他。")
            user = "对方：以后用语音回我吧。"
        sys_p = ("你就是「%s」，正在跟对方聊天。%s"
                 "只输出这一句话：不要引号、不要解释、不要反问他、不要写括号里的动作。" % (bot_name, task))
        r = bot.pool.chat(sys_p, [], user, max_tokens=80, temperature=0.9, thinking=False)
        if not getattr(r, "ok", False):
            log("voice", "口令回执生成失败 %s" % (getattr(r, "error", "") or ""))
            return None, ""
        line = clean_for_tts(getattr(r, "text", "") or "")[:60]
        if not line:
            return None, ""
        raw, err = reply_synth(cfg, line)
        if not raw:
            log("voice", "口令回执合成失败 %s" % err)
            return None, line
        return save_wav(raw), line
    except Exception as e:
        log("voice", "口令回执语音异常 %r" % (e,))
        return None, ""


def _send_voice_reply(bot, conv, is_group, gid, uid, name, cmd_text, path, said):
    """把口令回执当语音条发出去，并入库（历史里显示 [语音]）。"""
    try:
        bot._mark_group_at(is_group, gid, uid, "at")
    except Exception:
        pass
    ok = False
    try:
        ok = bot._send(is_group, gid, uid, [record_seg(path)])
        if ok and said:
            ledger_note(getattr(bot, "_last_sent_mid", None), conv, said, getattr(bot, "self_id", ""))
    except Exception as e:
        log("voice", "口令语音发送失败 %r" % (e,))
    try:
        bot.store.add_message(conv, uid, name or "", "in", str(cmd_text or ""), "", 0, [])
        bot.store.add_message(conv, bot.self_id, bot.cfg.get("bot_name"), "out",
                              said or "（发了条语音）", "", 0,
                              [{"type": "record", "note": "语音条"}])
        bot.store.touch_session(conv)
    except Exception as e:
        log("voice", "口令语音入库失败 %r" % (e,))
    return ok


def handle_command(bot, cmd_text, conv, is_group, gid, uid, is_owner, name=""):
    """口令入口。返回 True 表示这条已经处理完，不再走模型。"""
    kind = parse_command(cmd_text)
    if not kind:
        return False
    cfg = bot.cfg
    line = ""
    if kind == "owner_on":
        if not is_owner:
            line = "这个只有主人能开。"
        else:
            if is_group:
                _id_add(cfg, "voice_groups", gid)
                line = "好，这个群可以发语音了。谁想听，在这里 @ 我说一句「语音回复」。"
            else:
                _id_add(cfg, "voice_privates", conv_id(conv))
                line = "好，这里可以发语音了。想听就说「语音回复」，不想听就说「文字回复」。"
    elif kind == "owner_off":
        if not is_owner:
            line = "这个只有主人能关。"
        else:
            if is_group:
                _id_del(cfg, "voice_groups", gid)
            else:
                _id_del(cfg, "voice_privates", conv_id(conv))
            line = "好，语音先关了，这里都发文字。"
    elif kind == "owner_status":
        line = _status_line(cfg, conv, is_group)
    elif kind == "pref_on":
        if not authorized(cfg, conv, is_group):
            line = "这里还没开语音，得主人先开。"
        else:
            already = user_wants(cfg, conv, uid, is_group)
            # 自主模式/默认语音也要记偏好；自主开启时代表允许收语音。
            set_pref(cfg, conv, uid, True)
            offh, w = off_now(cfg)
            if offh:
                # 静音时段先文字答应一声，别在这会儿把模型拉起来
                line = ("好，我记着了。不过现在是%s，这段时间我只打字；"
                        "过了这一段，我就用声音回你。") % off_line(w)
            elif bool(cfg.get("voice_auto_choose", False)):
                line = "好，记着你可以收语音了，我会按聊天自己选语音或文字。想只看文字就说「文字回复」。"
            else:
                # 回执也用它的声音说（它自己组织话），比固定一句文字自然
                path, said = _voice_ack(bot, conv, is_group, gid, uid, name, "on")
                if path:
                    _send_voice_reply(bot, conv, is_group, gid, uid, name, cmd_text, path, said)
                    log("voice", "口令 %s conv=%s 回执=语音" % (kind, conv))
                    return True
                line = "本来就在用语音回你呀。" if already else "好，以后在这里我用语音回你。"
    elif kind == "pref_off":
        if not authorized(cfg, conv, is_group):
            line = "这里还没开语音，得主人先开。"
        elif not user_wants(cfg, conv, uid, is_group):
            set_pref(cfg, conv, uid, False)
            line = "本来就在发文字呀。"
        else:
            set_pref(cfg, conv, uid, False)
            # 对方明确说不要语音了：这一条用它的声音答应一声（也是最后一条语音）
            path, said = _voice_ack(bot, conv, is_group, gid, uid, name, "off")
            if path:
                _send_voice_reply(bot, conv, is_group, gid, uid, name, cmd_text, path, said)
                log("voice", "口令 %s conv=%s 回执=语音（告别）" % (kind, conv))
                return True
            line = "好，以后在这里我给你发文字。"
    if not line:
        return True
    try:
        bot._mark_group_at(is_group, gid, uid, "at")
    except Exception:
        pass
    try:
        bot._send(is_group, gid, uid, [text_seg(line)])
        bot.store.add_message(conv, uid, name or "", "in", str(cmd_text or ""), "", 0, [])
        bot.store.add_message(conv, bot.self_id, cfg.get("bot_name"), "out", line, "", 0, [])
        bot.store.touch_session(conv)
    except Exception as e:
        log("voice", "口令回执失败 %r" % (e,))
    log("voice", "口令 %s conv=%s uid=%s owner=%s" % (kind, conv, uid, is_owner))
    return True
