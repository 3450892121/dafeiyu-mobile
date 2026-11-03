# -*- coding: utf-8 -*-
"""语音原文账本：机器人发语音条时，把**原文**写进共享文件，别的机器人直接读，
不用跑 ASR，100% 准确。

2026-10-28 主人提的：「能不能把机器人发的话塞给其他机器人，给语音加翻译」——
比转写靠谱。实现成 /var/lib/dafeiyu/gate 下的 jsonl（三只共用、同一台机器），
按 message_id 对齐；读不到再退回 ASR。
"""

import json
import os
import time

LEDGER_PATH = os.environ.get("DAFEIYU_VOICE_LEDGER", "/var/lib/dafeiyu/gate/voice_texts.jsonl")
_MAX_LINES = 4000
_MAX_BYTES = 2 * 1024 * 1024


def _ensure_dir():
    try:
        d = os.path.dirname(LEDGER_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass


def write_note(mid, conv, text, sender=""):
    """发完语音条后记一条原文。mid 拿不到就按「会话+时间」兜底。"""
    try:
        text = str(text or "").strip()
        if not text:
            return False
        _ensure_dir()
        row = {"mid": str(mid) if mid else "", "conv": str(conv or ""),
               "ts": int(time.time()), "sender": str(sender or ""), "text": text[:600]}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        _maybe_prune()
        return True
    except Exception:
        return False


def _maybe_prune():
    try:
        if not os.path.exists(LEDGER_PATH):
            return
        if os.path.getsize(LEDGER_PATH) < _MAX_BYTES:
            return
        with open(LEDGER_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_LINES:
            return
        keep = lines[-_MAX_LINES:]
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        pass


def _rows(max_age=600):
    try:
        if not os.path.exists(LEDGER_PATH):
            return []
        with open(LEDGER_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-800:]
        now = int(time.time())
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if now - int(row.get("ts") or 0) <= max_age:
                out.append(row)
        return out
    except Exception:
        return []


def lookup(mid=None, conv="", sender="", window=150):
    """三级匹配，任何一级都只认**唯一**结果，宁可放弃也不张冠李戴。

    1) message_id 完全相同（同通道最准）
    2) 同会话 + 同发送者 + 180 秒内恰好一条
    3) 跨通道兜底：官方通道和 NapCat 的 message_id / 群号是两套命名空间
       （缇宝看到「群200004 + 本地号」，大肥鱼写「g:1037599423 + NapCat 号」），
       只能按「时间窗口内恰好一条」认。
    """
    rows = _rows()
    if not rows:
        return ""
    mid = str(mid or "")
    if mid:
        for row in reversed(rows):
            if row.get("mid") and str(row.get("mid")) == mid:
                return str(row.get("text") or "")
    now = int(time.time())
    cand = []
    for row in reversed(rows):
        if conv and row.get("conv") != str(conv):
            continue
        if sender and row.get("sender") and str(row.get("sender")) != str(sender):
            continue
        if now - int(row.get("ts") or 0) > 180:
            continue
        cand.append(row)
    if len(cand) == 1:
        return str(cand[0].get("text") or "")
    try:
        win = int(window or 150)
        cand2 = [row for row in rows if abs(now - int(row.get("ts") or 0)) <= win]
        if len(cand2) == 1:
            return str(cand2[0].get("text") or "")
    except Exception:
        pass
    return ""
