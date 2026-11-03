# -*- coding: utf-8 -*-
"""账目：DeepSeek 官方余额 + 本机用量折算成钱。

数据来源（都是只读，不花钱）：
- 余额：GET https://api.deepseek.com/user/balance（DeepSeek 官方文档 2026-10）
- 用量：bot.db 的 usage_records（每次模型成功调用都记账，含缓存命中 token）
- 单价：DeepSeek 官方价格页 2026-10-17（元 / 百万 tokens，型号 deepseek-flash）：
    输入（缓存命中）  空闲 0.02  高峰 0.04
    输入（缓存未命中）空闲 1     高峰 2
    输出              空闲 4     高峰 8
  高峰 = 北京时间周一~周五 9:00-12:00、14:00-18:00。
  官方说“不含中国法定节假日”，本机没有节假日表，这里按工作日近似（估的钱只会略偏高）。
- 火山方舟 Agent Plan 按 AFP 套餐计量，不从 DeepSeek 余额扣钱；本地模型不花钱。

设计原则：
- 只有主人问起、且问得明显（余额/花了多少/token/账单…）才查，平时不打扰、不发请求。
- 余额 60 秒缓存，防止连问时反复打接口。
- 查不到就如实说查不到，绝不编数字。
"""
import json
import re
import time
import urllib.error
import urllib.request

from util import log

BALANCE_TTL = 60
BALANCE_URL = "/user/balance"

# 元 / 百万 tokens，DeepSeek 官方 2026-10-17 价格页
PRICE = {
    "flash": {"peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
              "off": {"hit": 0.02, "miss": 1.0, "out": 4.0}},
    "pro": {"peak": {"hit": 0.30, "miss": 9.0, "out": 27.0},
            "off": {"hit": 0.15, "miss": 4.5, "out": 13.5}},
}
PEAK_HOURS = ((9, 12), (14, 18))

# “明显在问账目”才触发：余额 / 花钱 / token / 账单 / 额度 / 充值
BILLING_RE = re.compile(
    r"(余额|账上|账户还有|还剩多少钱|还有多少钱|花多少钱|花了多少钱|花了多少米|花费多少|"
    r"用了多少(?:token|tokens|个token|钱|块|米)|用掉多少token|扣了多少钱|扣费|欠费|账单|充值|"
    r"tokens?|token\s*用量|用量|额度|套餐还剩|烧钱|成本多少|费钱吗|"
    r"今天花|这个月花|一天花|一个月花)"
)

_bal_cache = {"at": 0.0, "data": None}


def looks_like_billing(text):
    return bool(BILLING_RE.search(str(text or "")))


def models_of(cfg):
    try:
        return [m for m in (cfg.models() or []) if isinstance(m, dict)]
    except Exception:
        return []


def deepseek_models(cfg):
    return [m for m in models_of(cfg) if "api.deepseek.com" in str(m.get("base_url") or "").lower()]


def deepseek_key(cfg):
    for m in deepseek_models(cfg):
        key = str(m.get("api_key") or "").strip()
        if key:
            return key
    try:
        return str(cfg.get("deepseek_api_key") or "").strip()
    except Exception:
        return ""


def _origin(base_url):
    m = re.match(r"^(https?://[^/]+)", str(base_url or "https://api.deepseek.com").strip())
    return m.group(1) if m else "https://api.deepseek.com"


def _num(v, default=0.0):
    try:
        return float(str(v).strip())
    except Exception:
        try:
            return float(v)
        except Exception:
            return default


def _fetch_balance(cfg, timeout=10):
    """真实请求官方余额接口。返回 (dict 或 None, 错误说明)。"""
    key = deepseek_key(cfg)
    if not key:
        return None, "没有配置 DeepSeek 官方 Key"
    conf = (deepseek_models(cfg) or [{}])[0]
    url = _origin(conf.get("base_url")) + BALANCE_URL
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=max(3, int(timeout))) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:160]
        return None, "HTTP %s %s" % (e.code, detail.replace("\n", " "))
    except Exception as e:
        return None, repr(e)[:180]


def balance(cfg, refresh=False, timeout=10):
    """带缓存的余额；返回统一结构，查不到时 ok=False 且带 error。"""
    now = time.time()
    cached = _bal_cache.get("data")
    if isinstance(cached, dict) and not refresh and now - float(_bal_cache.get("at") or 0) < BALANCE_TTL:
        return cached
    obj, err = _fetch_balance(cfg, timeout=timeout)
    data = {"ok": bool(obj), "error": err, "is_available": False, "infos": [], "at": int(now)}
    if obj:
        data["is_available"] = bool(obj.get("is_available"))
        for b in obj.get("balance_infos") or []:
            if not isinstance(b, dict):
                continue
            data["infos"].append({
                "currency": str(b.get("currency") or "").strip().upper(),
                "total": _num(b.get("total_balance")),
                "granted": _num(b.get("granted_balance")),
                "topped": _num(b.get("topped_up_balance")),
            })
    _bal_cache["at"] = now
    _bal_cache["data"] = data
    if err:
        log("billing", "余额查询失败：%s" % err[:180])
    return data


