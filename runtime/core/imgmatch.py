# -*- coding: utf-8 -*-
"""图片感知哈希：缩放/另存/转发过的同一张图也能认出来。

64 位 dHash（8x8 差分）。索引跟控制台「表情包上传查重」共用
data/stickers/sticker_hash.json，不另外维护一套；控制台补新图后这边按 mtime 自动重载。
"""
import io
import json
import os
import threading

_lock = threading.Lock()
_cache = {"path": "", "mtime": 0, "map": {}}


def dhash_bytes(data):
    """图片字节 -> 16 位十六进制 dHash；解不开返回空串。"""
    if not data:
        return ""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("L").resize((9, 8), Image.LANCZOS)
            px = im.tobytes()
    except Exception:
        return ""
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return "%016x" % bits


def dhash_of(path):
    try:
        with open(path, "rb") as f:
            return dhash_bytes(f.read())
    except Exception:
        return ""


def ham(a, b):
    """两个 dHash 的汉明距离；格式不对返回 99（当作不同图）。"""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 99


def index_path(sticker_dir):
    return os.path.join(sticker_dir, "sticker_hash.json")


def load_index(sticker_dir):
    """file -> dHash。只读控制台维护的哈希表；文件变了自动重载。"""
    path = index_path(sticker_dir)
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {}
    with _lock:
        if _cache["path"] == path and _cache["mtime"] == mtime:
            return _cache["map"]
    try:
        raw = json.load(open(path, "r", encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    if isinstance(raw, dict):
        for name, v in raw.items():
            if isinstance(v, dict) and v.get("dhash"):
                out[str(name)] = str(v["dhash"])
    with _lock:
        _cache.update({"path": path, "mtime": mtime, "map": out})
    return out


def similar_files(dhash_hex, index, max_distance=6, limit=8):
    """返回视觉上跟 dhash_hex 是同一张的图库文件（按距离近到远）。"""
    if not dhash_hex or not index:
        return []
    try:
        max_distance = max(0, int(max_distance))
    except Exception:
        max_distance = 6
    scored = []
    for name, h in index.items():
        d = ham(dhash_hex, h)
        if d <= max_distance:
            scored.append((d, name))
    scored.sort()
    try:
        limit = max(1, int(limit or 8))
    except Exception:
        limit = 8
    return [name for _, name in scored[:limit]]
