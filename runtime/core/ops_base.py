# -*- coding: utf-8 -*-
"""运维页后端（v0.53.0）：自检 / 告警记录 / 机器人清单 / 人设读写。

设计原则（给不懂代码的主人用）：
- 面板只调这里，不碰任何危险动作；所有写操作都先备份、限大小、限白名单。
- 三类数据来源：
  1) 机器人清单 <shared>/bots.json（唯一事实源，加机器人只改它）
  2) 探针结果 <shared>/health.json + <shared>/alerts.jsonl（deploy/health_probe.py、alert.py 写）
  3) 人设文件 每只机器人自己的 appassets/persona_*.txt
- 没有第 2 项时退回内置轻量检查（服务/端口/文件），页面照样能用。
"""
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

PERSONA_FILES = [
    ("persona_owner.txt", "主人版人设", "私聊主人时用，越像本人越好"),
    ("persona_group.txt", "群聊版人设", "对群友/普通好友时用，可只留公开部分"),
    ("persona_identity.txt", "身份卡（谁是我）", "名字、形象、自称、忌讳；换一只机器人就换这段"),
    ("persona_pc_note.txt", "补充说明", "服务器/电脑版额外规则，可以留空"),
    ("persona_phone_note.txt", "手机版补充", "只有 pc 补充为空时才用，一般不用改"),
    ("persona_qzone.txt", "QQ空间人设", "发说说/回评论时的口吻，可留空"),
]
SELF_FILE = ("persona_self.txt", "当前自我资料（所有场合共用）", "只记录身份和生活背景；性格、爱好与观点在真实相处中形成，不在这里规定终点")
FILE_LABEL = {n: (l, h) for n, l, h in PERSONA_FILES + [SELF_FILE]}
MAX_PERSONA_BYTES = 200 * 1024
MAX_BACKUPS = 5

DEFAULT_BOTS = []

CHECKS_LABEL = {
    "service": "服务在跑", "port": "端口在听", "process": "进程在", "onebot": "QQ 收发通道",
    "persona": "人设文件可读", "recent": "最近有活动",
}