def is_peak(ts=None):
    t = time.localtime(ts if ts is not None else time.time())
    if t.tm_wday >= 5:
        return False
    return any(a <= t.tm_hour < b for a, b in PEAK_HOURS)


def price_for(model_name, ts=None):
    name = str(model_name or "").lower()
    table = PRICE["pro"] if "pro" in name else PRICE["flash"]
    return table["peak" if is_peak(ts) else "off"]


def cost_of(model_name, prompt_tokens, completion_tokens, cache_hit_tokens=0, ts=None):
    """按官方单价估算一次调用/一批调用的钱（元）。缓存命中比未命中便宜 50 倍。"""
    p = max(0, int(prompt_tokens or 0))
    c = max(0, int(completion_tokens or 0))
    hit = max(0, min(p, int(cache_hit_tokens or 0)))
    miss = p - hit
    price = price_for(model_name, ts)
    return (miss * price["miss"] + hit * price["hit"] + c * price["out"]) / 1000000.0


def _label(conf):
    return str(conf.get("name") or conf.get("model") or conf.get("id") or "").strip() or "未知模型"


def _fmt_money(v):
    if v is None:
        return ""
    if v <= 0:
        return "0 元"
    if v < 0.01:
        return "不到 1 分钱"
    if v < 1:
        return "%.2f 元" % v
    return "%.2f 元" % v


def _fmt_tokens(n):
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    if n >= 10000:
        return "%d 万" % int(round(n / 10000.0))
    return str(n)


def summary(cfg, store, now=None):
    """汇总：余额 + 今日 token + 今日官方费用估算。非主人不要调用。"""
    now = float(now if now is not None else time.time())
    lt = time.localtime(now)
    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    models = models_of(cfg)
    ds_ids = [str(m.get("id") or "") for m in deepseek_models(cfg) if m.get("id")]
    rows = []
    try:
        rows = (store.usage_stats() or {}).get("today") or []
    except Exception as e:
        log("billing", "读今日用量失败 %r" % (e,))
    total_p = total_c = 0
    by_model = []
    costs = []
    for r in rows:
        mid = str(r.get("model") or "")
        p = max(0, int(r.get("prompt") or 0))
        c = max(0, int(r.get("completion") or 0))
        total_p += p
        total_c += c
        conf = next((m for m in models if str(m.get("id") or "") == mid), {})
        official = "api.deepseek.com" in str(conf.get("base_url") or "").lower()
        cost = None
        if official:
            # 逐条按调用时间判断高峰/空闲，缓存命中按官方 hit 价
            cost = 0.0
            try:
                for u in store.usage_rows_since(day0, model_ids=[mid]):
                    cost += cost_of(conf.get("model") or mid,
                                    u.get("prompt_tokens"), u.get("completion_tokens"),
                                    u.get("cache_hit_tokens"), u.get("ts"))
            except Exception as e:
                log("billing", "算官方费用失败 %r" % (e,))
                cost = None
            if cost is not None:
                costs.append(cost)
        by_model.append({"id": mid, "name": _label(conf) if conf else mid,
                         "prompt": p, "completion": c, "total": p + c,
                         "official": official, "cost": cost})
    return {
        "at": int(now),
        "date": time.strftime("%Y-%m-%d", lt),
        "balance": balance(cfg),
        "tokens": {"prompt": total_p, "completion": total_c, "total": total_p + total_c},
        "by_model": by_model,
        "official_ids": ds_ids,
        "cost": round(sum(costs), 6) if costs else None,
        "has_official": bool(by_model and any(b["official"] and b["total"] > 0 for b in by_model)),
    }


def _balance_line(bal):
    if not bal or not bal.get("ok"):
        err = str((bal or {}).get("error") or "未知错误")[:120]
        return "- 余额：这次没查到（%s）。如实说查不到，别编数字。" % err
    infos = bal.get("infos") or []
    cny = next((x for x in infos if x.get("currency") == "CNY"), None) or (infos[0] if infos else None)
    if not cny:
        return "- 余额：接口查到了，但没有余额条目。如实说暂时没有余额信息。"
    line = "- DeepSeek 官方账户余额：%s %s" % (_fmt_money(cny.get("total")), cny.get("currency") or "")
    extra = []
    if cny.get("granted"):
        extra.append("赠金 %s" % _fmt_money(cny.get("granted")))
    if cny.get("topped"):
        extra.append("充值余额 %s" % _fmt_money(cny.get("topped")))
    if extra:
        line += "（%s）" % "、".join(extra)
    if not bal.get("is_available"):
        line += "；接口说账户当前不可用"
    return line


