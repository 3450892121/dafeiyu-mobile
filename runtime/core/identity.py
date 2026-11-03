# -*- coding: utf-8 -*-
"""认人：把 QQ 号变成稳定昵称，生成给模型看的 @ 说明。

设计约束：
- 只读 store.people / store.person_name，不写库、不联网。名字缺失时用消息里自带的昵称兜底。
- self_id 在 Bot.__init__ 时可能是 0，登录后才由 login 循环写回 Bot.self_id；
  ContextMixin 每次用之前会调 sync_self_id()，所以这里不用自己猜。
"""

from store import _clean_name, _person_label


def _as_qq(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0


class Identity(object):
    def __init__(self, store, onebot=None, self_id=0, cfg=None):
        self.store = store
        self.onebot = onebot
        self.cfg = cfg
        self.self_id = _as_qq(self_id)

    @staticmethod
    def person_label(qq, name):
        """统一「昵称(QQ号)」格式。"""
        return _person_label(qq, name)

    def bot_name(self):
        try:
            nm = self.cfg.get("bot_name") if self.cfg else ""
        except Exception:
            nm = ""
        return _clean_name(nm, 40) or "蓝色大肥鱼"

    def sync_self_id(self, qq):
        """Bot 登录后把真实 self_id 同步进来；0 不覆盖已有值。"""
        q = _as_qq(qq)
        if q:
            self.self_id = q
        return self.self_id

    def name(self, qq, fallback=""):
        """稳定昵称：people 表优先，其次消息里带的昵称，最后退回 QQ 号。"""
        q = _as_qq(qq)
        if q and q == self.self_id:
            return self.bot_name()
        nm = ""
        if q and self.store is not None:
            try:
                nm = self.store.person_name(q) or ""
            except Exception:
                nm = ""
            if not nm:
                # 老数据里 name 可能被清空，但 aliases 还留着最后一次见过的昵称
                try:
                    aliases = self.store.person_aliases(q)
                except Exception:
                    aliases = []
                if aliases:
                    nm = aliases[-1]
            if not nm:
                # people 表里没有时，用他最近一次发言的群名片兜底（@ 别人时至少能显示名字）
                try:
                    nm = self.store.last_sender_name(q) or ""
                except Exception:
                    nm = ""
        nm = _clean_name(nm, 40)
        if not nm:
            nm = _clean_name(fallback, 40)
        if not nm:
            nm = str(q) if q else "未知"
        return nm

    def mention_names(self, segs):
        """返回 [(qq, name, is_self)]；@全体成员记成 (0, '全体成员', False)。"""
        out, seen = [], set()
        for s in segs or []:
            if not isinstance(s, dict) or s.get("type") != "at":
                continue
            d = s.get("data") or {}
            raw = d.get("qq")
            if raw is None or str(raw).strip() == "":
                continue
            if str(raw).strip().lower() == "all":
                if "all" not in seen:
                    seen.add("all")
                    out.append((0, "全体成员", False))
                continue
            q = _as_qq(raw)
            if not q or q in seen:
                continue
            seen.add(q)
            nm = self.name(q, d.get("name") or "")
            out.append((q, nm, bool(self.self_id and q == self.self_id)))
        return out

    def describe_mentions(self, segs, sender_qq=0):
        """给系统提示看的一句中文：谁被 @ 了、是不是 @ 你、发送人有没有被 @。"""
        items = self.mention_names(segs)
        if not items:
            return ""
        self_labels, other_labels, sender_mentioned = [], [], False
        at_all = False
        sender = _as_qq(sender_qq)
        for q, nm, is_self in items:
            if is_self:
                self_labels.append("你")
            elif not q:
                at_all = True
            else:
                other_labels.append(_person_label(q, nm))
            if sender and q and q == sender:
                sender_mentioned = True
        if self_labels:
            if other_labels or at_all:
                names = list(other_labels)
                if at_all:
                    names.append("全体成员")
                line = "这条消息被 @ 的人：%s；你也被 @ 了" % "、".join(names)
            else:
                line = "这条消息 @ 了你"
        elif other_labels or at_all:
            names = list(other_labels)
            if at_all:
                names.append("全体成员")
            line = "这条 @ 的是 %s，不是在 @ 你" % "、".join(names)
        else:
            return ""
        if sender:
            sender_name = self.name(sender, "")
            sender_label = _person_label(sender, sender_name)
            if sender_mentioned:
                line += "；其中 %s 是发送人自己" % sender_label
            else:
                line += "；发送人 %s 不在被 @ 的人里" % sender_label
        return line


__all__ = ["Identity"]
