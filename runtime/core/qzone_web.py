# -*- coding: utf-8 -*-
"""QQ空间「cookie 通道」：拿 NapCat 的 Cookie 直接打空间网页接口。

接口来源
--------
1. 说说列表 / 说说详情 / 回复评论 / 点赞：第三方开源插件
   https://github.com/Zhalslar/astrbot_plugin_qzone 的 core/qzone/api.py
   （LIST_URL / DETAIL_URL / COMMENT_URL / DOLIKE_URL，参数照抄）。
2. 删除评论 emotion_cgi_delcomment_ugc：
   https://github.com/cube-lover/astrbot_qzone_auto_like 的 qzone_comment.py。
   **注意**：上面那个插件里没有删评论接口（它只支持删自己的说说）；这个地址
   来自另一个开源实现，是浏览器表单里能看到的参数，但本项目没实测过。
   哪天确定不能用了，就把 delete_comment 直接改成 return False，不要再编地址。

现状（重要）
------------
这些接口**都没有在本项目实测**，只是对照开源实现写的。QQ空间随时可能改参数、
加风控、要求额外签名。所以本模块的铁律是：
- 所有公开方法失败一律返回空值（[] / {} / False）并 log，**绝不抛异常**；
  调用方（flow_qzone）按空结果处理，不会因为空间接口抽风把线程搞崩。
- g_tk 放 URL query；业务参数放 form body；Referer 固定
  https://user.qzone.qq.com/<uin>/main （契约第 2 节）。
- 解析一律宽松：JSON / JSONP 都能吃，键名在多个候选里找，找不到就返回空，
  不做任何"看起来像就编一个"的兜底。
- 只用标准库 urllib，不引第三方依赖。
"""

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from util import log

TAG = "qzone_web"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

LIST_URL = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
            "/cgi-bin/emotion_cgi_msglist_v6")
DETAIL_URL = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
              "/cgi-bin/emotion_cgi_msgdetail_v6")
# 契约第 2 节给的回复地址（h5 代理）
REPLY_URL = ("https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
             "/cgi-bin/emotion_cgi_re_feeds")
# 点赞：168 行那个开源插件里的地址，契约里标"可选，默认不用"
LIKE_URL = ("https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
            "/cgi-bin/likes/internal_dolike_app")
# 删评论：开源实现里的地址，未实测（见模块注释）
DEL_COMMENT_URL = ("https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
                   "/cgi-bin/emotion_cgi_delcomment_ugc")


# ------------------------- 纯函数小工具（不抛异常） -------------------------

def _cookie_value(cookies, name):
    """从 "a=b; c=d" 里取某个 cookie 值，取不到返回空串。"""
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part.split("=", 1)[1].strip()
    return ""


def _gtk_from_cookie(cookies):
    """没给 bkn 时，按 p_skey/skey 自己算 g_tk（和空间网页一致的 djb2 变体）。"""
    for key in ("p_skey", "skey", "media_p_skey"):
        value = _cookie_value(cookies, key)
        if not value:
            continue
        h = 5381
        for ch in value:
            h += (h << 5) + ord(ch)
        return str(h & 0x7FFFFFFF)
    return ""


def _to_int(value, default=0):
    """宽松转 int：数字、数字字符串都认，其它给 default。"""
    try:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return default
        if re.match(r"^-?\d+$", text):
            return int(text)
        return default
    except Exception:
        return default


def _to_str(value):
    """宽松转 str：None/异常一律空串。"""
    try:
        if value is None:
            return ""
        return str(value)
    except Exception:
        return ""


def _slice_json(text, start):
    """从 start 位置切出一段括号配平的 JSON（字符串里的括号不算）。"""
    if start < 0 or start >= len(text) or text[start] not in "{[":
        return ""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _loads(text):
    """宽松 JSON 解析：JSON / JSONP（callback({...})）都能吃；失败返回 None。"""
    try:
        raw = str(text or "").strip()
        if not raw:
            return None
        if raw[0] in "{[":
            try:
                return json.loads(raw)
            except Exception:
                pass
        # JSONP：找到第一个 { 或 [，切出配平的一段再解析
        positions = [p for p in (raw.find("{"), raw.find("[")) if p >= 0]
        if not positions:
            return None
        chunk = _slice_json(raw, min(positions))
        if not chunk:
            return None
        chunk = re.sub(r"\bundefined\b", "null", chunk)
        try:
            return json.loads(chunk)
        except Exception:
            return None
    except Exception:
        return None


