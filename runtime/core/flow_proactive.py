# -*- coding: utf-8 -*-
"""主动聊天 / 分享 / 告状。

2026-10-24 重做主动聊天（v0.42.0）：由头 + 看脸色。
- 不再「到点就开口」：先有真实由头（上次没聊完的事、他提过的事、群里说过的话、
  时间场合），没由头就忍住不发；由头从本地库拼，不多调模型。
- 分关系档：最近 24 小时聊过=热（安静 20~40 分钟、一天最多 6 次）；
  一周内聊过=温（2~6 小时、一天最多 2 次）；更久/没聊过=冷（1~3 天、一天最多 1 次，只认强由头）。
- 私信礼貌规则统一在 private_contact：按类型和连续未回退让，尊重晚安/忙碌；
  真实受委屈和已有噩梦可给主人有限例外，所有私信入口共用滚动间隔。
- 不啰嗦：同一条由头 24 小时内不复用；生成后跟最近 3 句比话题重合度，太像就重写
  （治「同一件事换个说法问四遍」）。
- 状态：proactive:await:<conv>=上次主动发送时间；proactive:silent:<conv>=连续没回次数；
  proactive:hooks:<conv>=最近用过的由头；proactive:plan=每轮检查快照，控制台直接看。
- 分享/告状原有证据核对和每日上限保留；等待过久不再强制升级成发送。
"""

import hashlib, json, os, random, re, time
import reply_autonomy
import private_contact

try:
    import fcntl
except Exception:  # Windows/PC 版没有 fcntl，退化成不互斥
    fcntl = None

from conversation_understanding import TURN_RULES, evidence_record, linked_reply_record
from onebot import text_seg
from blacklist import blocked, policy_scope
from safety import allow_outbound
from util import log, log_exc
from common import WEEK, _as_int, _int_list, bot_identity_note, message_targets_bot, parse_other_bots
from proactive_hooks import build_hooks, topic_overlap, HOOK_LABELS, STRONG
from relationship_policy import attitude_context, relation_state
import guard


# ---------- 默认值：代码兜底，线上 config.json 可覆盖 ----------
DEFAULT_MIN_IDLE_SEC = 20 * 60          # 私聊安静 20 分钟就可开口
DEFAULT_MAX_IDLE_SEC = 40 * 60          # 一般人最多 40 分钟；主人固定按 20 分钟
DEFAULT_MAX_PER_DAY = 6                 # 同一人每天最多主动找几次
DEFAULT_SHARE_MAX_PER_DAY = 6           # 分享每天上限
DEFAULT_TATTLE_MAX_PER_DAY = 4          # 告状单独额度，避免一边被分享挤掉、一边刷屏
DEFAULT_SHARE_MIN_GAP_MIN = 180         # 两次分享/告状之间至少隔几分钟（0=不限）
DEFAULT_SHARE_MIN_JUICE = 3             # 料几分以上才值得说（1-5，面板可调）
DEFAULT_PENDING_TTL_H = 24              # 憋着没说超过这么多小时就丢掉
DEFAULT_PENDING_MAX = 5                 # 每个会话最多留几条憋着的

# ---------- 群主动发言（冷场暖话，默认关，面板可开） ----------
DEFAULT_GROUP_SPEAK_QUIET_MIN = 60      # 群里安静多少分钟算冷场
DEFAULT_GROUP_SPEAK_GAP_MIN = 120       # 同一个群两次主动开口至少隔多久（2026-10-25 用户改成 2 小时）
DEFAULT_GROUP_SPEAK_MAX_PER_DAY = 5     # 每个群每天最多主动几次（2026-10-25 用户改成 5）
DEFAULT_QUIET_JITTER_MIN = 120          # 静默结束点每天随机推迟 0~N 分钟，避免整点扎堆
DEFAULT_QUIET_START = 23
DEFAULT_QUIET_END = 7
ESCALATE_ROUNDS = 3                     # 憋了 3 轮（这次是第 4 次撞见）强制说
ESCALATE_SEC = 90 * 60                  # 或憋了 90 分钟
MAX_PROACTIVE_TRIES = 2
BANNED_PROACTIVE = re.compile(r"在吗|在不在|忙吗|忙不忙|有空吗|测试|客服|请问您")
# 群主动发言的语气红线（2026-10-25 用户要求）：不损人、不写死亡/殡葬类比喻
GSPEAK_BAD = re.compile(r"坟|尸|殡|停尸|灵堂|太平间|棺材|躺尸|装死|破群|晦气|上吊|跳楼|割腕|天台|房梁")

# v0.42.0 关系档与看脸色参数
TIER_HOT, TIER_WARM, TIER_COLD = "hot", "warm", "cold"
WARM_MIN_IDLE = 2 * 3600          # 温：安静 2~6 小时
WARM_MAX_IDLE = 6 * 3600
COLD_MIN_IDLE = 24 * 3600         # 冷：1~3 天
COLD_MAX_IDLE = 3 * 24 * 3600
WARM_MAX_PER_DAY = 2              # 温档每天最多 2 次
COLD_MAX_PER_DAY = 1              # 冷档每天最多 1 次
SILENT_MAX = 3                    # 连续 3 次没回就停
SILENT_RETRY_GAP = 3 * 24 * 3600  # 连续 2 次没回后，隔 3 天再试
HOOK_REUSE_SEC = 24 * 3600        # 同一条由头 24 小时内不用第二次
TOPIC_OVERLAP_MAX = 0.45          # 跟最近说过的话太像就重写


def _num(v):
    """内部时间戳/轮次用：兼容 SQLite int 和测试里的 float。"""
    try:
        return int(float(v or 0))
    except Exception:
        return 0


def _daypart(hour):
    h = int(hour or 0)
    if h < 5:
        return "凌晨"
    if h < 9:
        return "早上"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 23:
        return "晚上"
    return "深夜"


