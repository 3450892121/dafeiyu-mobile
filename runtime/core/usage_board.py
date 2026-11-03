# -*- coding: utf-8 -*-
"""用量看板聚合（v0.65.2）。

面板「用量」页要一次拿到画图要的全部数字：总量、按模型/按厂商拆分、按天、近 24 小时。
只读 bot.db 的 usage_records，不改任何东西。所有查询都带时间范围，30 天也就几千行。

口径（2026-10-27 用户拍板）：
- 请求数：每一次「换模型尝试」算一次；同一只模型的自动重试不单独记。
- 成功率：成功次数 / 请求总数。失败记录是 v0.65.2 才开始写的，更早的算成功。
- 提供商：模型属哪个厂商（DeepSeek / 通义千问 / 豆包…），不是线路。
- 账号/线路：配置里那条（ark-flash、syuan-flash…）。
"""
import time

# 厂商识别：先看配置里的模型名 / base_url，再退回线路 id 里的关键字
VENDOR_KEYS = (
    ("deepseek", "DeepSeek"),
    ("doubao", "豆包"),
    ("seed", "豆包"),
    ("qwen", "通义千问"),
    ("glm", "智谱"),
    ("kimi", "月之暗面"),
    ("moonshot", "月之暗面"),
    ("gpt", "OpenAI"),
    ("claude", "Anthropic"),
    ("gemini", "Google"),
    ("llama", "Meta"),
    ("mistral", "Mistral"),
)

RANGE_LABEL = {
    "today": "今天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    "month": "本月",
    "all": "全部",
    "custom": "自定义",
}


def _day_start(now, off=0):
    x = time.localtime(now - off * 86400)
    return int(time.mktime((x.tm_year, x.tm_mon, x.tm_mday, 0, 0, 0, 0, 0, -1)))


def _vendor_of(mid, conf):
    text = " ".join([str(conf.get("model") or ""), str(conf.get("name") or ""),
                     str(conf.get("base_url") or "")]).lower()
    for key, name in VENDOR_KEYS:
        if key in text:
            return name
    low = str(mid or "").lower()
    for key, name in VENDOR_KEYS:
        if key in low:
            return name
    return "其他"


def _label_of(mid, conf):
    name = str(conf.get("name") or "").strip()
    if name:
        return name
    model = str(conf.get("model") or "").strip()
    if model:
        return model
    return (mid or "-") + "（配置里已没有）"