def format_prompt_block(data):
    """主人问账目时塞进系统提示的块：只给事实和口径，不让模型编。"""
    if not data:
        return ""
    lines = ["\n\n# 你的账目（刚刚实时查的：DeepSeek 官方接口 + 本机记账，是真实数据）"]
    lines.append(_balance_line(data.get("balance")))
    tok = data.get("tokens") or {}
    if tok.get("total"):
        lines.append("- 今天（%s）到现在，你一共用了 %d 个 token（输入 %d、输出 %d）。"
                     % (data.get("date") or "", int(tok.get("total") or 0),
                        int(tok.get("prompt") or 0), int(tok.get("completion") or 0)))
        for b in data.get("by_model") or []:
            if not b.get("total"):
                continue
            if b.get("official"):
                extra = "，%s（按官方单价估算，实际以官方账单为准）" % _fmt_money(b.get("cost"))
                lines.append("- %s：%d 个 token%s。" % (b.get("name"), int(b.get("total") or 0), extra))
            elif "127.0.0.1" in str(b.get("id") or "") or "local" in str(b.get("id") or "").lower():
                lines.append("- %s：%d 个 token（本机跑的，不花钱）。" % (b.get("name"), int(b.get("total") or 0)))
            else:
                lines.append("- %s：%d 个 token（走套餐/按厂商自己的额度计，不从 DeepSeek 余额扣钱）。"
                             % (b.get("name"), int(b.get("total") or 0)))
    else:
        lines.append("- 今天到现在还没调用过模型（0 token）。")
    if not data.get("has_official"):
        lines.append("- 今天还没走 DeepSeek 官方接口，所以没从这个余额里扣钱。")
    lines.append("- 回答要求：用你自己一两句话的语气说清楚就行，别列明细表、别报接口名/配置/代码；"
                 "数字按这里的原样说，别改别编；问余额就报余额，问今天花多少就报 token 和估的钱；"
                 "余额没查到就直说这次查不到。")
    return "\n".join(lines)


def non_owner_block():
    """非主人问用量：一个数字都不许报，但话要说人话。"""
    return ("\n\n# 有人问你的用量（token）\n"
            "这是主人的私事：一个数字都别报，也别猜、别报余额和钱。"
            "用你平时说话的样子把话岔开（一两句，别背课文、别说“抱歉/无法”）。")


# v0.56.0：机器人只知道自己用了多少 token，不知道余额、金额、别的机器人。
TOKEN_TOPIC_RE = re.compile(
    r"(tokens?|用量|额度|账单|余额|"
    r"花(?:了|掉)?多少|用了多少|吃了多少|吃掉多少|跑了多少|烧了?多少|消耗|喂了?多少|"
    r"\d+\s*(?:万|k|w)?\s*(?:token|个字|字))")


def looks_like_token_topic(text):
    """粗筛“可能跟用量有关”，最终报不报交给模型判意图。"""
    return bool(TOKEN_TOPIC_RE.search(str(text or "")))


def summary_tokens_only(cfg, store, now=None):
    """只算这只机器人自己的 token；不查余额、不算钱。"""
    import time as _time
    now = float(now if now is not None else _time.time())
    lt = _time.localtime(now)
    day0 = int(_time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    total_p = total_c = 0
    try:
        for r in (store.usage_stats() or {}).get("today") or []:
            total_p += max(0, int(r.get("prompt") or 0))
            total_c += max(0, int(r.get("completion") or 0))
    except Exception as e:
        log("billing", "读今日用量失败 %r" % (e,))
    return {
        "at": int(now),
        "date": _time.strftime("%Y-%m-%d", lt),
        "tokens": {"prompt": total_p, "completion": total_c, "total": total_p + total_c},
    }


def format_token_block(data, bot_name=""):
    """给模型的用量块：只有它自己今天的 token，没有余额/金额/别的机器人。"""
    if not data:
        return ""
    tok = data.get("tokens") or {}
    total = int(tok.get("total") or 0)
    lines = ["\n\n# 你自己的用量（只算你今天的，真实数据）"]
    if total:
        lines.append("- 今天（%s）到现在，你一共用了 %d 个 token（输入 %d、输出 %d）。"
                     % (data.get("date") or "", total,
                        int(tok.get("prompt") or 0), int(tok.get("completion") or 0)))
    else:
        lines.append("- 今天到现在还没调用过模型（0 token）。")
    lines.append("- 怎么答：用你平时跟对方说话的样子，一两句说清就行；别列明细表、别客服腔；"
                 "只说 token，不许提余额、金额、钱、账户、别的机器人；数字按这里的说，别改别编。")
    return "\n".join(lines)
