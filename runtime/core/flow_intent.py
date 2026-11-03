# -*- coding: utf-8 -*-
"""理解力强化：先把“这一句到底在要什么”读懂，再交给回复模型。

背景（2026-10-23 用户截图）：
  主人：「不要嘛，不给叫我就不会给你大白饭了」
  它：  「还带往回拿的啊？…行吧，我又没说不叫：在我心里叫，行了吧」
字面上接住了“大白饭”，但没正面回应“叫一声”这个要求，还用“在心里叫”糊过去。
用户原话：「能不能加强一下他的理解能力啊」。

拆成两层：
1) 提示词层（persona.py）：加“先读懂再回”的硬规则，禁止用“在心里叫/精神上答应”这类空话绕开要求。
   零延迟，所有会话都吃。
2) 判断层（本文件）：只在粗筛命中“条件句 / 交易 / 明确要求 / 反话 / 指代”时，
   跑一次小模型（thinking=False，约 0.5~1 秒），把「要什么 / 条件 / 语气 / 最容易漏什么」
   作为参考注入系统提示。普通闲聊不触发；判断失败、超时、解析失败一律不注入，回复照常。

只做判断，不改回复内容；除了面板日志，不写库、不碰 QQ。
"""

import json
import re

from conversation_understanding import turn_frame, frame_note
from util import log


# 只在这些“容易答偏”的说法上触发：条件句、交易、明确要求、反话、指代。
# 普通闲聊不跑这层，别为了“更懂”把每条消息都拖慢一秒。
_CUE = re.compile(
    r"(不[^，。！？!?\s]{0,6}就(?:不|没|别|给|是|叫)|"
    r"要是|否则|除非|才(?:给|叫|说|答应)|"
    r"跟你换|拿.{0,6}换|用.{0,6}换|换你一|"
    r"叫[^，。！？!?\s]{0,3}一声|叫我|让你叫|你再叫|"
    r"可真|倒是(?:挺|会|行)|真会|行啊你|挺(?:厉害|行)|"
    r"那个呢|这个呢|刚才那个|刚才说的|上面说的|你说的那个|"
    r"你是不是让|你是让我|我的意思是|这不是让你)"
)


def looks_indirect(text):
    """粗筛：这句话有没有“字面之外的东西”值得先读一遍。"""
    t = str(text or "").strip()
    if len(t) < 6:
        return False
    return bool(_CUE.search(t))


class IntentMixin(object):
    """理解力：把“对方这句话到底要什么”先读出来，再交给回复模型。"""

    def _intent_judge_prompt(self, text, history, name, is_group):
        lines = ["你是聊天意图判断器：只读对方这句话到底要什么，不回复、不评价、不教人怎么改。", ""]
        try:
            me = self._bot_name()
        except Exception:
            me = "蓝色大肥鱼"
        lines.append("对方是「%s」；你是 QQ 机器人「%s」，%s。" % (
            name or "对方", me or "蓝色大肥鱼", "群聊" if is_group else "私聊"))
        lines.append("")
        if history:
            lines.append("最近几句（越靠下越新；群聊方括号内才是发言人，不能把群友都认成当前对方；引用旧话不是引用者说的）：")
            for h in (history or [])[-8:]:
                role = "你（机器人）" if h.get("role") == "assistant" else ("群聊发言（作者见方括号）" if is_group else "对方")
                t = re.sub(r"\s+", " ", str(h.get("content") or "")).strip()[:160]
                if t:
                    lines.append("- %s：%s" % (role, t))
            lines.append("")
        lines.append("当前这句：%s" % str(text or "").strip()[:400])
        lines.append("")
        lines.append("只输出一行 JSON：")
        lines.append('{"want":"他真正想让你做的一件事（一句话；没有具体事就写空）",'
                     '"cond":"他给的条件或筹码（没有就写空）",'
                     '"tone":"撒娇|威胁|阴阳|开玩笑|认真|求助|闲聊 里选一个",'
                     '"trap":"只按字面回复最容易漏掉或答偏什么（一句话）"}')
        return "\n".join(lines)

    @staticmethod
    def _intent_parse(raw):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def intent_read_note(self, text, history, name, is_group, conv="", uid=0, quote=None, current_mid=0):
        """命中就返回一段注入提示；没命中/失败返回空串。"""
        if not looks_indirect(text) and not (is_group and 2 <= len(str(text or "").strip()) <= 160):
            return ""
        try:
            if not self.cfg.get("intent_read_enabled", True):
                return ""
        except Exception:
            pass
        try:
            prompt = self._intent_judge_prompt(text, history, name, is_group)
            if uid and hasattr(self, "store"):
                frame = turn_frame(self.store, conv, uid, name, text, quote, self._self_qq(), current_mid)
                prompt += frame_note(frame)
            prompt += ("\n同时给meaning（保留否定和真实原意）、reference（本句谈论对象，不能把@对象自动当主语）、"
                       "uncertain（指代是否仍不唯一的布尔值）。先沿同一人的前句和引用理解，"
                       "再看旁人。没有依据不补恋爱、项目归属或动机。只给JSON，不起草回复。")
            r = self.pool.chat("你是聊天意图判断助手，只核对语义与对象，不安排人格或态度。", [], prompt,
                               max_tokens=480, temperature=0.1, thinking=False,
                               timeout_budget=8, max_attempts=1)
            raw = str(getattr(r, "text", "") or "")
            if not getattr(r, "ok", False) or not raw.strip():
                log("intent", "判断模型没回，本轮不注入")
                return ""
            data = self._intent_parse(raw)
            if not data:
                log("intent", "判断输出解析失败：%s" % raw[:80].replace("\n", " "))
                return ""
        except Exception as e:
            log("intent", "意图读取失败 %r" % (e,))
            return ""

        want = re.sub(r"\s+", " ", str(data.get("want") or "")).strip()[:80]
        cond = re.sub(r"\s+", " ", str(data.get("cond") or "")).strip()[:80]
        tone = re.sub(r"\s+", " ", str(data.get("tone") or "")).strip()[:12]
        trap = re.sub(r"\s+", " ", str(data.get("trap") or "")).strip()[:100]
        meaning = re.sub(r"\s+", " ", str(data.get("meaning") or ""))[:180]
        reference = re.sub(r"\s+", " ", str(data.get("reference") or ""))[:120]
        if not (want or cond or trap or meaning or reference):
            return ""
        lines = ["", "# 这一句到底要什么（先看这个再回；判断供参考，明显不对就别硬套）"]
        if meaning:
            lines.append("- 原意核对：" + meaning)
        if reference:
            lines.append("- 本句所指：" + reference)
        if data.get("uncertain") is True:
            lines.append("- 指代尚未唯一确定，本轮简短问清所指，不以旧话题编出具体细节。")
        if want:
            lines.append("- 对方真正要的是：" + want)
        if cond:
            lines.append("- 附带的条件/筹码：" + cond)
        if tone:
            lines.append("- 语气：" + tone)
        if trap:
            lines.append("- 只按字面回答最容易漏：" + trap)
        lines.append("- 回之前先对一遍：有没有正面接住上面「要的是」；不想答应就明说，别用空头承诺糊弄。")
        try:
            log("intent", "conv=%s want=%s | cond=%s | tone=%s | trap=%s"
                % (conv, want, cond, tone, trap))
            self.store.add_log("intent", "conv=%s want=%s | cond=%s | tone=%s | trap=%s"
                               % (conv, want, cond, tone, trap), True)
        except Exception:
            pass
        return "\n".join(lines)