def collect(con, models_conf=None, rng="today", frm=0, to=0, now=None):
    """返回画图要的整包数据。con 是 bot.db 的 sqlite 连接。"""
    now = int(now or time.time())
    rng = str(rng or "today")
    lt = time.localtime(now)
    month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    if rng == "today":
        start, end = _day_start(now, 0), 0
    elif rng == "7d":
        start, end = _day_start(now, 6), 0
    elif rng == "30d":
        start, end = _day_start(now, 29), 0
    elif rng == "month":
        start, end = month_start, 0
    elif rng == "all":
        start, end = 0, 0
    elif rng == "custom":
        start, end = int(frm or 0), int(to or 0)
        if not start:
            start, rng = _day_start(now, 6), "7d"
    else:
        rng, start, end = "today", _day_start(now, 0), 0

    out = {
        "ok": True,
        "range": rng,
        "label": RANGE_LABEL.get(rng, rng),
        "from": start,
        "to": end or now,
        "empty": True,
        "totals": {"req": 0, "ok": 0, "fail": 0, "rate": 0.0,
                   "prompt": 0, "completion": 0, "total": 0,
                   "cache_hit": 0, "cache_rate": 0.0},
        "days": {"active": 0, "streak": 0, "first": "", "last": ""},
        "models": [], "vendors": [], "daily": [], "hourly": [],
    }

    cols = [r[1] for r in con.execute("PRAGMA table_info(usage_records)").fetchall()]
    if not cols:
        return out
    has_ok = "ok" in cols
    okexpr = "COALESCE(ok,1)" if has_ok else "1"

    conf_map = {}
    for m in (models_conf or []):
        if isinstance(m, dict) and m.get("id"):
            conf_map[str(m.get("id"))] = m

    where = "ts >= ?"
    args = [start]
    if end:
        where += " AND ts < ?"
        args.append(end)
    else:
        # 没给上限时也别把「未来时间」算进来
        where += " AND ts <= ?"
        args.append(now)

    rows = []
    try:
        rows = con.execute(
            "SELECT model_id mid, COUNT(*) n, SUM(" + okexpr + ") okn, "
            "SUM(prompt_tokens) p, SUM(completion_tokens) c, "
            "SUM(COALESCE(cache_hit_tokens,0)) h "
            "FROM usage_records WHERE " + where + " GROUP BY model_id",
            tuple(args)).fetchall()
    except Exception:
        rows = []

    tot = {"req": 0, "ok": 0, "fail": 0, "prompt": 0, "completion": 0, "cache_hit": 0}
    models = []
    vendor_map = {}
    for r in rows:
        mid = str(r["mid"] or "").strip() or "-"
        conf = conf_map.get(mid) or {}
        n = int(r["n"] or 0)
        okn = int(r["okn"] or 0)
        p = int(r["p"] or 0)
        c = int(r["c"] or 0)
        h = int(r["h"] or 0)
        vendor = _vendor_of(mid, conf)
        models.append({"id": mid, "label": _label_of(mid, conf), "vendor": vendor,
                       "alive": bool(conf),   # 配置里还有这条线路吗（删掉的老线路只剩历史记录）
                       "req": n, "ok": okn, "fail": max(0, n - okn),
                       "prompt": p, "completion": c, "total": p + c, "cache_hit": h})
        tot["req"] += n
        tot["ok"] += okn
        tot["prompt"] += p
        tot["completion"] += c
        tot["cache_hit"] += h
        v = vendor_map.setdefault(vendor, {"name": vendor, "req": 0, "total": 0, "prompt": 0})
        v["req"] += n
        v["total"] += p + c
        v["prompt"] += p
    tot["fail"] = max(0, tot["req"] - tot["ok"])
    grand = tot["prompt"] + tot["completion"]
    models.sort(key=lambda x: (-x["total"], -x["req"]))
    for m in models:
        m["pct"] = round(m["total"] * 100.0 / grand, 1) if grand else 0.0
    vendors = sorted(vendor_map.values(), key=lambda x: -x["total"])
    for v in vendors:
        v["pct"] = round(v["total"] * 100.0 / grand, 1) if grand else 0.0

    out["empty"] = tot["req"] == 0
    out["totals"] = {
        "req": tot["req"], "ok": tot["ok"], "fail": tot["fail"],
        "rate": round(tot["ok"] * 100.0 / tot["req"], 1) if tot["req"] else 0.0,
        "prompt": tot["prompt"], "completion": tot["completion"], "total": tot["prompt"] + tot["completion"],
        "cache_hit": tot["cache_hit"],
        "cache_rate": round(tot["cache_hit"] * 100.0 / tot["prompt"], 1) if tot["prompt"] else 0.0,
    }
    out["models"] = models
    out["vendors"] = vendors

    # ---- 按天 ----
    try:
        drows = con.execute(
            "SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n, "
            "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(COALESCE(cache_hit_tokens,0)) h "
            "FROM usage_records WHERE " + where + " GROUP BY d ORDER BY d",
            tuple(args)).fetchall()
    except Exception:
        drows = []
    day_tot = {}
    for r in drows:
        day_tot[str(r["d"])] = {"date": str(r["d"]), "req": int(r["n"] or 0),
                                "prompt": int(r["p"] or 0), "completion": int(r["c"] or 0),
                                "cache_hit": int(r["h"] or 0),
                                "total": int(r["p"] or 0) + int(r["c"] or 0)}
    daily = []
    if rng in ("today", "7d", "30d", "month"):
        span = max(1, int((_day_start(now, 0) - start) / 86400) + 1)
        span = min(span, 120)
        for i in range(span):
            d = time.strftime("%Y-%m-%d", time.localtime(_day_start(now, 0) - (span - 1 - i) * 86400))
            row = day_tot.get(d) or {"date": d, "req": 0, "prompt": 0, "completion": 0,
                                     "cache_hit": 0, "total": 0}
            row["label"] = d[5:]
            daily.append(row)
    else:
        for d in sorted(day_tot)[-120:]:
            row = day_tot[d]
            row["label"] = d[5:]
            daily.append(row)
    out["daily"] = daily

    # ---- 近 24 小时（与所选范围无关，固定看最近一天） ----
    hstart = now - 23 * 3600
    hstart = hstart - (hstart % 3600)
    try:
        hrows = con.execute(
            "SELECT strftime('%H',ts,'unixepoch','localtime') hh, COUNT(*) n, "
            "SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(COALESCE(cache_hit_tokens,0)) h "
            "FROM usage_records WHERE ts >= ? AND ts <= ? GROUP BY hh",
            (hstart, now)).fetchall()
    except Exception:
        hrows = []
    hmap = {}
    for r in hrows:
        hmap[str(r["hh"])] = {"req": int(r["n"] or 0), "prompt": int(r["p"] or 0),
                              "completion": int(r["c"] or 0), "cache_hit": int(r["h"] or 0)}
    hourly = []
    for i in range(24):
        ts = hstart + i * 3600
        hh = time.strftime("%H", time.localtime(ts))
        row = dict(hmap.get(hh) or {"req": 0, "prompt": 0, "completion": 0, "cache_hit": 0})
        row["h"] = hh + ":00"
        row["total"] = row["prompt"] + row["completion"]
        row["cache_rate"] = round(row["cache_hit"] * 100.0 / row["prompt"], 1) if row["prompt"] else 0.0
        hourly.append(row)
    out["hourly"] = hourly

    # ---- 活跃天数 / 连续天数 ----
    # 活跃天数按「所选范围」算（今天就是 1 天，别显示成全周期 7 天）；
    # 连续天数按全周期算（今天没调用就看昨天往前）。
    try:
        in_range = con.execute(
            "SELECT COUNT(DISTINCT date(ts,'unixepoch','localtime')) FROM usage_records WHERE " + where,
            tuple(args)).fetchone()[0]
    except Exception:
        in_range = 0
    out["days"]["in_range"] = int(in_range or 0)
    try:
        days = [str(r[0]) for r in con.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM usage_records ORDER BY 1").fetchall()]
    except Exception:
        days = []
    out["days"]["active"] = len(days)
    if days:
        out["days"]["first"] = days[0]
        out["days"]["last"] = days[-1]
        allday = set(days)
        cur = _day_start(now, 0)
        if time.strftime("%Y-%m-%d", time.localtime(cur)) not in allday:
            cur = _day_start(now, 1)
        streak = 0
        while time.strftime("%Y-%m-%d", time.localtime(cur)) in allday and streak < 4000:
            streak += 1
            cur -= 86400
        out["days"]["streak"] = streak
    return out