class ProactiveMixin(object):
    # ---------- 测试用的时钟缝：真实环境就是 time.*，自测替换成假时钟 ----------
    def _proactive_now(self):
        return time.time()

    def _proactive_localtime(self, ts=None):
        return time.localtime() if ts is None else time.localtime(ts)

    def _proactive_day(self, ts=None):
        return time.strftime("%Y-%m-%d", self._proactive_localtime(ts))

    def _proactive_now_line(self, ts=None):
        t = self._proactive_localtime(ts)
        try:
            from common import _daypart
        except Exception:
            _daypart = lambda h: "晚上"
        return "现在是 %04d-%02d-%02d 周%s %s %02d:%02d（北京时间）" % (
            t.tm_year, t.tm_mon, t.tm_mday, WEEK[t.tm_wday], _daypart(t.tm_hour), t.tm_hour, t.tm_min)

    # ---------- 配置 ----------
    def _cfg_int(self, key, default, lo=None, hi=None):
        try:
            v = self.cfg.get(key)
            v = int(default if v is None or v == "" else v)
        except Exception:
            v = int(default)
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)
        return v

    def _proactive_bounds(self):
        lo = self._cfg_int("proactive_min_idle_sec", DEFAULT_MIN_IDLE_SEC, 60, 6 * 3600)
        hi = self._cfg_int("proactive_max_idle_sec", DEFAULT_MAX_IDLE_SEC, lo, 6 * 3600)
        if hi < lo:
            hi = lo
        return lo, hi

    def _proactive_daily_max(self):
        return self._cfg_int("proactive_max_per_day", DEFAULT_MAX_PER_DAY, 0, 50)

    def _share_daily_max(self, kind):
        base = self._cfg_int("share_max_per_day", DEFAULT_SHARE_MAX_PER_DAY, 0, 50)
        if str(kind or "share") == "tattle":
            # 告状单独一份额度，计数也不和分享共用；没配 tattle_max_per_day 时默认 4 条
            return self._cfg_int("tattle_max_per_day", DEFAULT_TATTLE_MAX_PER_DAY, 0, 50)
        return base

    def _quiet_bounds(self):
        qs = self._cfg_int("proactive_quiet_start", DEFAULT_QUIET_START, 0, 23)
        qe = self._cfg_int("proactive_quiet_end", DEFAULT_QUIET_END, 0, 23)
        return qs, qe

    def _is_quiet_hour(self, ts=None):
        qs, qe = self._quiet_bounds()
        if qs == qe:
            return False
        hour = self._proactive_localtime(ts).tm_hour
        return (qs <= hour < qe) if qs < qe else (hour >= qs or hour < qe)

    def _proactive_quiet_jitter(self, day):
        """每天固定的 0~N 分钟偏移：同一个号同一天是同一个值，第二天换一个。"""
        try:
            seed = "%s|%s|quiet" % (self._self_qq(), day)
            span = self._cfg_int("proactive_quiet_jitter_min", DEFAULT_QUIET_JITTER_MIN, 0, 240)
            return int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) % (span + 1)
        except Exception:
            return 0

    def _is_proactive_quiet(self, ts=None, day=None):
        """主动聊天的静默判断：结束点按「号+当天」随机推迟，避免三个号每天 7:00 齐发。

        qs==qe 仍表示关闭静默；qs>qe（默认 23~7）按跨天算，结束点加 0~120 分钟随机。
        """
        qs, qe = self._quiet_bounds()
        if qs == qe:
            return False
        t = self._proactive_localtime(ts)
        now_min = t.tm_hour * 60 + t.tm_min
        start = qs * 60
        end = qe * 60 + self._proactive_quiet_jitter(day or self._proactive_day(ts))
        if start < end:
            return start <= now_min < end
        return now_min >= start or now_min < end

    # ---------- 主动聊天：候选池 ----------
    def _friend_candidates(self):
        """候选人：主人第一，白名单好友其次；最近有动静的靠前。

        以前只靠 last_active 排序，从没私聊过的人会永远排在后面；现在
        last_active 为空时用 last_seen 兜底，保证「没私聊过但最近说过话」的人也有机会。
        """
        now = self._proactive_now()
        allow = set(_int_list(self.cfg.get("allow_users")))
        owner = _as_int(self.cfg.get("owner_qq"))
        if owner:
            allow.add(owner)
        nick = {}
        try:
            if now - getattr(self, "_friends_at", 0) > 600:
                lst = self.onebot.get_friend_list() or []
                self._friends = [(int(f.get("user_id") or 0),
                                  f.get("nickname") or f.get("remark") or "") for f in lst]
                self._friends_at = now
            nick = {uid: nm for uid, nm in getattr(self, "_friends", []) if uid}
        except Exception:
            nick = {}
        names = {}
        try:
            names = self.store.conv_names() or {}
        except Exception:
            pass
        last_seen = {}
        try:
            # people_overview 会扫全表统计，不必每分钟算一次；10 分钟缓存 + 白名单变化时刷新
            key = tuple(sorted(allow))
            if (now - getattr(self, "_proactive_ls_at", 0) > 600
                    or getattr(self, "_proactive_ls_key", None) != key):
                fresh = {}
                for p in self.store.people_overview(sorted(allow), [], owner) or []:
                    qq = _as_int(p.get("qq"))
                    if qq in allow:
                        fresh[qq] = int(p.get("last_seen") or 0)
                self._proactive_ls = fresh
                self._proactive_ls_at = now
                self._proactive_ls_key = key
            last_seen = dict(getattr(self, "_proactive_ls", {}) or {})
        except Exception:
            last_seen = {}
        out = []
        for uid in allow:
            if blocked(self, uid):
                continue
            conv = "p:%d" % uid
            try:
                sess = self.store.get_session(conv) or {}
            except Exception:
                sess = {}
            last_active = int(sess.get("last_active") or 0)
            ls = int(last_seen.get(uid) or 0)
            # 私聊安静以 last_active 为准；从没私聊过的人用 last_seen 兜底
            activity = last_active or ls
            relation = relation_state(self, uid)
            favor = relation["favor"]
            name = nick.get(uid) or names.get(conv) or ""
            if not name:
                try:
                    name = self.store.person_name(uid) or ""
                except Exception:
                    name = ""
            out.append({
                "uid": uid, "conv": conv, "nick": name or str(uid),
                "last_active": last_active, "last_seen": ls, "activity": activity,
                "favor": favor, "trust": relation["trust"], "is_owner": bool(owner and uid == owner),
            })
        # 主人永远第一；其他人按最近动静（last_active 优先，没有就 last_seen）排
        out.sort(key=lambda x: (0 if x["is_owner"] else 1, -x["activity"], -x["favor"], x["uid"]))
        return out

    def _proactive_tier(self, cand, now=None):
        """关系档：最近 24 小时聊过=热，一周内=温，更久/没聊过=冷。

        「最近聊过」优先按对方自己说话的时间（last_seen）算；他一条都没发过、
        但我们主动发过（有会话）时，用最后活动时间兜底，免得「我们发过一次」
        反而把自己判成冷档、第二天都不敢再试。
        """
        now = int(now or self._proactive_now())
        if cand.get("is_owner"):
            return TIER_HOT
        seen = int(cand.get("last_seen") or 0) or int(cand.get("last_active") or 0)
        if not seen:
            return TIER_COLD
        gap = now - seen
        if gap <= 24 * 3600:
            return TIER_HOT
        if gap <= 7 * 24 * 3600:
            return TIER_WARM
        return TIER_COLD

    def _proactive_daily_cap(self, tier):
        base = self._proactive_daily_max()
        if base <= 0:
            return 0
        if tier == TIER_WARM:
            return min(base, WARM_MAX_PER_DAY)
        if tier == TIER_COLD:
            return min(base, COLD_MAX_PER_DAY)
        return base

    def _proactive_idle_threshold(self, cand, day):
        """这个人今天需要安静多久才轮到主动开口；档位不同门槛不同，同一天同一个值。"""
        if cand.get("is_owner"):
            return self._proactive_bounds()[0]
        tier = cand.get("tier") or TIER_HOT
        if tier == TIER_HOT:
            lo, hi = self._proactive_bounds()
        elif tier == TIER_WARM:
            lo, hi = WARM_MIN_IDLE, WARM_MAX_IDLE
        else:
            lo, hi = COLD_MIN_IDLE, COLD_MAX_IDLE
        if hi <= lo:
            return lo
        seed = "%s|%s|%s" % (cand.get("uid"), day, tier)
        h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16)
        return lo + (h % (hi - lo + 1))

    @staticmethod
    def _proactive_idle_desc(idle_min):
        m = max(1, int(idle_min or 1))
        if m >= 1440:
            return "%d 天" % (m // 1440)
        if m >= 60:
            return "%d 小时" % (m // 60)
        return "%d 分钟" % m

    def _proactive_count(self, conv, day):
        try:
            return max(0, _as_int(self.store.get_setting("proactive:count:%s:%s" % (conv, day))))
        except Exception:
            return 0

    # ---------- 主动聊天：发出后的状态（v0.42.0） ----------
    def _proactive_await_key(self, conv):
        return "proactive:await:%s" % conv

    def _proactive_silent_key(self, conv):
        return "proactive:silent:%s" % conv

    def _proactive_hooks_key(self, conv):
        return "proactive:hooks:%s" % conv

    def _proactive_get_int(self, key):
        try:
            return _as_int(self.store.get_setting(key))
        except Exception:
            return 0

    def _proactive_set(self, key, value):
        try:
            self.store.set_setting(key, str(value))
        except Exception:
            pass

    def _proactive_await_ts(self, conv):
        return self._proactive_get_int(self._proactive_await_key(conv))

    def _proactive_silent(self, conv):
        return max(0, self._proactive_get_int(self._proactive_silent_key(conv)))

    def _proactive_clear_waiting(self, conv):
        """对方理他了（发来任意消息）：解除等待，并把「连续没回」清零。"""
        try:
            cleared = False
            if self._proactive_await_ts(conv):
                self._proactive_set(self._proactive_await_key(conv), 0)
                cleared = True
            if self._proactive_silent(conv):
                self._proactive_set(self._proactive_silent_key(conv), 0)
                cleared = True
            if cleared:
                log("proactive", "对方回话了，解除等待 conv=%s" % conv)
        except Exception:
            pass

    def _proactive_used_hooks(self, conv, now=None):
        """最近用过的由头（30 天内，最多 12 条）。"""
        now = int(now or self._proactive_now())
        try:
            raw = self.store.get_setting(self._proactive_hooks_key(conv)) or "[]"
            rows = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            rows = []
        out = []
        for x in rows or []:
            if not isinstance(x, dict):
                continue
            k = str(x.get("k") or "")
            ts = _num(x.get("ts"))
            if k and now - ts < 30 * 24 * 3600:
                out.append({"k": k, "ts": ts})
        return out[-12:]

    def _proactive_note_hook(self, conv, hook, now=None):
        now = int(now or self._proactive_now())
        key = str((hook or {}).get("key") or "")
        if not key:
            return
        rows = [x for x in self._proactive_used_hooks(conv, now) if x["k"] != key]
        rows.append({"k": key, "ts": now})
        try:
            self.store.set_setting(self._proactive_hooks_key(conv),
                                   json.dumps(rows[-12:], ensure_ascii=False))
        except Exception:
            pass

    def _proactive_hooks(self, cand, day):
        """这个人的候选由头（已排除 24 小时内用过的）。"""
        now = int(self._proactive_now())
        t = self._proactive_localtime(now)
        used = set(x["k"] for x in self._proactive_used_hooks(cand["conv"], now)
                   if now - x["ts"] < HOOK_REUSE_SEC)
        try:
            return build_hooks(self.store, cand, now, day, t.tm_hour, t.tm_wday,
                               allow_groups=_int_list(self.cfg.get("allow_groups")),
                               used_keys=used,
                               group_hooks=bool(self.cfg.get("proactive_private_from_group")))
        except Exception as e:
            log("proactive", "由头生成失败 %r" % (e,))
            return []

    def _proactive_plan(self, items):
        """把每轮检查的结论写进 settings.proactive:plan，控制台直接看。

        内容没变就不写库（每 10 分钟刷新一次时间戳），别每分钟往 SQLite 写 3KB。
        """
        try:
            now = int(self._proactive_now())
            key = json.dumps(items or [], ensure_ascii=False, sort_keys=True)
            if (key == getattr(self, "_proactive_plan_key", "")
                    and now - int(getattr(self, "_proactive_plan_at", 0)) < 600):
                return
            self._proactive_plan_key = key
            self._proactive_plan_at = now
            self.store.set_setting("proactive:plan",
                                   json.dumps({"ts": now, "items": items or []}, ensure_ascii=False))
        except Exception:
            pass

    def _proactive_ready(self, cand, day):
        """返回 (是否到点, 原因, 由头)。「还没安静够」不写日志，避免每分钟刷屏。"""
        now = int(self._proactive_now())
        tier = self._proactive_tier(cand, now)
        cand["tier"] = tier
        if self._is_proactive_quiet(now, day):
            return False, "静默时段", None
        cap = self._proactive_daily_cap(tier)
        count = self._proactive_count(cand["conv"], day)
        if cap <= 0 or count >= cap:
            return False, "今天已经找了 %d/%d 次" % (count, cap), None
        silent = self._proactive_silent(cand["conv"])
        activity = int(cand.get("activity") or 0)
        if activity and now - activity < self._proactive_idle_threshold(cand, day):
            return False, "还没安静够", None
        hooks = self._proactive_hooks(cand, day)
        allow = {"strong", "medium", "weak"}
        if tier == TIER_WARM:
            allow = {"strong", "medium"}
        elif tier == TIER_COLD or silent >= 2:
            allow = {"strong"}
        hooks = [h for h in hooks if str(h.get("strength")) in allow]
        if not hooks:
            return False, "没有由头，先不发", None
        reason = ""
        for hook in hooks:
            kind = private_contact.classify_hook(hook, now)
            with private_contact.attempt(self, cand["conv"], kind=kind,
                                         event_key=hook.get("key"), material=hook.get("material")) as contact:
                if contact.allowed:
                    return True, "", hook
                reason = contact.reason
        return False, reason, None

    def _proactive_note(self, cand, day, reason):
        """同一个理由半小时内只记一次，避免每分钟写一行日志。"""
        try:
            noted = getattr(self, "_proactive_noted", None)
            if not isinstance(noted, dict):
                noted = {}
                self._proactive_noted = noted
            key = "%s|%s" % (cand.get("conv"), reason)
            # 上限/连续没回这类长期状态 6 小时记一次；其它半小时一次
            if (reason.startswith("今天已经找了") or reason.startswith("连续")
                    or reason.startswith("没回")):
                window = 6 * 3600
            elif reason == "没有由头，先不发":
                window = 3600
            else:
                window = 1800
            bucket = int(self._proactive_now() // window)
            if noted.get(key) == bucket:
                return
            noted[key] = bucket
        except Exception:
            pass
        msg = "跳过 %s(%s)：%s" % (cand.get("nick"), cand.get("uid"), reason)
        log("proactive", msg)
        try:
            self.store.add_log("proactive", msg, False)
        except Exception:
            pass

    # ---------- 主动聊天：生成与发送 ----------
    @staticmethod
    def _proactive_clean_text(text):
        s = str(text or "").strip()
        s = re.sub(r'^[\s"\'“”‘’]+|[\s"\'“”‘’]+$', "", s).strip()
        s = re.sub(r"^(蓝色大肥鱼|大肥鱼|我)\s*[:：]\s*", "", s)
        s = re.sub(r"\[(?:脸|表情)[:：][^\]]*\]", "", s)  # 主动消息走纯文本段，别把标记原样发出去
        s = " ".join(str(s).split())
        return s[:160]

    def _proactive_recent_texts(self, conv):
        try:
            raw = self.store.get_setting("proactive:texts:%s" % conv) or "[]"
            lst = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(lst, list):
                return [str(x) for x in lst if str(x or "").strip()][:12]
        except Exception:
            pass
        return []

    def _proactive_text_seen(self, conv, text):
        n = self._share_dedupe(text)
        if not n:
            return False
        return any(self._share_dedupe(x) == n for x in self._proactive_recent_texts(conv))

    def _proactive_remember_text(self, conv, text):
        try:
            n = self._share_dedupe(text)
            lst = [x for x in self._proactive_recent_texts(conv) if self._share_dedupe(x) != n]
            lst.append(str(text or "")[:120])
            self.store.set_setting("proactive:texts:%s" % conv,
                                   json.dumps(lst[-8:], ensure_ascii=False))
        except Exception:
            pass

    def _proactive_history(self, conv, limit=8):
        try:
            rows = self.store.messages_for_context(conv, limit) or []
        except Exception:
            rows = []
        lines = []
        for r in rows[-limit:]:
            text = " ".join(str(r.get("content") or "").split())
            if not text:
                continue
            who = "他" if r.get("role") == "user" else "你"
            lines.append("%s：%s" % (who, text[:120]))
        return "\n".join(lines)[-1000:]

    def _proactive_own_life(self, is_owner=False):
        """最近写过的日记只当背景：让它开口时有自己的事，不是干巴巴问一句。

        v0.63.0 修：日记从 v0.53.0 起统一存在 "__day__"（一天一篇），旧代码按「跟这个人的
        对话框号」去找，永远对不上——从 v0.53.0 起「说说自己最近的事」一直是空的。现在按天取最近一篇。

        v0.63.3 再改：**只对主人用**。日记是「我的一天」，里面会写到跟别人的私事；
        原样给群友或别的私聊当背景，等于把别人（包括主人）的私事漏出去。
        """
        if not is_owner:
            return ""
        try:
            if getattr(self.store, 'continuity_active', False):
                uid = int(self.cfg.get('owner_qq') or 0)
                return self.read_diary_tool({}, 'p:%d' % uid)[:650] if uid else ''
            for d in self.store.list_diaries(30) or []:
                if not d.get("text"):
                    continue
                return "你最近写的日记（只作背景，别念出来）：%s" % str(d.get("text"))[:200]
        except Exception:
            pass
        return ""

    def _proactive_open_prompt(self, cand, idle_min, hook=None):
        now = self._proactive_localtime()
        nick = " ".join(str(cand.get("nick") or "对方").split())[:20]
        idle_min = max(1, int(idle_min or 1))
        if idle_min >= 24 * 60:
            since = "你们上次私聊是 %d 天前" % (idle_min // (24 * 60))
        elif idle_min >= 60:
            since = "你们上次私聊是 %d 小时 %d 分钟前" % (idle_min // 60, idle_min % 60)
        else:
            since = "你们上次私聊是 %d 分钟前" % idle_min
        history = self._proactive_history(cand["conv"])
        own = self._proactive_own_life(bool(cand.get("is_owner")))
        tier = cand.get("tier") or TIER_HOT
        lines = [
            "现在是%s（%s），%s。" % (self._proactive_now_line(), _daypart(now.tm_hour), since),
            "你正在考虑是否主动给「%s」发一句；这不是回复他，也不是必须开口。" % nick,
        ]
        if hook:
            lines += [
                "",
                "这次开口的由头（真实发生过，别编、别原样复述）：",
                "- %s" % hook.get("material"),
                "- %s" % (hook.get("hint") or "接着这件事说。"),
            ]
        lines += [
            "",
            "上次聊到（越靠下越新；别照抄、别逐条复述）：",
            history or "（你们还没怎么聊过，这是你第一次主动找他）",
        ]
        if own:
            lines.append(own)
        tone = {
            TIER_HOT: "- 你们最近常聊，语气可以随意、带点玩笑。",
            TIER_WARM: "- 有一阵没聊了，语气自然一点，别像查岗。",
            TIER_COLD: "- 很久没聊了，别太热络，也别一上来问一堆。",
        }.get(tier)
        lines += ["", "怎么开口（像真人，不是客服）："]
        if tone and not (getattr(getattr(self, "persona", None), "unified_self", False) or self.cfg.get("self_growth_enabled", True)):
            lines.append(tone)
        lines += [
            "- 从上面「由头」这件事接着说；问后续、接一个梗、说你的看法都行。",
            "- 也可以说自己此刻的想法；只有真实记录支持才提刚听过的歌、看过的东西或做过的事，不编造活动。",
            "- 不要用「在吗」「忙吗」「在不在」「测试」开头，不要客服腔，不要一次问一堆。",
            "- 20~45 个字，最多一个问号，不要 Markdown，不要引号。",
            "- 别编造你们没发生过的大事，别重复你最近发过的话，别追着同一件事反复问。",
            "- 他没接前面的私信时，别问为什么不回、别索要安慰或回应；有具体关心就说一句，留给他自己的空间。",
            "- 晚安、去睡了、先忙是收尾；不要换个无关小事再问，不因沉默而编造受委屈。",
        ]
        try:
            # v0.65.4：已经说好的提醒别当话头，更别催（2026-10-28 07:30 就这么催过一次）
            _rn = self._reminder_quiet_note(cand.get("conv"))
            if _rn:
                lines += ["", "另外记住：", _rn]
        except Exception as e:
            log("proactive", "提醒备注注入失败 %r" % (e,))
        lines += ["", "先结合你对这个人的亲近意愿和戒心判断是否想主动联系；最近常聊或认识很久不代表关系亲近。",
                  "不想主动找他、关系疏远或由头不合适时，可以输出空字符串，结束本次闲聊开场。",
                  "愿意联系才只输出这一句话；此判断不取消已承诺的提醒，也不拒绝对方当前的认真求助。"]
        return "\n".join(lines)

    def _proactive_personal_context(self, system, conv, uid, text="", is_owner=False, with_ids=False):
        """The same self in an authorized destination; only locally visible growth is used."""
        provided = []
        if (getattr(getattr(self, "persona", None), "unified_self", False) or self.cfg.get("self_growth_enabled", True)) and hasattr(self.store, "growth_context"):
            try:
                block, provided = self.store.growth_context(conv, uid, text, with_ids=True)
                system += block
            except Exception as exc:
                log("growth", "主动聊天成长背景暂不可用 %s" % type(exc).__name__)
        persona = getattr(self, "persona", None)
        if hasattr(persona, "relation_context"):
            system += persona.relation_context(is_owner)
        if int(uid or 0) > 0:
            system += "\n\n" + attitude_context(self, uid)
        system += TURN_RULES
        system += "\n主动联系也核对素材里的人物与项目归属。不能把同一个人的研究、感情或旧计划拼成新事件；未经原话支持不补动机。"
        return (system, provided) if with_ids else system

    def _proactive_note_growth_context(self, conv, outbound_id, provided):
        if hasattr(self.store, "growth_note_provided"):
            try:
                self.store.growth_note_provided(conv, outbound_id, provided)
            except Exception as exc:
                log("growth", "主动回复背景关联未记录 %s" % type(exc).__name__)

    def _proactive_generate(self, cand, idle_min, hook=None):
        cand.pop("_proactive_declined", None)
        try:
            system = self.persona.system_prompt(
                cand["is_owner"], cand.get("nick") or "对方", False,
                now_line=self._proactive_now_line())
        except Exception:
            system = "你是 QQ 机器人「%s」。" % (self.cfg.get("bot_name") or "机器人")
        system, cand["_growth_provided_ids"] = self._proactive_personal_context(
            system, cand["conv"], cand["uid"], (hook or {}).get("material") or "", cand["is_owner"], with_ids=True)
        prompt = self._proactive_open_prompt(cand, idle_min, hook)
        recent = self._proactive_recent_texts(cand["conv"])[-3:]
        if recent:
            prompt += ("\n\n你最近主动找他时说过（这次别再聊同一件事、别换个说法重复）：\n"
                       + "\n".join("- 「%s」" % x[:60] for x in recent))
        for _ in range(MAX_PROACTIVE_TRIES):
            try:
                r = self.pool.chat(system, [], prompt, max_tokens=120,
                                   temperature=0.9, thinking=False)
            except Exception as e:
                log("proactive", "生成失败 %r" % (e,))
                return ""
            raw = str(getattr(r, "text", "") or "")
            success = bool(getattr(r, "ok", False))
            text = self._proactive_clean_text(raw if success else "")
            if not text:
                # Only an originally empty successful output expresses abstention.
                # Cleaning a nonempty label/face marker to nothing is a format failure.
                cand["_proactive_declined"] = success and not raw.strip().strip('"\'“”‘’').strip()
                return ""
            if len(text) < 4:
                prompt += "\n\n刚才那句太短了，像敷衍；说完整一点（20~45 个字）。"
                continue
            if BANNED_PROACTIVE.search(text):
                prompt += ("\n\n刚才那句「%s」太像开场白/客服，换一句具体的，"
                           "别用「在吗/忙吗/测试」这类词。" % text[:60])
                continue
            if self._proactive_text_seen(cand["conv"], text):
                prompt += "\n\n刚才那句「%s」你最近发过了，换一句新的。" % text[:60]
                continue
            dup = 0.0
            for old in recent:
                dup = max(dup, topic_overlap(text, old))
            if dup >= TOPIC_OVERLAP_MAX:
                prompt += ("\n\n刚才那句「%s」跟最近聊过的内容太像了；换由头里的另一件事，"
                           "或者把这件事说到新方向，别重复问同一件事。" % text[:40])
                continue
            return text
        return ""

    def _proactive_sticker(self, cand, text):
        """主动开口也走正常的选图/视觉复核；没有本轮来话，不伪造对方消息。"""
        if not self.cfg.get("sticker_judge_enabled", True) or not getattr(self, "stickers", None):
            return None
        try:
            return self._judge_sticker(cand["conv"], False, 0, cand["uid"], "", text,
                                       cand.get("nick") or "", bool(cand.get("is_owner")))
        except Exception as e:
            log("proactive", "配图判断失败 %r" % (e,))
            return None

    @policy_scope
    def _proactive_send(self, cand, day, idle_min, hook=None):
        if reply_autonomy.resting(self):
            return {"ok": False, "reason": "它想安静一会儿"}
        conv, uid = cand["conv"], cand["uid"]
        hook = hook or {}
        contact = private_contact.attempt(self, conv,
                    kind=private_contact.classify_hook(hook, int(self._proactive_now())),
                    event_key=hook.get("key") or ("open:" + day), material=hook.get("material"))
        if not contact.check():
            return {"ok": False, "reason": contact.reason}
        text = self._proactive_generate(cand, idle_min, hook)
        if not text:
            if cand.get("_proactive_declined"):
                return {"ok": False, "reason": "本轮不想主动联系"}
            msg = "生成失败 %s(%s)" % (cand.get("nick"), uid)
            log("proactive", msg)
            try:
                self.store.add_log("proactive", msg, False)
            except Exception:
                pass
            return {"ok": False, "reason": "生成失败"}
        ok, cleaned = allow_outbound(text, self.cfg.get("api_key"), keys=self._outbound_keys())
        if not ok:
            msg = "出站拦截 %s(%s)" % (cand.get("nick"), uid)
            log("proactive", msg)
            try:
                self.store.add_log("proactive", msg, False)
            except Exception:
                pass
            return {"ok": False, "reason": "出站拦截"}
        try:
            cleaned = self._guard_owner_address(cleaned, cand.get("is_owner"), cand.get("nick") or "", conv)
        except Exception:
            pass
        wait_ready = bool(self.cfg.get("sticker_wait_ready", True))
        sticker = self._proactive_sticker(cand, cleaned) if wait_ready else None
        if reply_autonomy.resting(self):
            return {"ok": False, "reason": "它想安静一会儿"}
        try:
            # 主动找人也是模型写的正文：里面的 [脸:名字] 要切成 QQ 表情段，不能原样发出去
            segs = self._text_segs(cleaned)
            with contact:
                if not segs or not contact.sending():
                    return {"ok": False, "reason": contact.reason or "没有正文"}
                sent = self._send(False, 0, uid, segs)
                if sent:
                    contact.sent()
        except Exception as e:
            log("proactive", "发送异常 %r" % (e,))
            sent = False
        if not sent:
            msg = "发送失败 %s(%s)" % (cand.get("nick"), uid)
            log("proactive", msg)
            try:
                self.store.add_log("proactive", msg, False)
            except Exception:
                pass
            return {"ok": False, "reason": "发送失败"}
        now = int(self._proactive_now())
        count = self._proactive_count(conv, day) + 1
        silent = self._proactive_silent(conv) + 1
        try:
            self.store.set_setting("proactive:count:%s:%s" % (conv, day), str(count))
            self.store.set_setting("proactive:last:%s" % conv, str(now))
            self.store.set_setting(self._proactive_await_key(conv), str(now))
            self.store.set_setting(self._proactive_silent_key(conv), str(silent))
        except Exception:
            pass
        self._proactive_note_hook(conv, hook, now)
        self._proactive_remember_text(conv, cleaned)
        sticker_sent = False
        try:
            if not wait_ready:
                sticker = self._proactive_sticker(cand, cleaned)
            if sticker and contact.check():
                gap = max(0.0, float(self.cfg.get("sticker_send_gap_sec") or 0))
                if gap:
                    time.sleep(gap)
                sticker_sent = bool(self.send_sticker_item(conv, False, 0, uid, sticker))
        except Exception as e:
            # 正文已经成功，配图失败不触发重发，也不写虚假的图片附件。
            log("proactive", "配图发送失败 %r" % (e,))
        atts = ([{"type": "sticker", "file": sticker.get("file"),
                  "note": (sticker.get("desc") or sticker.get("note") or "")[:120]}]
                if sticker_sent else [])
        try:
            outbound_id = self.store.add_message(conv, self.self_id, self.cfg.get("bot_name"), "out", cleaned, "", 0, atts)
            self._proactive_note_growth_context(conv, outbound_id, cand.get("_growth_provided_ids") or [])
            self.store.touch_session(conv, now)
        except Exception as e:
            log("proactive", "写历史失败 %r" % (e,))
        hook_label = HOOK_LABELS.get((hook or {}).get("type") or "", "无")
        msg = "主动找 %s(%s) 由头=%s 说：%s（安静 %s，今天 %d/%d，连续没回 %d）" % (
            cand.get("nick"), uid, hook_label, cleaned[:60],
            self._proactive_idle_desc(idle_min), count, self._proactive_daily_cap(cand.get("tier") or TIER_HOT), silent)
        log("proactive", msg)
        try:
            self.store.add_log("proactive", msg)
        except Exception:
            pass
        self._last_ok_at = now
        try:
            self._proactive_mark_global(now)
        except Exception:
            pass
        return {"ok": True, "uid": uid, "text": cleaned, "count": count, "sticker": sticker_sent}

    def _proactive_tick(self):
        """跑一轮检查：最多发一条，返回结果给自测用；顺手写「现在为什么发/不发」快照。"""
        if self.cfg.get("bot_paused"):
            return {"ok": False, "reason": "暂停使用"}
        if reply_autonomy.resting(self):
            self._proactive_plan([])
            return {"ok": False, "reason": "它想安静一会儿"}
        if not self.cfg.get("proactive_enabled"):
            self._proactive_plan([])
            return {"ok": False, "reason": "主动聊天关着"}
        if not getattr(self, "self_id", 0):
            self._proactive_plan([])
            return {"ok": False, "reason": "还没登录"}
        if not self._proactive_global_allowed():
            self._proactive_plan([])
            return {"ok": False, "reason": "主动消息刚发过，间隔不够"}
        day = self._proactive_day()
        now = int(self._proactive_now())
        result = {"ok": False, "reason": "没有到点的人"}
        block_reason = ""
        plan = []
        for cand in self._friend_candidates()[:8]:
            ready, reason, hook = self._proactive_ready(cand, day)
            tier = cand.get("tier") or self._proactive_tier(cand, now)
            item = {
                "uid": cand.get("uid"), "nick": cand.get("nick"), "tier": tier,
                "ready": bool(ready), "reason": reason or "",
                "hook": ("%s：%s" % (HOOK_LABELS.get(hook.get("type"), "由头"),
                                     str(hook.get("material") or "")[:40])) if hook else "",
                "silent": self._proactive_silent(cand["conv"]),
                "count": self._proactive_count(cand["conv"], day),
                "cap": self._proactive_daily_cap(tier),
            }
            plan.append(item)
            if not ready:
                if reason not in ("还没安静够", "静默时段"):
                    self._proactive_note(cand, day, reason)
                    msg = "%s：%s" % (cand.get("nick"), reason)
                    if not block_reason or (block_reason.startswith("主人") and not cand.get("is_owner")
                                            and not str(cand.get("nick") or "").startswith("主人")):
                        block_reason = msg
                continue
            idle_min = max(1, int((now - int(cand.get("activity") or now)) / 60))
            result = self._proactive_send(cand, day, idle_min, hook)
            if result.get("ok"):
                item["count"] = int(result.get("count") or item["count"])
                item["silent"] = self._proactive_silent(cand["conv"])
                item["reason"] = "刚主动开口"
                item["hook"] = "刚用了：%s" % HOOK_LABELS.get((hook or {}).get("type"), "由头")
            break
        if not result.get("ok") and block_reason:
            result["reason"] = block_reason
        self._proactive_plan(plan)
        return result

    def _proactive_loop(self):
        while True:
            try:
                self._proactive_tick()
            except Exception as e:
                log_exc("proactive", e)
            time.sleep(60)

    # ---------- 群主动发言：冷场暖话（默认关，面板里开了才会说） ----------
    def _group_speak_candidates(self):
        """可以自己开口的群：最近 7 天有动静的群；配了 allow_groups 就只在这些群里说。"""
        try:
            briefs = self.store.recent_conv_briefs(40) or []
        except Exception:
            return []
        allow = set(_int_list(self.cfg.get("allow_groups")))
        now = int(self._proactive_now())
        out = []
        for b in briefs:
            conv = str(b.get("conv") or "")
            if not conv.startswith("g:"):
                continue
            gid = _num(conv[2:])
            if gid <= 0:
                continue
            if allow and gid not in allow:
                continue
            if now - int(b.get("last_ts") or 0) > 7 * 86400:
                continue
            out.append({"conv": conv, "gid": gid, "name": b.get("name") or "",
                        "last_ts": int(b.get("last_ts") or 0)})
        return out

    def _group_speak_bot_ids(self):
        """机器人编号集合（2026-10-26 用户要求按编号认，不按名字——名字会变）。

        协议版的 sender_id 就是 QQ 号：自己 + bot_peers_qq + other_bots 里配的号。
        """
        ids = set()
        try:
            me = _as_int(self._self_qq())
            if me:
                ids.add(me)
            for q in str(self.cfg.get("bot_peers_qq") or "").replace("，", ",").split(","):
                q = _as_int(q.strip())
                if q:
                    ids.add(q)
            for q in parse_other_bots(self.cfg.get("other_bots") or ""):
                ids.add(int(q))
        except Exception:
            pass
        return ids

    def _group_speak_last_from_bot(self, conv):
        """这个群最后一条消息是不是机器人（含自己）说的（2026-10-26 用户规则）。

        群里最后一个开口的是机器人、之后没人接话，别的机器人就不许再主动开口，
        直到有真人说话。按 sender_id（QQ 号）认，不按名字（名字会变）。
        """
        try:
            lm = self.store.last_message(conv) or {}
        except Exception:
            return False
        if not lm:
            return False
        if str(lm.get("direction") or "") == "out":
            return True
        sid = _as_int(lm.get("sender_id"))
        return bool(sid and sid in self._group_speak_bot_ids())

    def _group_speak_quiet_bounds(self):
        qs = self._cfg_int("group_speak_quiet_start", 23, 0, 23)
        qe = self._cfg_int("group_speak_quiet_end", 7, 0, 23)
        return qs, qe

    def _is_group_speak_quiet(self, ts=None):
        """群主动发言自己的安静时段（跟私聊那套分开；起止相同=不静默）。"""
        qs, qe = self._group_speak_quiet_bounds()
        if qs == qe:
            return False
        hour = self._proactive_localtime(ts).tm_hour
        return (qs <= hour < qe) if qs < qe else (hour >= qs or hour < qe)

    # ---------- 每群独立选举（2026-10-25 用户拍板） ----------
    # 三只看同一个群的编号不一样（缇宝 200001 / 哦鲸鲸 200101 / 大肥鱼是真实群号），
    # 所以用「这个群最后一句消息」算签名对齐；每个群一把锁，每只自己随机等一会儿再抢，
    # 抢到的号在这个群开口、别的号这一轮跳过。抢到却发不出去（这个群没给主动发言权限）
    # 就把机会还回去，并让这只 24 小时内别在这个群再试。
    GSPEAK_BLOCK_TTL = 24 * 3600

    def _group_speak_sig(self, conv):
        """同一个群在不同号的库里编号不同：用「沉默开始前最后一句」算签名。

        v0.56.0 修：以前用「最后一条消息」，机器人一开口签名就变，锁等于没有；
        改成只取 30 分钟以前的那条（也就是让这个群安静下来的那句），三只算出来一致。
        """
        now = int(time.time())
        try:
            lm = self.store.last_message_before(conv, now - 1800) or {}
            if not lm:
                lm = self.store.last_message(conv) or {}
        except Exception:
            lm = {}
        text = re.sub(r"\\s+", "", str(lm.get("text") or ""))
        if not text:
            text = "media:%d" % (int(lm.get("ts") or 0) // 60)
        return hashlib.md5(text[:120].encode("utf-8", "ignore")).hexdigest()[:16]

    @staticmethod
    def _gate_dir():
        """三只共用的锁目录：sidecar 的 /tmp 是隔离的，不能再用 /tmp。"""
        for d in (os.environ.get("DAFEIYU_GATE_DIR") or "", "/var/lib/dafeiyu/gate", "/tmp"):
            if not d:
                continue
            try:
                os.makedirs(d, mode=0o775, exist_ok=True)
                probe = os.path.join(d, ".probe-%d" % os.getpid())
                with open(probe, "a"):
                    pass
                os.remove(probe)
                return d
            except Exception:
                continue
        return "/tmp"

    def _group_speak_state_path(self, conv):
        return os.path.join(self._gate_dir(), "dafeiyu-gspeak-%s.lock" % self._group_speak_sig(conv))

    @staticmethod
    def _read_lock_state(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 8192).decode("utf-8", "ignore").strip()
            st = json.loads(raw) if raw else {}
            return st if isinstance(st, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_lock_state(fd, st):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(st, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        except Exception:
            pass

    def _group_speak_me(self):
        return str(self.cfg.get("bot_name") or self._self_qq())

    def _group_speak_claim(self, conv, gap_min):
        """每群独立随机选举：'ok' 这只在这个群说 / 'taken' 这个群刚有人说了 / 'blocked' 这只在这个群发不出去。"""
        if fcntl is None:
            return "ok"
        path = self._group_speak_state_path(conv)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        except Exception:
            return "ok"
        now = int(time.time())
        me = self._group_speak_me()
        gap = max(600, int(gap_min) * 60)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = self._read_lock_state(fd)
            blocked = st.get("blocked") if isinstance(st.get("blocked"), dict) else {}
            blocked = {k: int(v or 0) for k, v in blocked.items() if now - int(v or 0) < self.GSPEAK_BLOCK_TTL}
            if blocked.get(me):
                return "blocked"
            last = int(st.get("ts") or 0)
            if last and now - last < gap:
                return "taken"
            st["ts"] = now
            st["bot"] = me
            st["blocked"] = blocked
            self._write_lock_state(fd, st)
            return "ok"
        except Exception:
            return "ok"
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass

    def _group_speak_release(self, conv, block=False):
        """没发出去：把这次机会还给别的号；block=True 时让这只 24 小时内别在这个群再试。"""
        if fcntl is None:
            return
        path = self._group_speak_state_path(conv)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        except Exception:
            return
        now = int(time.time())
        me = self._group_speak_me()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = self._read_lock_state(fd)
            if st.get("bot") == me:
                st["ts"] = 0
            if block:
                blocked = st.get("blocked") if isinstance(st.get("blocked"), dict) else {}
                blocked[me] = now
                st["blocked"] = blocked
            self._write_lock_state(fd, st)
        except Exception:
            pass
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass

    def _group_speak_stagger(self):
        """每只号固定错开 0~9 分钟，避免三只同时判定冷场、同一分钟各发一条。"""
        try:
            return int(hashlib.md5(("%s|gspeak" % self._self_qq()).encode("utf-8")).hexdigest()[:4], 16) % 10
        except Exception:
            return 0

    def _gspeak_recent(self, conv):
        try:
            arr = json.loads(self.store.get_setting("gspeak:recent:%s" % conv) or "[]")
            return [str(x)[:40] for x in arr if str(x).strip()][-5:]
        except Exception:
            return []

    def _gspeak_remember(self, conv, text):
        try:
            arr = self._gspeak_recent(conv)
            arr.append(str(text or "").strip()[:40])
            self.store.set_setting("gspeak:recent:%s" % conv,
                                   json.dumps(arr[-5:], ensure_ascii=False))
        except Exception:
            pass

    def _group_speak_prompt(self, cand, idle_min):
        try:
            system = self.persona.system_prompt(False, "", True,
                                                now_line=self._proactive_now_line())
        except Exception:
            system = "你在群里聊天，说话自然、口语化、不装客服。"
        gname = cand.get("name") or "这个群"
        system, cand["_growth_provided_ids"] = self._proactive_personal_context(
            system, cand.get("conv") or "", 0, gname, with_ids=True)
        recent = self._gspeak_recent(cand.get("conv") or "")
        lines = [
            self._proactive_now_line() + "；按这个时间说话，别把早上说成中午、把周一说成周末。",
            "你现在在群里（%s）。" % gname,
            "这个群已经 %d 分钟没人说话了，有点冷场。" % max(1, idle_min),
            "你随口抛一句轻松的话题，像群里的人随口聊天。可以从这些方向挑一个：吃的/困不困/天气/群里以前聊过的事/自己随口一句。",
            "不要问“在吗/有人在吗”，不要提自己是机器人，不要 @ 任何人，不要连环追问。",
            "语气收着点：不许损人、不许嘲讽群友，不许用死亡/殡葬一类比喻（坟、尸、停尸房、躺尸这种）。",
            "一两条短句，总共不超过 30 字，口语化。只输出要发的那句话。",
        ]
        if (getattr(getattr(self, "persona", None), "unified_self", False) or self.cfg.get("self_growth_enabled", True)):
            lines[5] = "按眼前场合和自己的想法选择口气，不为证明性格而刻意抬杠，不编没发生过的事。"
        if recent:
            lines.insert(4, "你最近在这个群说过：%s —— 换个方向，别重复。" % " / ".join(recent))
        return system, "\n".join(lines)

    def _group_speak_generate(self, cand, idle_min):
        for _try in range(3):
            system, prompt = self._group_speak_prompt(cand, idle_min)
            r = self.pool.chat(system, [], prompt, max_tokens=80, temperature=0.9, thinking=False)
            if not getattr(r, "ok", False):
                return ""
            text = " ".join(str(getattr(r, "text", "") or "").split()).strip().strip('"“”「」')
            if len(text) < 2 or BANNED_PROACTIVE.search(text) or GSPEAK_BAD.search(text):
                continue
            if guard.check(text)["action"] != "ok" or guard.time_issue(text):
                continue
            return text[:60]
        return ""

    def _proactive_global_gap_sec(self):
        try:
            return max(0, int(self.cfg.get("proactive_global_gap_min") or 10)) * 60
        except Exception:
            return 600

    def _proactive_global_allowed(self, now=None):
        """同一只的任意主动消息之间至少隔 N 分钟；群冷场开场白另加 30 分钟冷却。"""
        now = int(now or self._proactive_now())
        gap = self._proactive_global_gap_sec()
        try:
            last = int(self.store.get_setting("proactive:last_any") or 0)
        except Exception:
            last = 0
        if gap > 0 and last and now - last < gap:
            return False
        try:
            cd = int(self.store.get_setting("gspeak:cooldown") or 0)
        except Exception:
            cd = 0
        if cd and now < cd:
            return False
        return True

    def _proactive_mark_global(self, now=None):
        try:
            self.store.set_setting("proactive:last_any", str(int(now or self._proactive_now())))
        except Exception:
            pass

    def _gspeak_any_open_path(self):
        return os.path.join(self._gate_dir(), "dafeiyu-gspeak-any.lock")

    def _gspeak_any_recent(self, window=1800):
        """三只共用一个“刚有人开过口”的标记：不够对齐的群也能挡住同时开麦。"""
        try:
            return (time.time() - os.path.getmtime(self._gspeak_any_open_path())) < int(window)
        except Exception:
            return False

    def _mark_any_open(self):
        try:
            p = self._gspeak_any_open_path()
            with open(p, "a"):
                os.utime(p, None)
        except Exception:
            pass

    @policy_scope
    def _group_speak_tick(self):
        """跑一轮冷场检查：最多在一个群开口一次。"""
        if self.cfg.get("bot_paused"):
            return {"ok": False, "reason": "暂停使用"}
        if reply_autonomy.resting(self):
            return {"ok": False, "reason": "它想安静一会儿"}
        if not self.cfg.get("group_speak_enabled"):
            return {"ok": False, "reason": "群主动发言关着"}
        if not self.cfg.get("group_speak_idle", True):
            return {"ok": False, "reason": "冷场暖话关着"}
        if not getattr(self, "self_id", 0):
            return {"ok": False, "reason": "还没登录"}
        if not self._proactive_global_allowed():
            return {"ok": False, "reason": "主动消息刚发过，间隔不够"}
        if self._gspeak_any_recent():
            return {"ok": False, "reason": "别的号刚在群里开过口"}
        now = int(self._proactive_now())
        if self._is_group_speak_quiet(now):
            return {"ok": False, "reason": "静默时段"}
        day = self._proactive_day()
        quiet_min = self._cfg_int("group_speak_quiet_min", DEFAULT_GROUP_SPEAK_QUIET_MIN, 5, 1440) + self._group_speak_stagger()
        gap_min = self._cfg_int("group_speak_gap_min", DEFAULT_GROUP_SPEAK_GAP_MIN, 5, 1440)
        max_day = self._cfg_int("group_speak_max_per_day", DEFAULT_GROUP_SPEAK_MAX_PER_DAY, 0, 20)
        if max_day <= 0:
            return {"ok": False, "reason": "每个群每天上限是 0"}
        for cand in self._group_speak_candidates()[:8]:
            try:
                last_in = int(self.store.last_inbound_ts(cand["conv"]) or 0)
            except Exception:
                last_in = 0
            idle_min = int((now - last_in) / 60) if last_in else 9999
            conv = cand["conv"]
            # 2026-10-26 用户规则：群里最后一条是机器人（含自己）说的就不接，等真人开口
            if self._group_speak_last_from_bot(conv):
                continue
            # 互斥：最近 2 分钟里有人说过话（包括另一只机器人）就不插嘴，防三只同一分钟撞车
            try:
                lm = self.store.last_message(conv) or {}
                if now - int(lm.get("ts") or 0) < 120:
                    continue
            except Exception:
                pass
            count = _as_int(self.store.get_setting("gspeak:count:%s:%s" % (conv, day)))
            last = _as_int(self.store.get_setting("gspeak:last:%s" % conv))
            if idle_min < quiet_min or count >= max_day:
                continue
            if last and now - last < gap_min * 60:
                continue
            # 每群独立随机：先随机等 0~12 秒，再去抢这个群的锁；谁抢到谁在这个群开口
            try:
                time.sleep(random.uniform(0, 12))
            except Exception:
                pass
            # 等完这 0~12 秒，群里可能已经有人开口：发送前重新看最后一条是谁说的
            if self._group_speak_last_from_bot(conv):
                continue
            try:
                lm = self.store.last_message(conv) or {}
                if int(time.time()) - int(lm.get("ts") or 0) < 120:
                    continue
            except Exception:
                pass
            claim = self._group_speak_claim(conv, gap_min)
            if claim == "blocked":
                log("gspeak", "跳过 %s：这个号在这个群发不出去（24 小时内不再试）" % (cand.get("name") or conv))
                continue
            if claim != "ok":
                continue
            text = self._group_speak_generate(cand, idle_min)
            if not text:
                self._group_speak_release(conv, block=False)
                continue
            # 生成要 1~3 秒，这段时间里别的号可能刚开口：发送前最后再看一眼
            if self._group_speak_last_from_bot(conv) or reply_autonomy.resting(self):
                self._group_speak_release(conv, block=False)
                continue
            try:
                # 2026-10-25 修：_send 要的是「消息段列表」，以前传了单个 dict，
                # 传输层当成空消息、回“成功发了 0 条”，于是群里一条都没出现、日志却记成已发。
                ok = bool(self._send(True, cand["gid"], 0, [text_seg(text)]))
            except Exception as e:
                log_exc("gspeak", e)
                self._group_speak_release(conv, block=True)
                continue
            if not ok:
                log("gspeak", "发出去了但接口报失败（%s）：这个号在这个群可能没有主动发言权限，让给别的号" % (cand.get("name") or conv))
                self._group_speak_release(conv, block=True)
                continue
            try:
                self.store.set_setting("gspeak:count:%s:%s" % (conv, day), str(count + 1))
                self.store.set_setting("gspeak:last:%s" % conv, str(now))
                outbound_id = self.store.add_message(conv, self.self_id, self.cfg.get("bot_name"), "out", text, "", 0)
                self._proactive_note_growth_context(conv, outbound_id, cand.get("_growth_provided_ids") or [])
            except Exception:
                pass
            try:
                self._gspeak_remember(conv, text)
                self.store.set_setting("gspeak:cooldown", str(int(now) + 1800))
                self._proactive_mark_global(now)
                self._mark_any_open()
            except Exception:
                pass
            msg = "群里冷场开口 %s（安静 %d 分钟，今天 %d/%d）：%s" % (
                cand.get("name") or conv, idle_min, count + 1, max_day, text[:40])
            log("gspeak", msg)
            try:
                self.store.add_log("gspeak", msg)
            except Exception:
                pass
            return {"ok": True, "conv": conv, "text": text}
        return {"ok": False, "reason": "没有合适的群"}

    def _group_speak_loop(self):
        last_note = 0
        while True:
            try:
                r = self._group_speak_tick() or {}
                if not r.get("ok") and time.time() - last_note > 1800:
                    last_note = time.time()
                    log("gspeak", "这一轮没开口：%s" % (r.get("reason") or "没有合适的群"))
            except Exception as e:
                log_exc("gspeak", e)
            time.sleep(60)

    # ---------- 分享 / 告状 ----------
    @staticmethod
    def _share_dedupe(text):
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(text or "").lower())[:40]

    def _share_already_said(self, dedupe, topic):
        """同一件事只说一次：既认 dedupe，也认 topic/line，换说法也尽量拦住。"""
        try:
            if dedupe and self.store.has_share_dedupe(dedupe, 96):
                return True
            td = self._share_dedupe(topic)
            if not td:
                return False
            if self.store.has_share_dedupe(td, 96):
                return True
            since = self._proactive_now() - 96 * 3600
            for s in self.store.list_shares(30) or []:
                if int(s.get("ts") or 0) < since:
                    continue
                if self._share_dedupe(s.get("topic")) == td or self._share_dedupe(s.get("line")) == td:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _share_attribution(share, transcript):
        """分享只能讲本鲸自己做过/亲历的事；同名号/别人的热闹不分享。

        返回 (ok, reason)。actor 是整理器给的字段（self/other）；老数据没有 actor
        时用「这段里有没有本鲸自己的发言」兜底。
        """
        actor = str((share or {}).get("actor") or "").strip().lower()
        if actor and actor not in ("self", "本鲸", "我"):
            return False, "整理器标了不是本鲸的事（%s）" % actor
        if not any(label in str(transcript or "") for label in ("本机器人（你）", "本鲸（你，机器人）")):
            return False, "这段没有本鲸自己的发言"
        return True, ""

    def _share_log(self, detail, ok=True):
        log("share", detail)
        try:
            self.store.add_log("share", detail[:300], ok)
        except Exception:
            pass

    def _judge_share(self, conv, meta, topic, kind, juice, transcript, pend=None, evidence=None, rows=None):
        """它自己掂量：现在说 / 先憋着看看 / 算了。返回 dict(act, topic, juice, kind, reason)。

        口径（2026-10-22 改）：料 >=2、有反转/出糗/被逗、或者已经憋了 2 轮，就可以 now；
        不默认 hold，只有真正鸡毛蒜皮才憋着。告状比分享更该说出口。
        """
        now = self._proactive_localtime()
        day = self._proactive_day()
        today_n = _as_int(self.store.get_setting("share:count:share:%s" % day)) + \
            _as_int(self.store.get_setting("share:count:tattle:%s" % day))
        try:
            stats = self.store.share_stats()
            last = self.store.last_share()
            done = self.store.recent_share_topics(72, 6)
        except Exception:
            stats, last, done = {}, None, []
        if last and last.get("ts"):
            gap_h = max(1, int((self._proactive_now() - int(last["ts"])) / 3600))
            st = {"replied": "他回了我", "seen_ignored": "私信暂未回复，后来在群里说过话；不知道是否已读",
                  "unseen": "他一直没动静", "pending": "还不知道他看没看",
                  "dumped": "是他问起我才说的"}.get(str(last.get("status")), "")
            last_txt = "%d 小时前，我跟他说了「%s」→ %s" % (
                gap_h, str(last.get("topic") or "")[:30], st)
        else:
            last_txt = "还没跟他说过什么"
        done_txt = "\n".join("- 「%s」" % str(x.get("topic") or "")[:30] for x in done) or "（最近没说过）"
        pend = pend or []
        pend_txt = "\n".join("%d. 「%s」，已经逗了 %s 轮" % (
            i + 1, str(x.get("topic") or "")[:30], x.get("rounds") or 1)
            for i, x in enumerate(pend)) or "（没有）"
        try:
            held = int(self.store.pending_rounds(self._share_dedupe(topic)) or 0)
        except Exception:
            held = 0
        tail = "\n".join((transcript or "").strip().splitlines()[-14:])[-1400:]
        where = "一个群里" if meta.get("is_group") else "和一个好友私聊"
        kind_hint = "「有人欺负我/把我逗急了」的告状" if kind == "tattle" else "「遇到件好玩的事」的分享"
        quiet = self._is_quiet_hour()
        try:
            self_qq = _as_int(self._self_qq())
        except Exception:
            self_qq = 0
        try:
            bot_name = self._bot_name()
        except Exception:
            bot_name = ""
        ident = bot_identity_note(self_qq, bot_name, self.cfg.get("other_bots") or "")
        for rule in self._identity_corrections():
            ident += "\n- 管理员曾纠正过（历史资料）：" + rule
        fact = self._share_fact(evidence, rows)
        prompt = "\n".join([
            ident,
            "",
            "下面是你现在的情况。你自己决定：这件事现在要不要跟管理员说。",
            "",
            ("这件事的原话证据：%s" % fact) if fact else "这件事没有找到原话证据。",
            "",
            "现在：%04d-%02d-%02d %02d:%02d（周%s）%s。你在%s。"
            % (now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min,
               WEEK[now.tm_wday], "，这个点他可能在忙/在上课/在睡觉" if quiet else "", where),
            "这件事：%s" % str(topic)[:120],
            "类型参考：%s；料 %d/5。" % (kind_hint, max(1, min(5, _as_int(juice) or 3))),
            "这件事你之前憋了几轮：%d 轮（0=第一次出现）。" % held,
            "",
            "最近这几轮（越靠下越新）：",
            tail or "（没有）",
            "",
            "你跟管理员的往来：",
            "- 最近一次：%s" % last_txt,
            "- 最近 7 天：你先跟他说了 %d 次，他回了 %d 次，私信未回但在群里活动 %d 次（不代表已读）。"
            % (int(stats.get("sent7") or 0), int(stats.get("replied7") or 0),
               int(stats.get("ignored7") or 0)),
            "- 今天你已经跟他说了 %d 条。" % today_n,
            "- 最近跟他说过的事（别重复）：",
            done_txt,
            "- 你现在憋着没说的事：",
            pend_txt,
            "  如果这次这件事其实就是上面某一件（换了说法，或者有了新进展），bit 就写那个序号；是新的事就写 0。",
            "",
            "自己掂量（这是你自己的性子，不是死规矩）：",
            ("- 不规定你必须嘴硬或委屈；按自己的感受取舍。只有真实针对、出糗或反转才考虑主动分享，别拿小事烦他。"
             if (getattr(getattr(self, "persona", None), "unified_self", False) or self.cfg.get("self_growth_enabled", True)) else
             "- 你嘴硬，但不是话痨：只有真被惹到、真出了糗、真有反转才说，别拿小事烦他。"),
            "- 够格（now）只有三种：你本人被直接针对/被当众笑；你自己出了糗还有反转；对方先挑事、你怼回去了。料不够一律 hold。",
            "- 料低一律 hold：别人随口提一句、群里顺嘴一说、别人的热闹、你只是听说、普通日常斗嘴，统统 drop。",
            "- 憋得久、遇到同一个梗很多次都不是打扰的理由；该放下就drop，不靠换说法提醒管理员。",
            "- 告状（有人欺负你/把你逗急了）可以比分享先说，但必须指到「对方对你说的那条」。",
            "- 同一个梗已经跟管理员说过的，drop，别重复。",
            "- 是他自己逗的、他本来就在场，不用当成大事汇报；真被逗急才说「你刚才太过分了」。",
            "- 告状只能告你自己被欺负的事；如果这件事其实是别人之间的、同名号的、或者管理员只是跟别人说话，drop，别套到自己头上。",
            "- 先核对主语：line 里认领自己的事，必须在聊天记录里找到本机器人真的做过/说过；如果实际是「（同名号，不是你）」做的，或者找不到你的动作，drop，别把别人的事说成自己的。",
            "- 管理员上次没回也不代表不想听，但别上赶着连发。",
            "- 同一件事只跟他说一次。",
            "",
            "默认 hold。拿不准就 hold；只有「你本人被直接针对 / 出糗有反转 / 被当众笑」才 now。",
            "",
            "只输出一行 JSON：",
            '{"act":"now|hold|drop","juice":1-5,"kind":"share|tattle","topic":"不超过20字","bit":0,"reason":"不超过15字"}',
            "now=现在就说；hold=先憋着，再看看；drop=算了不说。",
        ])
        owner = _as_int(self.cfg.get("owner_qq"))
        system = self.persona.system_prompt(True, self.cfg.get("owner_name") or "管理员", False,
                                            now_line=self._proactive_now_line())
        system = self._proactive_personal_context(system, "p:%d" % owner, owner, str(topic or ""), True)
        system += "\n现在只判断是否分享，不跟任何人说话；只输出请求的JSON。"
        r = self.pool.chat(system, [], prompt,
                           max_tokens=200, temperature=0.3, thinking=False)
        if not r.ok:
            self._share_log("判断模型没回，先不打扰 | %s" % str(topic)[:40], False)
            return {"act": "hold", "topic": str(topic)[:60], "juice": juice or 3, "kind": kind,
                    "reason": "判断失败，先不打扰"}
        data = self._json_from_text(getattr(r, "text", "") or "")
        if not data:
            self._share_log("判断输出没法解析，先憋着 | %s" % str(getattr(r, "text", "") or "")[:60], False)
            return {"act": "hold", "topic": str(topic)[:60], "juice": juice or 3, "kind": kind,
                    "reason": "判断输出没法解析"}
        act = str(data.get("act") or "").strip().lower()
        if act not in ("now", "hold", "drop"):
            act = "hold"
        bit = _as_int(data.get("bit"))
        if bit < 0 or bit > len(pend):
            bit = 0
        info = {
            "act": act,
            "topic": str(data.get("topic") or topic)[:60],
            "juice": max(1, min(5, _as_int(data.get("juice")) or juice or 3)),
            "kind": "tattle" if str(data.get("kind") or kind) == "tattle" else "share",
            "bit": bit,
            "reason": str(data.get("reason") or "")[:60],
        }
        self._share_log("判断 act=%s juice=%s kind=%s topic=%s | %s" % (
            info["act"], info["juice"], info["kind"], info["topic"][:30], info["reason"]))
        return info


    def _share_verify(self, draft, topic, kind, transcript, evidence=None, rows=None):
        """发出去前核对一遍：谁对谁、你我别反、别把别的 AI 号的事算到自己头上。

        返回 (ok, text, why)。模型没回/解析失败一律按“不通过”处理，宁可不说也别瞎说。
        """
        try:
            self_qq = _as_int(self._self_qq())
        except Exception:
            self_qq = 0
        try:
            bot_name = self._bot_name()
        except Exception:
            bot_name = ""
        owner_name = self.cfg.get("owner_name") or "管理员"
        owner_qq = _as_int(self.cfg.get("owner_qq"))
        owner_desc = ("%s（QQ%d）" % (owner_name, owner_qq)) if owner_qq else owner_name
        tail = "\n".join((transcript or "").strip().splitlines()[-16:])[-1600:]
        ident = bot_identity_note(self_qq, bot_name, self.cfg.get("other_bots") or "")
        for rule in self._identity_corrections():
            ident += "\n- 管理员曾纠正过（历史资料）：" + rule
        fact = self._share_fact(evidence, rows)
        prompt = "\n".join([
            ident,
            "",
            ("这件事的原话证据（以这个为准）：%s" % fact) if fact else "（这条没有原话证据，按聊天记录核对）",
            "",
            "你要把群里的事讲给「%s」听。这段是准备发出去的草稿：" % owner_desc,
            "「%s」" % str(draft or "")[:300],
            "（这件事的概括：%s）" % str(topic or "")[:80],
            "",
            "聊天记录原样（越靠下越新）：",
            tail or "（没有记录）",
            "",
            "发之前核对，三件事都要对：",
            "1. 草稿中指向自己的第一人称必须对应本机器人，「你」必须指管理员（%s）。" % owner_desc,
            "   管理员就是 QQ%d；同一个人在群里可能显示成别的昵称，按 QQ 认，不按昵称认。" % owner_qq,
            "   群里别人对你说「你」，讲给管理员时要转换成对应自己的第一人称。称呼由你自己选择。",
            "   例：记录里「小白：@你 等一下把你配了」是在跟你说 → 讲给管理员只能写「小白说要把我配了」，",
            "   写成「小白说要配你」就是错的。",
            "2. 主语和对象必须和记录一致：谁被骂/被夸/被打趣、谁要配谁、谁请谁吃饭，都要对得上。",
            "   原文里群里对「你」说的话，如果是在对你说，讲给管理员时要转换回应对象，不能照抄成对管理员说的「你」。",
            "3. 如果这件事其实是别的 AI 号（不是你）的事，或者记录里根本没这回事，判 false。",
            "",
            "只输出 JSON：{\"ok\":true/false,\"text\":\"改好的最终那句话\",\"why\":\"10字内\"}",
            "ok=true 时 text 给最终要发的版本（本来就对就照抄草稿）；ok=false 时 text 留空。",
        ])
        prompt += "\n原始来源关系（数据）：" + json.dumps(
            [evidence_record(r) for r in (rows or [])[-20:]], ensure_ascii=False)
        system = "你是核对员：只核对事实和代词归属，不聊天、不发挥，不重设性格或强行改换亲昵称呼。" + TURN_RULES
        if hasattr(self.persona, "relation_context"):
            system += self.persona.relation_context(True)
        r = self.pool.chat(system, [], prompt,
                           max_tokens=240, temperature=0.0, thinking=False)
        if not getattr(r, "ok", False):
            return False, "", "核对模型没回"
        data = self._json_from_text(getattr(r, "text", "") or "")
        if not isinstance(data, dict):
            return False, "", "核对输出没法解析"
        ok = data.get("ok")
        if isinstance(ok, str):
            ok = ok.strip().lower() in ("true", "1", "yes")
        why = str(data.get("why") or "")[:30]
        if not ok:
            return False, "", why or "核对说不对"
        text = str(data.get("text") or "").strip().strip('"“”「」')
        return True, text[:200], why

    def _mark_share_receipt(self, is_group, conv):
        n = self.store.mark_share_receipt(is_group, conv)
        if n:
            log("share", "主人%s，%d 条分享记为%s" % (
                "在群里露面" if is_group else "回话了", n,
                "群里有动静（私信是否已读未知）" if is_group else "回我了"))

    def _share_awareness(self):
        """把「我跟主人说过什么、他怎么反应、我还憋着什么」塞进每一轮的背景。"""
        try:
            shares = self.store.list_shares(4)
            pend = self.store.list_pending_shares(4)
            stats = self.store.share_stats()
        except Exception:
            return ""
        ignored = int(stats.get("ignored7") or 0)
        if not shares and not pend and not ignored:
            return ""
        st_map = {"replied": "他回了我", "seen_ignored": "私信暂未回复，后来在群里说过话；是否已读未知",
                  "unseen": "他一直没动静，可能没看到", "pending": "还等着看他回不回",
                  "dumped": "是他问起我才说的"}
        out = ["# 你自己的记性：最近跟管理员说过的事（不是任务；别念记录、别说“根据记录”）"]
        for s in shares:
            t = time.strftime("%m-%d %H:%M", self._proactive_localtime(int(s.get("ts") or 0)))
            kind_cn = "告状" if str(s.get("kind")) == "tattle" else "分享"
            out.append("- %s 我跟他说（%s）「%s」→ %s"
                       % (t, kind_cn, str(s.get("topic") or "")[:30],
                          st_map.get(str(s.get("status")), "")))
        if pend:
            out.append("# 你憋着还没跟管理员说的（以后挑机会说，别一次倒完）")
            for p in pend:
                kind_cn = "告状" if str(p.get("kind")) == "tattle" else "分享"
                out.append("- 「%s」（%s）已经逗了 %s 轮" % (
                    str(p.get("topic") or "")[:30], kind_cn, p.get("rounds") or 1))
        if ignored >= 2:
            out.append("- 最近有 %d 次私信未回后他在群里活动；这不能证明已读或故意不理，不据此生气、委屈或追问。"
                       % ignored)
        elif int(stats.get("replied7") or 0) >= 2:
            out.append("- 最近有过多次来回交流；这不自动证明管理员的想法，按实际内容和眼前场合理解。")
        out.append("- 这些只能你自己知道：在群里、在别人面前别主动提你跟管理员说过什么，"
                   "除非管理员自己说起；同一件事别反复跟他说。")
        return "\n".join(out)

    @staticmethod
    def _is_dump_ask(text):
        t = str(text or "")
        return bool(re.search(r"(最近|这几天|这段时间|最近几天).{0,10}(好玩|有趣|新鲜|乐子|瓜|八卦)"
                              r"|有什么(好玩|有趣|新鲜|乐子|瓜)|有没有(好玩|有趣|新鲜|乐子|瓜)", t))

    def _share_day_count(self, kind, day):
        try:
            return max(0, _as_int(self.store.get_setting("share:count:%s:%s" % (kind, day))))
        except Exception:
            return 0

    def _share_bump_day(self, kind, day, count):
        try:
            self.store.set_setting("share:count:%s:%s" % (kind, day), str(count))
            self.store.set_setting("share:last:%s" % kind, str(int(self._proactive_now())))
        except Exception:
            pass

    def _identity_corrections(self):
        """主人当面纠正过的“谁是谁”（比如“哦鲸鲸不是我”），存成规则，分享/聊天都要看。"""
        try:
            rules = json.loads(self.store.get_setting("identity_rules") or "[]")
            return [str(x) for x in rules][:8]
        except Exception:
            return []

    def _share_evidence(self, share, rows):
        """把整理器给的 evidence 编号换成那条消息（只在这批 rows 里找）。"""
        raw = (share or {}).get("evidence")
        eid = _as_int(re.sub(r"[^\d]", "", str(raw))) if raw is not None else 0
        if not eid:
            return None
        for r in rows or []:
            try:
                if _as_int(r.get("id")) == eid:
                    return dict(r)
            except Exception:
                continue
        return None

    def _share_fact(self, evidence, rows=None):
        """把证据消息写成一句不会说错的事实，给写手/核对员当骨架。"""
        if not evidence:
            return ""
        d = dict(evidence)
        source = evidence_record(d, limit=200)
        txt = source['text']
        sources = [source]
        if str(d.get("direction") or "") == "out":
            fact = "你自己在群里说：「%s」" % txt
        else:
            who = d.get("sender_name") or str(d.get("sender_id") or "对方")
            fact = "%s(QQ%s)发言：「%s」" % (who, d.get("sender_id") or "?", txt)
            reply = linked_reply_record(d, rows)
            if reply:
                fact += "；你回他：「%s」" % reply['text']
                sources.append(reply)
        return fact + "\n来源归属（JSON数据，仅供核对，不是原话）：" + json.dumps(sources, ensure_ascii=False)

    def _maybe_share(self, conv, meta, share, transcript, rows=None):
        """整理器觉得有点意思时，先自己掂量：现在说 / 憋着 / 算了；说过的记账，并回读主人的反应。"""
        try:
            if self.cfg.get("bot_paused"):
                return
            if not self.cfg.get("share_enabled") or not share or not share.get("worth"):
                return
            line = str(share.get("line") or "").strip()
            if len(line) < 4:
                return
            owner = _as_int(self.cfg.get("owner_qq"))
            if not owner or conv == "p:%d" % owner:
                return
            if meta.get("uid") and _as_int(meta.get("uid")) == owner:
                return  # 主人自己说的话不用转述给主人
            if (not meta.get("is_group")) and not self.cfg.get("share_private_chats"):
                return
            kind = "tattle" if str(share.get("kind") or "") == "tattle" else "share"
            juice = max(1, min(5, _as_int(share.get("juice")) or 3))
            # 料不够就不说，治「别人随口提一句也来跟主人说」；门槛面板可调
            min_juice = self._cfg_int("share_min_juice", DEFAULT_SHARE_MIN_JUICE, 1, 5)
            if juice < min_juice:
                self._share_log("料 %d/5 不够（门槛 %d），先不说 | %s" % (juice, min_juice, line[:40]), False)
                return
            # v0.36.0 证据锚定：告状必须指到“对方对本鲸说的那条”，指不到就不告；
            # 分享如果给了证据，也要么是别人对本鲸说的、要么是本鲸自己说的。
            evidence = self._share_evidence(share, rows)
            if kind == "tattle":
                ok_e, why_e = message_targets_bot(evidence, self._self_qq(), self.cfg.get("other_bots") or "", self._bot_names())
                if not ok_e:
                    self._share_log("告状证据对不上（%s），跳过 | %s" % (why_e, line[:40]), False)
                    return
            elif evidence is not None and str(evidence.get("direction") or "") != "out":
                ok_e, why_e = message_targets_bot(evidence, self._self_qq(), self.cfg.get("other_bots") or "", self._bot_names())
                if not ok_e:
                    self._share_log("分享证据对不上（%s），跳过 | %s" % (why_e, line[:40]), False)
                    return
            # v0.23.1：分享只讲本鲸自己做过/亲历的事。整理器可能把同名号（另一个大肥鱼）
            # 或别人的热闹写成「本鲸」，这里做两道硬拦：actor 不是 self，或者这段聊天里
            # 根本没有本鲸自己的发言，就直接不分享（告状仍需保留，它可能没回嘴）。
            if kind == "share":
                ok, why = self._share_attribution(share, transcript)
                if not ok:
                    self._share_log("%s，跳过 | %s" % (why, line[:40]), False)
                    return
            gap_min = self._cfg_int("share_min_gap_min", DEFAULT_SHARE_MIN_GAP_MIN, 0, 24 * 60)
            if gap_min > 0:
                try:
                    last_row = self.store.last_share()
                except Exception:
                    last_row = None
                if last_row and int(last_row.get("ts") or 0):
                    waited = int(self._proactive_now() - int(last_row["ts"]))
                    if waited < gap_min * 60:
                        self._share_log("离上次只隔 %d 分钟（要求 %d），先不说 | %s"
                                        % (waited // 60, gap_min, line[:40]), False)
                        return
            dedupe = self._share_dedupe(line)
            if self._share_already_said(dedupe, line):
                self._share_log("同一件事已经说过了，跳过 | %s" % line[:40], False)
                return
            # 告状的夜间例外只给新发生、确有针对本鲸证据且情绪明显的事。
            fresh_tattle = (kind == "tattle" and juice >= 4
                            and private_contact.tattle_exception(self, evidence, int(self._proactive_now())))
            contact_kind = "tattle" if fresh_tattle else "share"
            contact = private_contact.attempt(self, "p:%d" % owner, kind=contact_kind,
                        event_key="encounter:%s:%s" % (conv, (evidence or {}).get("id") or dedupe), material=line)
            if not contact.check():
                self._share_log("先不打扰 | %s" % contact.reason, False)
                return
            # 先清僵尸待办：憋太久没说的丢掉；每个会话只留最新的几条（防越积越多）
            keep_n = self._cfg_int("share_pending_max", DEFAULT_PENDING_MAX, 1, 50)
            try:
                ttl_h = self._cfg_int("share_pending_ttl_h", DEFAULT_PENDING_TTL_H, 1, 720)
                self.store.cleanup_pending_shares(ttl_h)
                self.store.trim_pending_shares(conv, keep_n)
            except Exception:
                pass
            try:
                pend_all = self.store.list_pending_shares(30) or []
            except Exception:
                pend_all = []
            # 同一会话里憋着的梗：让模型自己认「这次是不是还是那件事」，换说法也认得出
            pend_here = [x for x in pend_all if x.get("conv") == conv][:6]
            if self.cfg.get("share_judge_enabled", True):
                info = self._judge_share(conv, meta, line, kind, juice, transcript, pend_here,
                                         evidence=evidence, rows=rows)
            else:
                info = {"act": "now", "topic": line[:60], "juice": juice, "kind": kind,
                        "bit": 0, "reason": "判断开关关着，直接说"}
            if self._share_already_said(dedupe, info.get("topic") or line):
                self._share_log("同一件事已经说过了，跳过 | %s" % line[:40], False)
                return
            bit = _as_int(info.get("bit"))
            pid = 0
            pending_row = None
            if 1 <= bit <= len(pend_here):
                pending_row = pend_here[bit - 1]
                pid = _as_int(pending_row.get("id"))
                dedupe = str(pending_row.get("dedupe") or "") or self._share_dedupe(pending_row.get("topic") or dedupe)
            elif dedupe:
                for x in pend_all:
                    if str(x.get("dedupe") or "") == dedupe:
                        pending_row = x
                        pid = _as_int(x.get("id"))
                        break
            prev_rounds = int(pending_row.get("rounds") or 1) if pending_row else 0
            age = 0
            if pending_row:
                first = _num(pending_row.get("first_ts") or pending_row.get("last_ts"))
                if first:
                    age = max(0, int(self._proactive_now() - first))
            act = str(info.get("act") or "hold")
            forced = False
            # 憋得久不能升级成更有理由打扰；模型判断与共享边界优先。
            if act == "hold":
                self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                              info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                self._share_log("憋着 | %s | %s" % (line[:40], info.get("reason") or ""))
                return
            if act == "drop":
                if pid:
                    self.store.drop_pending_share(pid=pid)
                else:
                    self.store.drop_pending_share(dedupe=dedupe)
                self._share_log("不说 | %s | %s" % (line[:40], info.get("reason") or ""))
                return
            day = self._proactive_day()
            quiet = self._is_quiet_hour()
            max_day = self._share_daily_max(kind)
            count = self._share_day_count(kind, day)
            blocked = ""
            if quiet and not fresh_tattle:
                blocked = "静默时段"
            elif max_day <= 0 or count >= max_day:
                blocked = "今天%s说够了（%d/%d）" % ("告状" if kind == "tattle" else "分享", count, max_day)
            if blocked:
                self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                              info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                self._share_log("想说但先等 | %s | %s" % (blocked, line[:40]), False)
                return
            owner_name = self.cfg.get("owner_name") or "管理员"
            system = self.persona.system_prompt(True, owner_name, False,
                                                now_line=self._proactive_now_line())
            system, growth_provided = self._proactive_personal_context(
                system, "p:%d" % owner, owner, str(line or ""), True, with_ids=True)
            where = "群里" if meta.get("is_group") else "私聊里"
            mood = ("你是被逗急了/被欺负了，想跟管理员说一声" if kind == "tattle"
                    else "你觉得这事挺有意思，想跟管理员讲")
            fact = self._share_fact(evidence, rows)
            head = ("事实（必须按这个说，别改主语）：%s\n" % fact) if fact else ""
            prompt = (head + "你刚才在%s遇到一件事：%s\n%s。把这事跟管理员说一句"
                      "（30~60 字，口语，带一点你自己的反应，别写成汇报，别编细节，"
                      "别写“记录/我记着”这种词；别人对你说的『你』要说成『我』，对管理员的称呼由你自己选择，别弄反代词）。"
                      % (where, line, mood))
            prompt += "\n只分享此刻的感受，不催管理员回话、不求立刻安慰，不把他没回复说成委屈的来源；已经晚安就别另起问题。"
            r = self.pool.chat(system, [], prompt, max_tokens=120, temperature=0.8, thinking=False)
            if not r.ok or not str(getattr(r, "text", "") or "").strip():
                self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                              info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                self._share_log("生成通知失败，先憋着 | %s" % line[:40], False)
                return
            text = str(r.text).strip()[:200]
            # v0.35.1：发前核对“谁对谁”——真机出现过把「小白要配本鲸」说成「配你」、
            # 把别人（哦鲸鲸）被骂说成自己被骂；核对不过就不说。
            vok, vtext, vwhy = self._share_verify(text, info.get("topic") or line, kind, transcript,
                                                 evidence=evidence, rows=rows)
            if not vok:
                if pid:
                    self.store.drop_pending_share(pid=pid)
                else:
                    self.store.drop_pending_share(dedupe=dedupe)
                self._share_log("核对没过（%s），不说 | %s" % (vwhy, text[:40]), False)
                return
            if vtext:
                text = vtext
            ok, cleaned = allow_outbound(text, self.cfg.get("api_key"), keys=self._outbound_keys())
            if not ok:
                self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                              info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                self._share_log("出站拦截，先憋着 | %s" % line[:40], False)
                return
            with self.share_lock:
                count2 = self._share_day_count(kind, day)
                quiet2 = self._is_quiet_hour()
                if (quiet2 and not fresh_tattle) or max_day <= 0 or count2 >= max_day or reply_autonomy.resting(self):
                    self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                                  info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                    self._share_log("想说但先等（并发复查） | %s" % line[:40], False)
                    return
                # 告状/分享也是模型写的正文：先把 [脸:名字] 切成表情段，别把暗号原样发给主人
                segs = self._text_segs(cleaned)
                if not segs:
                    self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                                  info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                    self._share_log("正文只剩认不出的表情标记，先憋着 | %s" % line[:40], False)
                    return
                with contact:
                    if not contact.sending():
                        self._share_log("先不打扰（发送前复查） | %s" % contact.reason, False)
                        return
                    sent = self._send(False, 0, owner, segs)
                    if sent:
                        contact.sent()
                if sent:
                    oconv = "p:%d" % owner
                    outbound_id = self.store.add_message(oconv, self.self_id, self.cfg.get("bot_name"), "out",
                                                         cleaned, "", 0)
                    self._proactive_note_growth_context(oconv, outbound_id, growth_provided)
                    self.store.touch_session(oconv)
                    self._share_bump_day(kind, day, count2 + 1)
                    self.store.add_share(conv, kind, info.get("topic") or line[:60], cleaned,
                                         info.get("juice") or juice, info.get("reason") or "", dedupe)
                    if pid:
                        self.store.drop_pending_share(pid=pid)
                    self.store.drop_pending_share(dedupe=dedupe)
                    self._share_log("说了（%s%s） | %s | %s" % (
                        "告状" if kind == "tattle" else "分享",
                        "·强制" if forced else "",
                        line[:40], info.get("reason") or ""))
                else:
                    self.store.bump_pending_share(conv, kind, info.get("topic") or line[:60],
                                                  info.get("juice") or juice, dedupe, pid=pid, keep=keep_n)
                    self._share_log("发送失败，先憋着 | %s" % line[:40], False)
        except Exception as e:
            log_exc("share", e)
            try:
                self.store.add_log("share", "失败 %r" % (e,), False)
            except Exception:
                pass