def _walk(obj):
    """递归产出所有 dict / list 节点（只看结构，不认键名）。"""
    stack = [obj]
    seen = 0
    while stack and seen < 20000:
        seen += 1
        node = stack.pop()
        yield node
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append(value)


def _looks_like_msg(node):
    """说说节点的判定：有 tid，并且有 created_time 或 commentlist（评论节点没有这两样）。"""
    return (isinstance(node, dict) and "tid" in node
            and ("created_time" in node or "commentlist" in node))


def _collect_msgs(obj):
    """从任意结构的响应里捞出所有说说节点（去重，保持出现顺序）。"""
    out = []
    keys = set()
    for node in _walk(obj):
        if not _looks_like_msg(node):
            continue
        key = _to_str(node.get("tid")) or _to_str(id(node))
        if key in keys:
            continue
        keys.add(key)
        out.append(node)
    return out


def _find_commentlist(obj):
    """整棵树里找第一个像评论列表的东西（detail 兜底用）。"""
    for node in _walk(obj):
        if isinstance(node, dict):
            for name in ("commentlist", "comments", "comment_list"):
                value = node.get(name)
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
    return []


class QzoneWeb(object):
    """QQ空间 cookie 通道。所有方法失败返回空值 + log，不抛异常。"""

    def __init__(self, cookies, bkn, uin, timeout=30):
        """cookies 是 "a=b; c=d" 字符串；bkn 是 NapCat 给的 g_tk；uin 是小号 QQ。"""
        self.cookies = _to_str(cookies).strip()
        self.uin = _to_str(uin).strip()
        self.bkn = _to_str(bkn).strip()
        try:
            self.timeout = float(timeout or 30)
        except Exception:
            self.timeout = 30.0
        # 没传 bkn 就从 cookie 里的 p_skey/skey 自己算；都算不出来就保持空（方法会直接返回空）
        if not self.bkn:
            self.bkn = _gtk_from_cookie(self.cookies)

    # ---------------------------- HTTP 底座 ----------------------------

    def _referer(self):
        return "https://user.qzone.qq.com/%s/main" % (self.uin or "")

    def _headers(self, extra=None):
        headers = {
            "User-Agent": UA,
            "Cookie": self.cookies,
            "Referer": self._referer(),
            "Origin": "https://user.qzone.qq.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if extra:
            headers.update(extra)
        return headers

    def _fetch(self, method, url, params=None, data=None):
        """发一次请求，返回响应文本；任何失败返回空串（并 log），绝不抛异常。"""
        try:
            full = url
            if params:
                full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
            if str(method).upper() == "POST":
                body = urllib.parse.urlencode(data or {}, doseq=True).encode("utf-8")
                headers = self._headers({"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
                req = urllib.request.Request(full, data=body, headers=headers, method="POST")
            else:
                req = urllib.request.Request(full, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(2 * 1024 * 1024)
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:
                detail = ""
            log(TAG, "HTTP %s %s (%s)" % (e.code, detail[:200].replace("\n", " "), url))
            return ""
        except Exception as e:
            log(TAG, "请求失败 %r (%s)" % (e, url))
            return ""

    def _result_ok(self, name, text, extra=""):
        """统一判断写操作结果：code/ret 为 0 才算成功。不抛异常。"""
        try:
            if not text:
                log(TAG, "%s 失败%s：响应为空" % (name, (" " + extra) if extra else ""))
                return False
            obj = _loads(text)
            if isinstance(obj, dict):
                for key in ("code", "ret", "result"):
                    if key in obj:
                        ok = _to_int(obj.get(key), -999) == 0
                        log(TAG, "%s %s%s code=%s" % (name, "成功" if ok else "失败",
                                                      (" " + extra) if extra else "", obj.get(key)))
                        return ok
            # 非标准 JSON（比如 format=fs 的裸响应）：退化成看 "code":0 / code:0
            if re.search(r'["\x27]?(?:code|ret)["\x27]?\s*:\s*0\b', str(text)):
                log(TAG, "%s 成功%s（文本判码）" % (name, (" " + extra) if extra else ""))
                return True
            head = str(text)[:200].replace("\n", " ")
            log(TAG, "%s 失败%s：%s" % (name, (" " + extra) if extra else "", head))
            return False
        except Exception as e:
            log(TAG, "%s 判定异常 %r" % (name, e))
            return False

    @staticmethod
    def _rand():
        """和网页表单一致的防缓存随机串。"""
        return str(int(time.time() * 1000)) + str(random.randint(100, 999))

    def _ready(self, name):
        """发请求前的自检：没有 uin / bkn 就直接返回 False（不联网）。"""
        if not self.uin or not self.bkn:
            log(TAG, "%s 跳过：缺少 uin 或 bkn" % name)
            return False
        return True

    # ---------------------------- 读：列表 / 详情 ----------------------------

    def list_feeds(self, pos=0, num=5):
        """拉自己的说说列表，返回 [{"tid","content","created","comment_count","like_count"}]。"""
        out = []
        try:
            if not self._ready("list_feeds"):
                return out
            params = {
                "g_tk": self.bkn,
                "uin": self.uin,
                "ftype": 0,
                "sort": 0,
                "pos": _to_int(pos, 0),
                "num": _to_int(num, 5) or 5,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            }
            text = self._fetch("GET", LIST_URL, params=params)
            obj = _loads(text)
            if obj is None:
                log(TAG, "list_feeds 解析失败，头 200 字：%s" % str(text)[:200].replace("\n", " "))
                return out
            msgs = _collect_msgs(obj)
            for msg in msgs[:(_to_int(num, 5) or 5)]:
                comments = msg.get("commentlist")
                out.append({
                    "tid": _to_str(msg.get("tid")),
                    "content": _to_str(msg.get("content")).strip(),
                    "created": _to_int(msg.get("created_time"), 0),
                    "comment_count": self._comment_count(msg, comments),
                    "like_count": self._like_count(msg),
                })
            log(TAG, "list_feeds 取到 %d 条说说" % len(out))
        except Exception as e:
            log(TAG, "list_feeds 异常 %r" % (e,))
            return []
        return out

    @staticmethod
    def _comment_count(msg, comments):
        """评论数：先看接口给的计数键，没有再数评论列表。"""
        for key in ("comment_count", "commentnum", "cmtnum", "commentCnt"):
            value = _to_int(msg.get(key), -1)
            if value >= 0:
                return value
        if isinstance(comments, list):
            return len(comments)
        return 0

    @staticmethod
    def _like_count(msg):
        """点赞数：键名各版本不一致，挨个试；认不出就当 0。"""
        for key in ("like_count", "likenum", "likeCount", "praise", "like", "likes"):
            value = msg.get(key)
            if isinstance(value, list):
                return len(value)
            num = _to_int(value, -1)
            if num >= 0:
                return num
        return 0

    def get_detail(self, tid):
        """拉单条说说详情（含完整评论），返回契约里的 dict；失败返回 {}。"""
        try:
            tid = _to_str(tid).strip()
            if not tid:
                log(TAG, "get_detail 跳过：tid 为空")
                return {}
            if not self._ready("get_detail"):
                return {}
            params = {
                "uin": self.uin,
                "tid": tid,
                "format": "jsonp",
                "g_tk": self.bkn,
            }
            text = self._fetch("GET", DETAIL_URL, params=params)
            obj = _loads(text)
            if obj is None:
                log(TAG, "get_detail 解析失败 tid=%s 头 200 字：%s" % (tid, str(text)[:200].replace("\n", " ")))
                return {}
            msg = None
            for item in _collect_msgs(obj):
                if _to_str(item.get("tid")) == tid:
                    msg = item
                    break
            if msg is None:
                msgs = _collect_msgs(obj)
                msg = msgs[0] if msgs else None
            if msg is None:
                # 兜底：有的版本把评论列表挂在别处
                comments = _find_commentlist(obj)
                if not comments:
                    log(TAG, "get_detail 没找到说说 tid=%s" % tid)
                    return {}
                log(TAG, "get_detail 只找到评论列表 tid=%s（说说正文缺失）" % tid)
                return {"tid": tid, "content": "", "created": 0,
                        "comments": self._parse_comments(comments, tid)}
            commentlist = msg.get("commentlist")
            if not isinstance(commentlist, list):
                commentlist = _find_commentlist(obj)
            return {
                "tid": _to_str(msg.get("tid")) or tid,
                "content": _to_str(msg.get("content")).strip(),
                "created": _to_int(msg.get("created_time"), 0),
                "comments": self._parse_comments(commentlist, tid),
            }
        except Exception as e:
            log(TAG, "get_detail 异常 %r" % (e,))
            return {}

    def _comment_item(self, raw, post_tid="", reply_to_key="", reply_to_name=""):
        """把一条原始评论整理成契约格式；键名宽松找。"""
        if not isinstance(raw, dict):
            return None
        created = 0
        for key in ("create_time", "created_time", "createTime", "time"):
            created = _to_int(raw.get(key), 0)
            if created:
                break
        raw_key = _to_str(raw.get("tid")) or _to_str(raw.get("commentid")) \
            or _to_str(raw.get("comment_id")) or _to_str(raw.get("id"))
        author_uin = _to_str(raw.get("uin")) or _to_str(raw.get("opuin"))
        comment_key = raw_key
        if not comment_key:
            # 接口有时不给评论 id：用 作者+时间+长度 拼个稳定 key（store 里是 UNIQUE）
            comment_key = "%s_%s_%s" % (author_uin, created, len(_to_str(raw.get("content"))))
        if post_tid:
            # 2026-10-26 修：接口给评论的 tid 只是「这个帖子下第几条」（1、2、3…），跨帖会重复。
            # 直接当唯一键的话，新评论会撞上别的帖子下的旧评论，被当成「已经回过了」静默跳过
            # （10-25 主人评论「何意味」就是这么丢的）。comment_key 必须带上帖子 tid。
            comment_key = "%s|%s" % (str(post_tid or "")[:60], comment_key)
        item = {
            "comment_key": comment_key,
            "comment_id": raw_key or comment_key,
            "author_uin": author_uin,
            "author_name": _to_str(raw.get("name")) or _to_str(raw.get("nickname")),
            "is_self": 1 if (author_uin and author_uin == self.uin) else 0,
            "reply_to_key": reply_to_key,
            "reply_to_name": reply_to_name,
            "content": _to_str(raw.get("content")),
            "created": created,
        }
        return item

    def _parse_comments(self, commentlist, post_tid=""):
        """主评论 + 楼中楼（list_3）拍平成一维列表。失败返回 []。"""
        out = []
        try:
            if not isinstance(commentlist, list):
                return out
            for main in commentlist:
                if not isinstance(main, dict):
                    continue
                item = self._comment_item(main, post_tid=post_tid)
                if item:
                    out.append(item)
                # 楼中楼：键名各版本不同，挨个试
                subs = None
                for key in ("list_3", "list", "sub_commentlist", "comments"):
                    value = main.get(key)
                    if isinstance(value, list):
                        subs = value
                        break
                if not subs:
                    continue
                parent_key = item["comment_key"] if item else ""
                parent_name = item["author_name"] if item else ""
                for sub in subs:
                    sub_item = self._comment_item(sub, post_tid=post_tid, reply_to_key=parent_key, reply_to_name=parent_name)
                    if sub_item:
                        out.append(sub_item)
        except Exception as e:
            log(TAG, "_parse_comments 异常 %r" % (e,))
            return []
        return out

    # ---------------------------- 写：回复 / 删评论 / 点赞 ----------------------------

    def reply_comment(self, tid, content, reply_comment_id=None, reply_uin=None):
        """在说说 tid 下评论；给了 reply_comment_id 就按楼中楼回复带齐楼层参数。"""
        try:
            tid = _to_str(tid).strip()
            content = _to_str(content).strip()
            if not tid or not content:
                log(TAG, "reply_comment 跳过：tid 或正文为空")
                return False
            if not self._ready("reply_comment"):
                return False
            data = {
                "topicId": "%s_%s__1" % (self.uin, tid),
                "uin": self.uin,
                "hostUin": self.uin,
                "feedsType": "100",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": "52",
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "paramstr": "1",
                "qzreferrer": self._referer(),
                "rand": self._rand(),
            }
            comment_id = _to_str(reply_comment_id).strip()
            if comment_id:
                target_uin = _to_str(reply_uin).strip() or self.uin
                # PC 代理要求不带 __1、format=json，这里为了兼容两套写法把楼层参数都带上
                data.update({
                    "commentId": comment_id,
                    "commentUin": target_uin,
                    "t1_uin": self.uin,
                    "t1_tid": tid,
                    "t2_uin": target_uin,
                    "t2_tid": comment_id,
                })
            text = self._fetch("POST", REPLY_URL, params={"g_tk": self.bkn}, data=data)
            return self._result_ok("reply_comment", text, "tid=%s" % tid)
        except Exception as e:
            log(TAG, "reply_comment 异常 %r" % (e,))
            return False

    def delete_comment(self, tid, comment_id):
        """删自己说说下面的评论。

        走的是开源实现里的 emotion_cgi_delcomment_ugc（未实测）：官方那套插件
        里没有删评论接口，这个地址是从另一个项目抄来的。宁可返回 False 也不编地址；
        失败时调用方按"没删成"处理即可。
        注：楼中楼的 comment_id 用的是子评论自己的 tid，删楼中楼【没有】验证过；
        最稳妥的用法是只删主评论（reply_to_key 为空的那条）。
        """
        try:
            tid = _to_str(tid).strip()
            comment_id = _to_str(comment_id).strip()
            if not tid or not comment_id:
                log(TAG, "delete_comment 跳过：tid 或 comment_id 为空")
                return False
            if not self._ready("delete_comment"):
                return False
            data = {
                "g_tk": self.bkn,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "hostUin": self.uin,
                "uin": self.uin,
                "topicId": "%s_%s__1" % (self.uin, tid),
                "feedsType": "100",
                "commentId": comment_id,
                "commentUin": self.uin,
                "format": "fs",
                "ref": "feeds",
                "paramstr": "1",
                "qzreferrer": self._referer(),
                "rand": self._rand(),
            }
            text = self._fetch("POST", DEL_COMMENT_URL, params={"g_tk": self.bkn}, data=data)
            return self._result_ok("delete_comment", text, "tid=%s comment=%s" % (tid, comment_id))
        except Exception as e:
            log(TAG, "delete_comment 异常 %r" % (e,))
            return False

    def like(self, tid):
        """给说说点赞（契约里标"可选，默认不用"）。来源：开源插件，但它自己 README 写了
        "接口显示成功，但实测点赞无效"，所以这个返回值只代表接口回包成功。"""
        try:
            tid = _to_str(tid).strip()
            if not tid:
                log(TAG, "like 跳过：tid 为空")
                return False
            if not self._ready("like"):
                return False
            # unikey/curkey 必须是 http://（https 时 QQ 可能回成功但不记赞）
            unikey = "http://user.qzone.qq.com/%s/mood/%s" % (self.uin, tid)
            data = {
                "qzreferrer": self._referer(),
                "opuin": self.uin,
                "unikey": unikey,
                "curkey": unikey,
                "appid": 311,
                "from": 1,
                "typeid": 0,
                "abstime": int(time.time()),
                "fid": tid,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            }
            text = self._fetch("POST", LIKE_URL, params={"g_tk": self.bkn}, data=data)
            return self._result_ok("like", text, "tid=%s" % tid)
        except Exception as e:
            log(TAG, "like 异常 %r" % (e,))
            return False
