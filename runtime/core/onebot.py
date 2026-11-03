import errno, json, os, urllib.error, urllib.parse, urllib.request


class OneBotError(Exception):
    pass


def is_connection_refused(error):
    """Only a typed transport refusal proves the HTTP request was not sent."""
    pending, seen, refused = [error], set(), False
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            return False  # An HTTP response is not a refused TCP connection.
        if isinstance(current, OSError) and not isinstance(current, urllib.error.URLError):
            if not (isinstance(current, ConnectionRefusedError) or current.errno in (errno.ECONNREFUSED, 10061)
                    or getattr(current, "winerror", None) == 10061):
                return False
            refused = True
        pending.extend((current.__cause__, current.__context__, getattr(current, "reason", None)))
    return refused


def file_uri(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + urllib.parse.quote(p, safe="/:")


def text_seg(text):
    return {"type": "text", "data": {"text": text}}


def image_seg(path):
    return {"type": "image", "data": {"file": file_uri(path)}}


def face_seg(face_id):
    """QQ 自带表情（小黄脸/超级表情）消息段；NapCat 按 id 自己补动画参数。"""
    return {"type": "face", "data": {"id": str(int(face_id))}}


def at_seg(qq):
    return {"type": "at", "data": {"qq": str(qq)}}


def record_seg(path):
    """v0.67.0 语音条。NapCat 认本地绝对路径，自己转格式（wav/mp3 都吃）。"""
    return {"type": "record", "data": {"file": os.path.abspath(path)}}


class OneBot(object):
    """最小 OneBot 11 HTTP 客户端（NapCat 兼容）。"""

    def __init__(self, api_base, token="", timeout=15):
        self.api_base = (api_base or "http://127.0.0.1:3000").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def call(self, action, timeout=None, **params):
        deny_user = getattr(self, "deny_user", None)
        if deny_user and action.startswith(("send_", "upload_")) and deny_user(params.get("user_id")):
            raise OneBotError("recipient is blocked")
        # timeout 只影响这一次请求；不传就继续用实例的 self.timeout（老调用不受影响）
        tp = timeout if timeout else self.timeout
        url = self.api_base + "/" + action
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=tp) as resp:
                raw = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace")
            raise OneBotError("HTTP %s %s" % (e.code, detail))
        except Exception as e:
            raise OneBotError("%r" % (e,))
        try:
            obj = json.loads(raw)
        except Exception:
            raise OneBotError("bad json: " + raw[:200])
        rc = obj.get("retcode")
        ok_rc = (0, 1) if action.startswith("send_") else (0,)
        if obj.get("status") == "failed" or rc not in ok_rc:
            raise OneBotError("retcode=%s %s" % (rc, obj.get("wording") or obj.get("message") or ""))
        return obj.get("data")

    def get_login_info(self):
        return self.call("get_login_info")

    def send_private(self, user_id, segments):
        return self.call("send_private_msg", user_id=int(user_id), message=segments)

    def send_group(self, group_id, segments):
        return self.call("send_group_msg", group_id=int(group_id), message=segments)

    def get_friend_list(self):
        return self.call("get_friend_list")

    def upload_private_file(self, user_id, path, name):
        return self.call("upload_private_file", user_id=int(user_id), file=str(path), name=str(name))

    def upload_group_file(self, group_id, path, name):
        return self.call("upload_group_file", group_id=int(group_id), file=str(path), name=str(name))

    def get_image(self, file):
        return self.call("get_image", file=file)

    def get_msg(self, message_id):
        return self.call("get_msg", message_id=message_id)

    def get_friend_msg_history(self, user_id, count=20):
        return self.call("get_friend_msg_history", user_id=int(user_id), count=int(count))

    def get_group_msg_history(self, group_id, count=20, message_seq=None):
        """v0.55.0：支持 message_seq 往前翻页（6 小时窗口补拉用）。"""
        params = {"group_id": int(group_id), "count": int(count)}
        if message_seq:
            params["message_seq"] = int(message_seq)
        return self.call("get_group_msg_history", **params)

    # ---- v0.29.0 QQ空间（NapCat 自带的 qzone 扩展接口）----

    def send_qzone(self, content, images=None, ugc_right=1, timeout=60):
        """发一条说说，返回 tid；拿不到 tid 返回空串（发说说比聊天慢，默认超时放宽到 60 秒）。"""
        data = self.call(
            "send_qzone_msg",
            timeout=timeout,
            content=content,
            ugc_right=int(ugc_right),
            **({"images": images} if images else {})
        )
        return (data or {}).get("tid", "")

    def delete_qzone(self, tid, timeout=30):
        """删除自己发的一条说说（tid 来自 send_qzone 或 cookie 通道）。"""
        self.call("delete_qzone_msg", timeout=timeout, tid=str(tid))

    def get_qzone_cookies(self, timeout=30):
        """取 user.qzone.qq.com 域的 Cookie，形如 {"cookies": "a=b; c=d", "bkn": "123"}；取不到返回 {}。"""
        data = self.call("get_cookies", timeout=timeout, domain="user.qzone.qq.com")
        return data if isinstance(data, dict) else {}
