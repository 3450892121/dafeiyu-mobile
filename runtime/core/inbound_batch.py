"""私聊攒批：连发多条 → 等安静 N 秒 → 合并成一条事件再处理。

为什么要有它：私聊里对方常常一句一个气泡连发三四条，一条一条回就成了碎嘴，
模型也会被前一句带着跑。这里只做一件事——按会话把私聊消息攒进池子，
等「安静 debounce_sec 秒」再合并冲出去，让 main.handle() 只回一轮。
群聊完全不进这个池（群聊门控一行没动）。

用户拍板的口径（不要自行改）：
- 静默窗口从「最后一条」算，默认 3 秒。
- 时间兜底 max_wait_sec 默认 0＝不限：对方还在说就一直等，绝不插嘴。
- 条数兜底 max_msgs 默认 20（0＝不限），只为防机器刷屏；到数立即冲。
- 合并时相邻两条消息之间必须插一个换行段：flow_context._parse() 用
  "".join(texts) 拼正文，不插换行会粘成一句。
- 线程安全：池操作全走 self._lock；定时器用 threading.Timer + daemon；
  回调只在锁外调用，且 on_flush 里只许做入队（不许耗时操作）。
"""

import threading
import time


class InboundBatcher(object):
    """按 key（私聊用 "p:<uid>"）攒消息，安静 debounce_sec 秒后合并冲出去。

    参数：
      debounce_sec  静默窗口（秒），从最后一条算；每次 feed 重置。
      max_msgs      条数兜底，>0 时到数立即冲；0＝不限。
      max_wait_sec  时间兜底，>0 时从「这一池的第一条」起算硬超时；0＝不限。
      on_flush      回调，参数是 merge() 出来的合并事件。
    """

    def __init__(self, debounce_sec=3.0, max_msgs=20, max_wait_sec=0.0, on_flush=None):
        self._lock = threading.Lock()
        self.debounce_sec = max(0.0, float(debounce_sec or 0.0))
        self.max_msgs = max(0, int(max_msgs or 0))
        self.max_wait_sec = max(0.0, float(max_wait_sec or 0.0))
        self.on_flush = on_flush
        self._pools = {}   # key -> {"events": [...], "timer": Timer, "first_at":, "last_at":, "gen": int}
        self.flush_count = 0   # 冲池次数（面板/自测看）
        self.feed_count = 0    # 入池条数

    # ---------- 配置热更新 ----------
    def configure(self, debounce_sec=None, max_msgs=None, max_wait_sec=None):
        """面板改值后重新对齐参数（已经在等的池子不重排，下一条进池时按新值算）。"""
        with self._lock:
            if debounce_sec is not None:
                self.debounce_sec = max(0.0, float(debounce_sec or 0.0))
            if max_msgs is not None:
                self.max_msgs = max(0, int(max_msgs or 0))
            if max_wait_sec is not None:
                self.max_wait_sec = max(0.0, float(max_wait_sec or 0.0))

    # ---------- 入池 ----------
    def feed(self, key, ev):
        """入池并重置静默计时；到条数上限立即冲。返回这一池当前的条数。"""
        merged = None
        with self._lock:
            pool = self._pools.get(key)
            now = time.time()
            if pool is None:
                pool = {"events": [], "timer": None, "first_at": now, "last_at": now, "gen": 0}
                self._pools[key] = pool
            else:
                # 同一条消息的重复投递不再进池（平台重投会把一句话答两遍）
                mid = ev.get("message_id") if isinstance(ev, dict) else None
                if mid is not None and any(
                        isinstance(e, dict) and e.get("message_id") == mid for e in pool["events"]):
                    return len(pool["events"])
            pool["events"].append(ev)
            pool["last_at"] = now
            self.feed_count += 1
            count = len(pool["events"])
            if self.max_msgs > 0 and count >= self.max_msgs:
                events = self._take_locked(key)
                merged = self.merge(events)
            else:
                self._arm_locked(key, pool)
        if merged is not None:
            # 锁外回调：on_flush 只做入队，绝不能阻塞定时器线程
            self._deliver(merged)
        return count

    # ---------- 冲池 ----------
    def flush(self, key):
        """冲一个池；有内容就回调 on_flush(merged)。返回合并后的事件（没有则 None）。"""
        with self._lock:
            events = self._take_locked(key)
        if not events:
            return None
        merged = self.merge(events)
        self._deliver(merged)
        return merged

    def flush_all(self):
        """冲掉所有池（优雅退出/停机前用）。返回冲掉的池数。"""
        with self._lock:
            keys = list(self._pools.keys())
        n = 0
        for key in keys:
            if self.flush(key) is not None:
                n += 1
        return n

    def drop(self, key):
        """丢弃一个池（不回调）。返回丢掉的条数。"""
        with self._lock:
            return len(self._take_locked(key))

    def pending(self):
        """各池当前积压条数 {key: n}（面板/状态页看）。"""
        with self._lock:
            return {k: len(p["events"]) for k, p in self._pools.items() if p["events"]}

    def pending_total(self):
        with self._lock:
            return sum(len(p["events"]) for p in self._pools.values())

    # ---------- 内部 ----------
    def _take_locked(self, key):
        """（锁内）拿走整池并让旧定时器作废。"""
        pool = self._pools.pop(key, None)
        if not pool:
            return []
        pool["gen"] += 1          # 让已经排上的 Timer 回调认出自己是旧的
        t = pool.get("timer")
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        return list(pool["events"])

    def _arm_locked(self, key, pool):
        """（锁内）按「最后一条+静默」和「第一条+兜底」里较早的那个排定时器。"""
        old = pool.get("timer")
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        pool["gen"] = int(pool.get("gen") or 0) + 1
        target = pool["last_at"] + self.debounce_sec
        if self.max_wait_sec > 0:
            target = min(target, pool["first_at"] + self.max_wait_sec)
        delay = max(0.0, target - time.time())
        t = threading.Timer(delay, self._fire, args=(key, pool["gen"]))
        t.daemon = True
        pool["timer"] = t
        t.start()

    def _fire(self, key, gen):
        """定时器到点：只有「池子还是排定时器时那一代」才冲（否则说明期间又进了新消息）。"""
        with self._lock:
            pool = self._pools.get(key)
            if pool is None or int(pool.get("gen") or 0) != gen:
                return
            events = self._take_locked(key)
        if not events:
            return
        self._deliver(self.merge(events))

    def _deliver(self, merged):
        if not isinstance(merged, dict):
            return
        self.flush_count += 1
        cb = self.on_flush
        if cb is None:
            return
        try:
            cb(merged)
        except Exception:
            # 定时器线程里绝不能把异常抛出去（会静默杀掉这个 Timer 线程）
            pass

    # ---------- 合并 ----------
    @staticmethod
    def merge(events):
        """把一池事件合成一条：message 段按顺序拼，相邻两条之间插换行段。

        基础字段取最后一条（message_id / time / sender / user_id / group_id /
        message_type / post_type / self_id 等），另外带：
          _batch_ids  这一批全部 message_id（handle 里用来标 seen，防平台重投重复答）
          _batched    这一批的条数
        quote/reply 段照原样留在拼接结果里（flow_context._reply_id 会自己扫）。
        """
        events = [e for e in (events or []) if isinstance(e, dict)]
        segs = []
        ids = []
        for i, ev in enumerate(events):
            if i:
                # 换行段：_parse() 是 "".join(texts)，不插换行两条话会粘成一句
                segs.append({"type": "text", "data": {"text": "\n"}})
            for s in (ev.get("message") or []):
                segs.append(s)
            mid = ev.get("message_id")
            if mid and mid not in ids:
                ids.append(mid)
        merged = dict(events[-1]) if events else {}
        merged["message"] = segs
        merged["_batch_ids"] = ids
        merged["_batched"] = len(events)
        return merged
