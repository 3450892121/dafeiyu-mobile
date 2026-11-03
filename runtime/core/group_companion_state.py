# -*- coding: utf-8 -*-
"""群陪伴模式的时序状态；不负责读取消息或发送回复。"""

import datetime
import threading
import time


class GroupCompanionState:
    def __init__(self, now=time.time, settle_sec=8, active_sec=300,
                 gap_sec=240, daily_limit=20):
        self.now = now
        self.settle_sec = settle_sec
        self.active_sec = active_sec
        self.gap_sec = gap_sec
        self.daily_limit = daily_limit
        self.lock = threading.Lock()
        self.groups = {}

    def _group(self, gid):
        return self.groups.setdefault(gid, {
            "generation": 0, "last_message": 0, "active_until": 0,
            "last_interjection": 0, "day": None, "count": 0, "quiet_until": 0,
        })

    def observe(self, gid):
        """每条群消息都调用，以使正在进行的旧判断失效。"""
        with self.lock:
            state = self._group(gid)
            state["generation"] += 1
            state["last_message"] = self.now()
            return state["generation"]

    def snapshot(self, gid):
        with self.lock:
            state = self._group(gid)
            now = self.now()
            return state["generation"], now - state["last_message"] >= self.settle_sec, now < state["active_until"]

    def eligible(self, gid, generation, followup=False):
        """模型判断之后再次核对；调用方发送前还需检查配置和机器人状态。"""
        with self.lock:
            state = self._group(gid)
            now = self.now()
            if now < state["quiet_until"]:
                return False
            if state["generation"] != generation or now - state["last_message"] < self.settle_sec:
                return False
            if followup:
                return now < state["active_until"]
            day = datetime.datetime.fromtimestamp(now).date()
            count = state["count"] if state["day"] == day else 0
            return count < self.daily_limit and (not state["last_interjection"] or now - state["last_interjection"] >= self.gap_sec)

    def sent(self, gid, followup=False):
        """只在确认发送成功后记账；普通被叫到的回复可通过 awaken 激活。"""
        with self.lock:
            state = self._group(gid)
            now = self.now()
            state["generation"] += 1
            state["active_until"] = now + self.active_sec
            if not followup:
                day = datetime.datetime.fromtimestamp(now).date()
                if state["day"] != day:
                    state["day"], state["count"] = day, 0
                state["count"] += 1
                state["last_interjection"] = now

    def awaken(self, gid):
        with self.lock:
            self._group(gid)["active_until"] = self.now() + self.active_sec

    def quiet(self, gid):
        with self.lock:
            state = self._group(gid)
            state["active_until"] = 0
            state["quiet_until"] = self.now() + 1800
            state["generation"] += 1