def _now_text(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _run(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace"), (p.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return 127, "", str(e)


class OpsApi(object):
    def __init__(self, shared_data, project_root, cfg=None, fallback_persona_dir=None, deploy_dir=""):
        self.shared = os.path.abspath(shared_data or ".")
        self.project_root = project_root or ""
        self.cfg = cfg or {}
        self.deploy_dir = str(deploy_dir or "")
        self.fallback_persona = fallback_persona_dir or (
            os.path.join(self.project_root, "appassets") if self.project_root else "")
        self.registry_path = os.path.join(self.shared, "bots.json")
        self.health_path = os.path.join(self.shared, "health.json")
        self.alerts_path = os.path.join(self.shared, "alerts.jsonl")
        self.backup_root = os.path.join(self.shared, "persona_backups")
        self._persona_lock = threading.RLock()

    # ---------------- 机器人清单 ----------------
    def bots(self):
        """读机器人户口本（bots.json v2）——统一走 bot_registry，顺带完成旧格式迁移。

        合法空清单保持空；坏清单报告错误，不猜测或恢复默认账号。
        """
        items = []
        self.registry_error = ""
        try:
            from bot_registry import BotRegistry
            items = BotRegistry(self.shared).bots()
        except Exception as e:
            self.registry_error = str(e)[:160]
            items = []
        out = []
        for i, b in enumerate(items):
            if not isinstance(b, dict):
                continue
            item = dict(b)
            item.setdefault("id", "bot-%d" % (i + 1))
            item.setdefault("name", item["id"])
            item.setdefault("channel", "napcat")
            item.setdefault("channel_label", "QQ 小号（协议端）" if item["channel"] == "napcat" else "QQ 官方机器人")
            item.setdefault("panel", "")
            item.setdefault("ports", [])
            if not item.get("persona_dir"):
                item["persona_dir"] = os.path.join(self.fallback_persona) if self.fallback_persona else ""
            out.append(item)
        return out

    def _bot(self, bot_id):
        for b in self.bots():
            if str(b.get("id")) == str(bot_id):
                return b
        return None

    # ---------------- 健康/告警 ----------------
    def health(self):
        d = _read_json(self.health_path)
        return d if isinstance(d, dict) else {}

    def alerts(self, limit=40):
        out = []
        try:
            with open(self.alerts_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    # 邮件通道测试（面板点「发测试邮件」）不算故障，别混进告警列表吓人
                    if isinstance(rec, dict) and str(rec.get("kind")) == "mail_test":
                        continue
                    out.append(rec)
        except Exception:
            return []
        return out[-int(limit):][::-1]

    def _daily(self):
        """近 24 小时的活动小结：只读每只机器人自己的 bot.db。"""
        since = int(time.time()) - 86400
        res = {"messages_in": 0, "messages_out": 0, "errors": 0, "bots": []}
        for b in self.bots():
            row = {"id": b.get("id"), "name": b.get("name"), "in": 0, "out": 0,
                   "last_ts": 0, "last_text": "", "errors": 0}
            dbp = os.path.join(str(b.get("data_dir") or ""), "bot.db")
            if b.get("data_dir") and os.path.isfile(dbp):
                try:
                    import sqlite3
                    c = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True, timeout=3)
                    c.row_factory = sqlite3.Row
                    try:
                        r = c.execute("SELECT COUNT(*) c FROM messages WHERE ts>=? AND direction='in'", (since,)).fetchone()
                        row["in"] = int(r["c"] if r else 0)
                        r = c.execute("SELECT COUNT(*) c FROM messages WHERE ts>=? AND direction='out'", (since,)).fetchone()
                        row["out"] = int(r["c"] if r else 0)
                        r = c.execute("SELECT ts,text,direction FROM messages ORDER BY id DESC LIMIT 1").fetchone()
                        if r:
                            row["last_ts"] = int(r["ts"] or 0)
                            row["last_text"] = str(r["text"] or "")[:60]
                        try:
                            r = c.execute("SELECT COUNT(*) c FROM logs WHERE ts>=? AND (text LIKE '%失败%' OR text LIKE '%error%' OR text LIKE '%异常%')", (since,)).fetchone()
                            row["errors"] = int(r["c"] if r else 0)
                        except Exception:
                            pass
                    finally:
                        c.close()
                except Exception as e:
                    row["error"] = str(e)[:120]
            res["bots"].append(row)
            res["messages_in"] += row["in"]
            res["messages_out"] += row["out"]
            res["errors"] += row["errors"]
        return res

    def _log_restarts(self):
        """近 24 小时看门狗重启次数（日志文件在就数，不在就算 0）。"""
        p = os.path.join(self.shared, "watchdog.log")
        n = 0
        cut = time.time() - 86400
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f.readlines()[-4000:]:
                    if "连续两次异常" in line or "执行重启" in line:
                        try:
                            ts = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            continue
                        if ts >= cut:
                            n += 1
        except Exception:
            pass
        return n

    def _live_checks(self, b):
        """内置轻量检查（探针脚本没装时兜底）。"""
        checks = {}
        svc = str(b.get("service") or "")
        if svc:
            rc, out, _ = _run(["systemctl", "is-active", svc], timeout=5)
            checks["service"] = (out.strip() == "active")
        for p in (b.get("ports") or [])[:2]:
            checks["port:%s" % p] = _port_open(p)
        pd = str(b.get("persona_dir") or "")
        filename = SELF_FILE[0] if self._unified_persona(b) else "persona_owner.txt"
        checks["persona"] = bool(pd and os.path.isfile(os.path.join(pd, filename)))
        return checks

    def state(self):
        bots = self.bots()
        h = self.health()
        hbots = {}
        for x in (h.get("bots") or []):
            if isinstance(x, dict) and x.get("id"):
                hbots[str(x["id"])] = x
        out = []
        for b in bots:
            hb = hbots.get(str(b.get("id"))) or {}
            checks = hb.get("checks")
            if not isinstance(checks, dict) or not checks:
                checks = self._live_checks(b)
                hb = dict(hb)
                hb["checks"] = checks
                hb["live_fallback"] = True
            # v0.53.0 补：官方通道没有 OneBot get_status，探针只能给 unknown；
            # 直接标成「不适用」，别在面板上显示成红色的 ✗（那不是故障）。
            checks = dict(checks)
            if str(checks.get("onebot") or "").lower() in ("unknown", "", "n/a", "none"):
                if str(b.get("channel")) != "napcat" and "onebot" in checks:
                    checks["onebot"] = "n/a"
            hb = dict(hb)
            hb["checks"] = checks
            probs = hb.get("problems") or []
            ok = hb.get("ok")
            if ok is None:
                ok = all(bool(v) for v in checks.values()) and not probs
            item = dict(b)
            item["health"] = {"ok": bool(ok), "checks": checks, "problems": probs,
                              "onebot": hb.get("onebot", ""), "ts": hb.get("ts", 0),
                              "last_msg_age_s": hb.get("last_msg_age_s")}
            out.append(item)
        daily = self._daily()
        alerts = self.alerts(40)
        return {
            "ok": not bool(getattr(self, "registry_error", "")),
            "error": str(getattr(self, "registry_error", "")),
            "registry_error": str(getattr(self, "registry_error", "")),
            "ts": int(time.time()),
            "now": _now_text(),
            "registry_path": self.registry_path,
            "registry_default": not os.path.isfile(self.registry_path),
            "probe_installed": self._probe_path() != "",
            "health_ts": h.get("ts", 0),
            "health_time": _now_text(h.get("ts")) if h.get("ts") else "",
            "bots": out,
            "alerts": alerts,
            "daily": daily,
            "restarts_24h": self._log_restarts(),
            "checks_label": CHECKS_LABEL,
            "persona_files": [{"name": n, "label": l, "hint": hh} for n, l, hh in PERSONA_FILES],
            "mail": self.mail(),
        }

    # ---------------- 邮件通道 ----------------
    def _cfg_get(self, key, default=""):
        try:
            v = self.cfg.get(key)
        except Exception:
            v = None
        return default if v is None else v

    def mail(self):
        """告警邮件通道状态。只判断「配没配」，不回显授权码。"""
        enabled = bool(self._cfg_get("alert_email_enabled", False))
        host = str(self._cfg_get("alert_smtp_host", "") or "")
        user = str(self._cfg_get("alert_smtp_user", "") or "")
        pwd = str(self._cfg_get("alert_smtp_pass", "") or "")
        to = str(self._cfg_get("alert_mail_to", "") or "")
        missing = []
        if not host:
            missing.append("SMTP 服务器")
        if not user:
            missing.append("SMTP 账号")
        if not pwd:
            missing.append("SMTP 授权码")
        if not to:
            missing.append("收件邮箱")
        ready = bool(enabled and not missing)
        hint = ("已配置，出问题会发到这个邮箱：" + to) if ready else (
            "还没配好（缺：" + "、".join(missing) + "）" if missing else
            "邮件告警开关关着（设置页「程序报错时发邮件给我」没勾）")
        if not enabled and missing:
            hint = "开关关着，且缺：" + "、".join(missing)
        return {"enabled": enabled, "ready": ready, "to": to, "hint": hint}

    def mail_test(self):
        m = self.mail()
        if not m["ready"]:
            return {"ok": False, "error": "邮件通道没配好：" + m["hint"]}
        script = os.path.join(self.deploy_dir, "alert.py") if self.deploy_dir else ""
        if not script or not os.path.isfile(script):
            script = ""
        if not os.path.isfile(script):
            return {"ok": False, "error": "服务器上找不到 alert.py（还没部署？）"}
        cmd = [sys.executable or "python3", script,
               "--bot", "运维中心", "--bot-id", "mail_test", "--kind", "mail_test",
               "--problem", "邮件通道测试（你在面板点的，不是真故障）",
               "--detail", "收到这封说明告警邮件能到你邮箱；以后只有真出问题才会发。",
               "--force"]
        rc, out, err = _run(cmd, timeout=60)
        text = (out or err or "").strip()[-500:]
        if rc != 0:
            return {"ok": False, "error": "发送失败：" + (text or ("退出码 %d" % rc))}
        return {"ok": True, "note": "已发送，请到邮箱（含垃圾箱）看一眼。", "detail": text}

    # ---------------- 自检 ----------------
    def _probe_path(self):
        deploy_dir = self.deploy_dir
        cand = []
        if deploy_dir:
            cand.append(os.path.join(str(deploy_dir), "health_probe.py"))
        # No external server probe fallback in the independent mobile instance.
        return next((p for p in cand if os.path.isfile(p)), "")

    def selftest(self):
        script = self._probe_path()
        if not script:
            # 没有探针脚本：只跑内置检查，页面照样能看
            bots = self.bots()
            res = []
            for b in bots:
                checks = self._live_checks(b)
                res.append({"id": b.get("id"), "name": b.get("name"),
                            "ok": all(bool(v) for v in checks.values()), "checks": checks, "problems": []})
            return {"ok": True, "mode": "builtin", "ts": int(time.time()),
                    "result": {"overall": "ok" if all(x["ok"] for x in res) else "warn", "bots": res},
                    "note": "服务器还没装健康探针（health_probe.py），这次只查了服务/端口/人设文件。"}
        # 探针正常跑一遍（写 health.json，异常时它自己决定要不要告警），然后读回结果
        rc, out, err = _run([sys.executable or "python3", script], timeout=60)
        h = self.health()
        if h and int(h.get("ts") or 0) >= int(time.time()) - 300:
            return {"ok": True, "mode": "probe", "ts": int(time.time()), "result": h}
        tail = ((out or "") + "\n" + (err or "")).strip()[-400:]
        return {"ok": False, "mode": "probe",
                "error": ("探针没写出结果（退出码 %s）：%s" % (rc, tail))[:500]}

    # ---------------- 人设 ----------------
    @staticmethod
    def _unified_persona(bot):
        # Registry identity, never a display name or the panel currently opened.
        return bool(bot and str(bot.get("id")) == "dafeiyu" and bot.get("channel") == "napcat")

    def _persona_files(self, bot_id):
        b = self._bot(bot_id)
        return [SELF_FILE] if self._unified_persona(b) else PERSONA_FILES

    def _persona_dir(self, bot_id):
        b = self._bot(bot_id)
        if not b:
            return ""
        return str(b.get("persona_dir") or "")

    def _safe_name(self, name, bot_id):
        name = str(name or "")
        if not self._bot(bot_id) or name != os.path.basename(name) or "/" in name or "\\" in name:
            return ""
        return name if any(name == item[0] for item in self._persona_files(bot_id)) else ""

    @staticmethod
    def _regular_path(path, allow_missing=False):
        """Do not let an editable file or backup escape through a symlink."""
        path = os.path.abspath(path)
        parent = os.path.dirname(path)
        while parent and parent != os.path.dirname(parent):
            if os.path.islink(parent):
                raise ValueError("目录不能是符号链接")
            parent = os.path.dirname(parent)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if not stat.S_ISREG(mode):
            raise ValueError("只允许普通文件")

    def _history_items(self, bot_id, name):
        hd = os.path.join(self.backup_root, str(bot_id))
        out = []
        try:
            for n in sorted(os.listdir(hd), reverse=True):
                if not n.endswith("-" + name):
                    continue
                p = os.path.join(hd, n)
                self._regular_path(p)
                out.append({"file": n, "name": name, "size": os.path.getsize(p),
                            "time_text": _now_text(os.path.getmtime(p))})
        except FileNotFoundError:
            pass
        return out[:MAX_BACKUPS]

    def persona_list(self, bot_id):
        b = self._bot(bot_id)
        if not b:
            return {"ok": False, "error": "没有这只机器人"}
        pd = self._persona_dir(bot_id)
        items = []
        for name, label, hint in self._persona_files(bot_id):
            fp = os.path.join(pd, name)
            try:
                st = os.stat(fp)
                size, mt = st.st_size, int(st.st_mtime)
            except Exception:
                size, mt = 0, 0
            items.append({"name": name, "label": label, "hint": hint,
                          "exists": size > 0, "size": size,
                          "mtime": mt, "mtime_text": _now_text(mt) if mt else "没有这个文件"})
        hist = []
        for item in items:
            hist.extend(self._history_items(bot_id, item["name"]))
        unified = self._unified_persona(b)
        return {"ok": True, "bot": {"id": b.get("id"), "name": b.get("name"),
                                    "channel_label": b.get("channel_label"),
                                    "persona_dir": pd},
                "files": items, "history": hist,
                "mode": "unified" if unified else "legacy",
                "current_file": SELF_FILE[0] if unified else "persona_owner.txt",
                "notice": ("私聊、群聊共用这一份自我资料。本实例关系和称呼自主已统一；性格、爱好与观点来自真实相处和成长记录，"
                           "不在这里预先写死。旧分版模板原文保留，不再参与当前回复；保存或回滚本页不会清空记忆和共同经历。"
                           if unified else "这只机器人继续使用自己的既有人设设置。"),
                "growth_panel": "大肥鱼面板 → 记忆 → 相处中长成的样子" if unified else ""}

    def persona_get(self, bot_id, name):
        name = self._safe_name(name, bot_id)
        if not name:
            return {"ok": False, "error": "只能改人设文件"}
        pd = self._persona_dir(bot_id)
        if not pd:
            return {"ok": False, "error": "这只机器人没有配置人设目录"}
        fp = os.path.join(pd, name)
        try:
            self._regular_path(fp, allow_missing=True)
            with open(fp, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        except Exception as e:
            return {"ok": False, "error": "读不出来：%s" % str(e)[:120]}
        return {"ok": True, "name": name, "label": FILE_LABEL[name][0], "content": text,
                "size": len(text.encode("utf-8")), "history": self._history_items(bot_id, name)}

    def _backup_dir(self, bot_id):
        d = os.path.join(self.backup_root, str(bot_id))
        self._regular_path(os.path.join(d, ".check"), allow_missing=True)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(self.backup_root, 0o700)
        os.chmod(d, 0o700)
        return d

    def persona_save(self, bot_id, name, content, who=""):
        with self._persona_lock:
            return self._persona_save(bot_id, name, content, who)

    def _persona_save(self, bot_id, name, content, who=""):
        name = self._safe_name(name, bot_id)
        if not name:
            return {"ok": False, "error": "只能改人设文件"}
        pd = self._persona_dir(bot_id)
        if not pd or not os.path.isdir(pd):
            return {"ok": False, "error": "人设目录不存在：%s" % pd}
        text = str(content or "").replace("\r\n", "\n")
        raw = text.encode("utf-8")
        if len(raw) > MAX_PERSONA_BYTES:
            return {"ok": False, "error": "太长了（%d 字，最多 %d）" % (len(raw), MAX_PERSONA_BYTES)}
        fp = os.path.join(pd, name)
        backup = ""
        tmp = fp + ".tmp-" + uuid.uuid4().hex
        try:
            self._regular_path(fp, allow_missing=True)
            original_stat = os.stat(fp) if os.path.isfile(fp) else None
            if original_stat is not None:
                with open(fp, "rb") as f:
                    old = f.read()
                bd = self._backup_dir(bot_id)
                backup = os.path.join(bd, time.strftime("%Y%m%d-%H%M%S-") + str(time.time_ns()) + "-" + name)
                with open(backup, "xb") as f:
                    f.write(old)
                os.chmod(backup, 0o600)
                files = sorted([os.path.join(bd, x) for x in os.listdir(bd) if x.endswith("-" + name)],
                               key=lambda p: os.path.getmtime(p), reverse=True)
                for old_p in files[MAX_BACKUPS:]:
                    self._regular_path(old_p)
                    os.remove(old_p)
            with open(tmp, "x", encoding="utf-8", newline="\n") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            if original_stat is not None and hasattr(os, "chown"):
                os.chown(tmp, original_stat.st_uid, original_stat.st_gid)
            os.chmod(tmp, (stat.S_IMODE(original_stat.st_mode) & ~0o022) if original_stat else 0o644)
            os.replace(tmp, fp)
        except Exception as e:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            return {"ok": False, "error": "保存失败，当前资料未覆盖：%s" % str(e)[:120]}
        return {"ok": True, "file": name, "size": len(raw), "backup": os.path.basename(backup) if backup else "",
                "note": "已保存，下一条消息会重新读取当前资料；已有记忆和成长记录保留。" + ("旧版已备份，可回滚。" if backup else "")}

    def persona_history(self, bot_id, name=""):
        if not self._bot(bot_id):
            return {"ok": False, "error": "没有这只机器人"}
        if name and not self._safe_name(name, bot_id):
            return {"ok": False, "error": "只能读取当前可编辑资料的备份"}
        out = []
        for filename, _, _ in self._persona_files(bot_id):
            if not name or filename == name:
                out.extend(self._history_items(bot_id, filename))
        return {"ok": True, "items": out[:30]}

    def persona_restore(self, bot_id, name, backup_file):
        name = self._safe_name(name, bot_id)
        bf = str(backup_file or "")
        if not name or bf != os.path.basename(bf) or "/" in bf or "\\" in bf or not bf.endswith("-" + name):
            return {"ok": False, "error": "文件名不对"}
        src = os.path.join(self.backup_root, str(bot_id), bf)
        try:
            with self._persona_lock:
                self._regular_path(src)
                with open(src, "r", encoding="utf-8") as f:
                    content = f.read(MAX_PERSONA_BYTES + 1)
                result = self._persona_save(bot_id, name, content)
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}
        if result.get("ok"):
            result["note"] = "已回滚当前资料，下一条消息会重新读取；记忆和成长记录保留，回滚前资料已备份。"
        return result

    # ---------------- 机器人清单维护 ----------------
    def bot_add(self, name, channel, account, note=""):
        return {"ok": False, "code": "legacy_registry_write_disabled",
                "error": "请到总控台顶部的机器人页添加；旧登记入口已停用。",
                "management_url": "http://127.0.0.1:18991/"}

    def bot_remove(self, bot_id):
        return {"ok": False, "code": "legacy_registry_write_disabled",
                "error": "请到总控台顶部的机器人页删除或停用；旧移除入口已停用。",
                "management_url": "http://127.0.0.1:18991/"}
