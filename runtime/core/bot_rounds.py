# -*- coding: utf-8 -*-
"""机器人互刷刹车（v0.54.9，协议版）。

和混合版同一个规则，只是这里按 QQ 号认机器人（NapCat 看到的就是真实 QQ）：
  - 同一个群里，同一个机器人 QQ 连续触发回复超过 limit 条、中间没有第三人说话 → 不再回；
  - 任何「不是名单里的」人（含主人、群友）说一句 → 计数立刻清零；reset_min 分钟没新往返也清零；
  - 私聊不限。
名单：config 的 bot_peers_qq（逗号分隔的 QQ 号）；留空就用 other_bots 里配的那几个 AI 号。
"""
import time

from common import _as_int

DEFAULT_LIMIT = 6
DEFAULT_RESET_MIN = 10


class BotRoundGuard(object):
    def __init__(self, now=time.time):
        self.state = {}  # gid -> {peer, streak, at}
        self.now = now

    def peers(self, cfg):
        raw = cfg.get("bot_peers_qq") if cfg is not None else ""
        qs = []
        if isinstance(raw, (list, tuple)):
            qs = [_as_int(x) for x in raw]
        else:
            for part in str(raw or "").replace("，", ",").replace("、", ",").split(","):
                q = _as_int(part)
                if q:
                    qs.append(q)
        # 名单完全以你填的为准（可以加/删/换），不再自动拼 other_bots
        return set(q for q in qs if q)

    def peek(self, cfg, gid, uid, counts=True):
        """只读预判（面板「为什么没回」用）：不写 state、不推进计数。"""
        if cfg is None or not bool(cfg.get("bot_rounds_enabled", True)):
            return True, 0, 0
        limit = max(1, min(100, _as_int(cfg.get("bot_round_limit")) or DEFAULT_LIMIT))
        reset_min = max(1, min(1440, _as_int(cfg.get("bot_round_reset_min")) or DEFAULT_RESET_MIN))
        now = self.now()
        st = self.state.get(gid) or {"peer": 0, "streak": 0, "at": 0.0}
        if uid not in self.peers(cfg):
            return True, 0, 0
        streak = st["streak"]
        if now - st["at"] > reset_min * 60 or (st["peer"] and st["peer"] != uid):
            streak = 0
        at_limit = bool(counts) and streak >= limit
        return (not at_limit), (streak + 1 if counts and not at_limit else streak), 0

    def check(self, cfg, gid, uid, counts=True):
        """返回 (allow, streak, reset_from)。allow=False 表示这条别回；reset_from>0 表示刚从几轮解除。"""
        if cfg is None or not bool(cfg.get("bot_rounds_enabled", True)):
            return True, 0, 0
        limit = max(1, min(100, _as_int(cfg.get("bot_round_limit")) or DEFAULT_LIMIT))
        reset_min = max(1, min(1440, _as_int(cfg.get("bot_round_reset_min")) or DEFAULT_RESET_MIN))
        reset_sec = reset_min * 60
        now = self.now()
        st = self.state.get(gid) or {"peer": 0, "streak": 0, "at": 0.0}
        peer = self.peers(cfg)
        if uid not in peer:
            reset_from = st["streak"] if st["streak"] >= limit else 0
            st = {"peer": 0, "streak": 0, "at": now}
            self.state[gid] = st
            return True, 0, reset_from
        if now - st["at"] > reset_sec or (st["peer"] and st["peer"] != uid):
            st["streak"] = 0
        st["peer"] = uid
        st["at"] = now
        at_limit = bool(counts) and st["streak"] >= limit
        if counts and not at_limit:
            st["streak"] += 1
        self.state[gid] = st
        return (not at_limit), st["streak"], 0
