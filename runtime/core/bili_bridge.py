# -*- coding: utf-8 -*-
"""B站 bili-cli 的最小包装（刷视频用）。

- 独立 venv：/opt/dafeiyu/bili-venv
- 独立凭证目录：/home/dafeiyu/DafeiyuBot/bili（不碰大肥鱼配置）
- 只做读/写命令的 JSON 调用；失败返回 (False, {}, 人话原因)，不抛异常给聊天线程。
"""
import json
import os
import subprocess

from util import log, BOT_HOME

BILI_BIN = os.environ.get("DAFEIYU_BILI_BIN") or os.path.join(BOT_HOME, "tools", "bin", "bili")
BILI_HOME = os.environ.get("DAFEIYU_BILI_HOME") or os.path.join(BOT_HOME, "bili")
DEFAULT_TIMEOUT = 90


def available():
    return bool(BILI_BIN and os.path.exists(BILI_BIN))


def _err_text(data, fallback=""):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("hint") or fallback)
        if err:
            return str(err)
    return fallback


def run(args, timeout=DEFAULT_TIMEOUT):
    """执行一条 bili-cli，返回 (ok, data, err)。"""
    if not available():
        return False, {}, "bili-cli 没装"
    env = dict(os.environ)
    env["HOME"] = BOT_HOME
    env["BILI_CLI_HOME"] = BILI_HOME
    try:
        r = subprocess.run([BILI_BIN] + list(args), capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, {}, "bili-cli 超时"
    except Exception as e:
        return False, {}, "bili-cli 启动失败：%r" % (e,)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    data = {}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except Exception:
            continue
    if not data:
        return False, {}, (err or out or "bili-cli 没返回 JSON")[:300]
    if not data.get("ok"):
        return False, data, _err_text(data, "bili-cli 失败")[:300]
    return True, data.get("data") or {}, ""


def auth_status():
    return run(["auth", "status"], timeout=30)


def owner_show():
    return run(["owner", "show"], timeout=30)


def feed_recommend(count=20, page=1):
    return run(["feed", "recommend", "--count", str(max(1, min(30, int(count)))),
                "--page", str(max(1, int(page)))], timeout=60)


def search_video(keyword, order="new", size=20, page=1):
    kw = " ".join(str(keyword or "").split())[:40]
    if not kw:
        return False, {}, "关键词为空"
    return run(["search", "video", kw, "--order", str(order or "new"),
                "--page", str(max(1, int(page))), "--size", str(max(1, min(50, int(size))))],
               timeout=60)


def video_relation(bvid):
    return run(["video", "relation", str(bvid)], timeout=30)


def comment_post(target, text, at="owner", dry_run=False):
    args = ["comment", "post", str(target), str(text)]
    if at:
        args += ["--at", str(at)]
    if dry_run:
        args += ["--dry-run"]
    return run(args, timeout=60)


def comment_check(target, rpid):
    return run(["comment", "check", str(target), str(rpid)], timeout=45)


def comment_delete(target, rpid):
    return run(["comment", "delete", str(target), str(rpid)], timeout=45)


def comment_reply(target, rpid, text, root=None, at=None):
    args = ["comment", "reply", str(target), str(rpid), str(text)]
    if root:
        args += ["--root", str(root)]
    if at:
        args += ["--at", str(at)]
    return run(args, timeout=60)


def feed_replies(limit=20, cursor=None):
    args = ["feed", "replies", "--limit", str(max(1, min(100, int(limit))))]
    if cursor:
        args += ["--cursor", str(cursor)]
    return run(args, timeout=60)


def video_like(bvid, undo=False):
    args = ["video", "like", str(bvid)]
    if undo:
        args += ["--undo"]
    return run(args, timeout=45)


def video_fav(bvid, remove=False):
    args = ["video", "fav", str(bvid)]
    if remove:
        args += ["--remove"]
    return run(args, timeout=45)
