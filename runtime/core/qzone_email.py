# -*- coding: utf-8 -*-
"""QQ空间功能的邮件告警（只给主人看，不影响空间主流程）。

用途：空间发说说 / 评论轮询 / cookie 失效这类"后台出事没人看见"的情况，
发一封邮件到配置的邮箱，正文带 时间 / 版本号 / 主机名 / 异常摘要。

规矩：
- 没开（alert_email_enabled 假）或没配全（host/user/pass/mail_to 缺一个）→ 只记 log，
  返回 False，不抛异常。
- 发信走 smtplib.SMTP_SSL，10 秒超时；主题前缀【大肥鱼】。
- 节流：dedupe_key 非空时，同一个 key 1 小时内只发一封
  （时间戳存 store settings 的 "qzone:mail:<key>"）。
- 函数内 try/except 全包：邮件发不出去也绝不能影响空间线程。
"""

import platform
import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from util import log
from version import VERSION

# 同一个 dedupe_key 的静默期（秒）
DEDUPE_SEC = 3600
# SMTP 超时（秒），契约要求 10 秒
SMTP_TIMEOUT = 10


def _truthy(value):
    """配置里可能是 bool / "1" / "true" / "on"，都当开。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")


def _setting_get(store, key):
    """读 store 里的设置；store 坏了也不影响发信，返回 None。"""
    try:
        return store.get_setting(key)
    except Exception as e:
        log("qzone", "邮件节流读设置失败 %r" % (e,))
        return None


def _setting_set(store, key, value):
    """写 store 里的设置（节流时间戳）；失败只 log。"""
    try:
        store.set_setting(key, value)
    except Exception as e:
        log("qzone", "邮件节流写设置失败 %r" % (e,))


def _build_body(subject, body, dedupe_key=""):
    """拼正文：时间 / 版本 / 主机名 / 摘要，方便一眼定位是哪出的问题。"""
    lines = [
        "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "版本：v%s" % VERSION,
        "主机：%s" % platform.node(),
        "标题：%s" % str(subject or ""),
    ]
    if dedupe_key:
        lines.append("事件：%s" % str(dedupe_key))
    lines.append("-" * 40)
    lines.append(str(body or "").strip() or "（正文为空）")
    lines.append("")
    lines.append("-- 大肥鱼手机版空间告警，不用回复。")
    return "\n".join(lines)


def send_alert(cfg, store, subject, body, dedupe_key="", log_tag="qzone"):
    """发一封告警邮件；成功 True，没开/没配/被节流/失败都是 False（不抛异常）。"""
    tag = log_tag or "qzone"
    try:
        # 配置既可能是 dict，也可能是 Config 对象（有 .get 就行）。
        # v0.29.4 之前这里只认 dict，导致传 Config 时永远返回 False —— 所有邮件告警被静默吞掉。
        if cfg is None or not hasattr(cfg, "get"):
            log(tag, "邮件告警跳过：配置对象不可用")
            return False
        if not _truthy(cfg.get("alert_email_enabled")):
            log(tag, "邮件告警未开启，本次只记日志：%s" % (subject,))
            return False

        host = str(cfg.get("alert_smtp_host") or "").strip()
        user = str(cfg.get("alert_smtp_user") or "").strip()
        password = str(cfg.get("alert_smtp_pass") or "")
        mail_to = str(cfg.get("alert_mail_to") or "").strip()
        try:
            port = int(cfg.get("alert_smtp_port") or 465)
        except Exception:
            port = 465

        missing = []
        if not host:
            missing.append("alert_smtp_host")
        if not user:
            missing.append("alert_smtp_user")
        if not password:
            missing.append("alert_smtp_pass")
        if not mail_to:
            missing.append("alert_mail_to")
        if missing:
            log(tag, "邮件告警未配置，只记日志（缺 %s）：%s" % (", ".join(missing), subject))
            return False

        # ---- 节流：同一个 dedupe_key 一小时内只发一封 ----
        dedupe_key = str(dedupe_key or "").strip()
        setting_key = ("qzone:mail:" + dedupe_key) if dedupe_key else ""
        if setting_key:
            last_raw = _setting_get(store, setting_key)
            try:
                last_ts = int(float(last_raw or 0))
            except Exception:
                last_ts = 0
            now = int(time.time())
            if last_ts and now - last_ts < DEDUPE_SEC:
                log(tag, "邮件告警节流中（%d 秒内同一事件只发一封）：%s"
                    % (DEDUPE_SEC, subject))
                return False

        # ---- 发信 ----
        text = _build_body(subject, body, dedupe_key)
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header("【大肥鱼】" + str(subject or "空间告警"), "utf-8")
        msg["From"] = formataddr((str(Header("大肥鱼机器人", "utf-8")), user))
        msg["To"] = mail_to
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context) as smtp:
            smtp.login(user, password)
            smtp.sendmail(user, [addr.strip() for addr in mail_to.split(",") if addr.strip()],
                          msg.as_string())

        # 只有真发出去了才写节流时间戳；发失败下次还能重试
        if setting_key:
            _setting_set(store, setting_key, int(time.time()))
        log(tag, "邮件告警已发送 → %s：%s" % (mail_to, subject))
        try:
            store.add_log(tag, "邮件已发 → %s：%s" % (mail_to, subject), True)
        except Exception:
            pass
        return True
    except Exception as e:
        # 邮件通道本身出问题也绝不往上抛
        log(tag, "邮件告警失败 %r（%s）" % (e, subject))
        return False
