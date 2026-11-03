import hashlib, json, math, os, struct, time, urllib.request, urllib.error


def _cfg_key(cfg):
    """取向量用的 Key：全局没配就用第一个火山模型的 Key（服务器版 Key 是按模型存的）。"""
    try:
        k = str(cfg.get("api_key") or "").strip()
        if k:
            return k
        models = cfg.get("models") or []
        for m in models:
            if "volces.com" in str(m.get("base_url") or "") and m.get("api_key"):
                return str(m.get("api_key"))
        for m in models:
            if m.get("api_key"):
                return str(m.get("api_key"))
    except Exception:
        pass
    return ""


class StickerIndex(object):
    """表情包图像向量索引：用方舟 doubao-embedding-vision 把图片和文字放到同一空间，按语义找图。"""

    def __init__(self, data_dir, cfg, kind="text"):
        self.cfg = cfg
        self.kind = kind
        self.dir = os.path.join(data_dir, "stickers")
        if kind == "cards":
            self.meta_path = os.path.join(self.dir, "sticker_vectors_cards.json")
            self.bin_path = os.path.join(self.dir, "sticker_vectors_cards.bin")
        else:
            self.meta_path = os.path.join(self.dir, "sticker_vectors.json")
            self.bin_path = os.path.join(self.dir, "sticker_vectors.bin")
        self.model = str(cfg.get("sticker_embed_model") or "doubao-embedding-vision-250515")
        self.url = "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings"
        self.files = []
        self.dim = 0
        self.norms = []
        self.raw = b""
        self.ok = False
        self._mtime = 0
        self.load()

    def maybe_reload(self):
        """控制台重建索引后，大脑不用重启就能用上新索引。"""
        try:
            mt = max(os.path.getmtime(self.meta_path), os.path.getmtime(self.bin_path))
        except Exception:
            return
        if mt != self._mtime:
            self.load()

    def load(self):
        try:
            meta = json.load(open(self.meta_path, "r", encoding="utf-8"))
            self.files = list(meta.get("files") or [])
            self.dim = int(meta.get("dim") or 2048)
            self.raw = open(self.bin_path, "rb").read()
            step = self.dim * 4
            if len(self.raw) < step * len(self.files):
                raise ValueError("vector file size mismatch")
            norms = []
            for i in range(len(self.files)):
                v = struct.unpack_from("<%df" % self.dim, self.raw, i * step)
                norms.append(math.sqrt(sum(x * x for x in v)) or 1.0)
            self.norms = norms
            self.ok = bool(self.files) and self.dim > 0
            try:
                self._mtime = max(os.path.getmtime(self.meta_path), os.path.getmtime(self.bin_path))
            except Exception:
                self._mtime = 0
        except Exception:
            self.ok = False


    def _embed_text(self, text):
        key = _cfg_key(self.cfg)
        if not key:
            return None
        body = json.dumps({"model": self.model, "input": [str(text)[:1000]]}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + key,
        })
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    obj = json.loads(resp.read().decode("utf-8", "replace"))
                data = obj.get("data") or []
                if not data:
                    return None
                return data[0].get("embedding") or None
            except Exception:
                if attempt == 1:
                    time.sleep(1.0)
        return None

    def search(self, text, top_k=8):
        if not self.ok:
            return []
        q = self._embed_text(text)
        if not q:
            return []
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        step = self.dim * 4
        out = []
        for i, f in enumerate(self.files):
            v = struct.unpack_from("<%df" % self.dim, self.raw, i * step)
            dot = 0.0
            for a, b in zip(v, q):
                dot += a * b
            out.append((dot / (self.norms[i] * qn), f))
        out.sort(reverse=True)
        return out[:max(1, int(top_k))]

    def has(self, file):
        return file in self.files


def _post_embeddings(url, model, key, inputs, timeout=90):
    body = json.dumps({"model": model, "input": inputs}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + key,
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            data = obj.get("data") or []
            return [d.get("embedding") or [] for d in data]
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < 2:
                time.sleep(2 + attempt * 2)
                continue
            raise
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(1.0)
                continue
            raise
    if last:
        raise last
    return []


def build_cards_index(data_dir, cfg, progress=None):
    """v0.41.0：按「画面 + 图上文字 + 标签 + 备注」重建卡片索引（服务器版用 _cfg_key）。"""
    from sticker_cards import load_cards, card_text

    st = os.path.join(data_dir, "stickers")
    meta_path = os.path.join(st, "stickers.jsonl")
    cache_path = os.path.join(st, "sticker_cards_build.json")
    bin_path = os.path.join(st, "sticker_vectors_cards.bin")
    out_meta = os.path.join(st, "sticker_vectors_cards.json")
    model = str(cfg.get("sticker_embed_model") or "doubao-embedding-vision-250515")
    url = "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings"
    key = _cfg_key(cfg)
    if not key:
        return {"ok": False, "error": "缺少 API Key"}

    cards = load_cards(data_dir)
    items = []
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return {"ok": False, "error": "没有 stickers.jsonl"}

    texts = {}
    for m in items:
        f = str(m.get("file") or "")
        if not f:
            continue
        # v0.55.8：图已经删了（回收站/手动删）就别再进索引，否则会出现
        # 「语义索引 552 条」但库里只有 550 张。
        if not os.path.exists(os.path.join(st, "images", f)):
            continue
        card = cards.get(f) or {}
        parts = [card_text(card), "、".join(m.get("tags") or []), m.get("note") or "", m.get("scene") or ""]
        t = "。".join(p for p in parts if p)[:600]
        if not t:
            continue
        texts[f] = (t, hashlib.sha1(t.encode("utf-8")).hexdigest()[:16])

    have = {}
    if os.path.exists(cache_path):
        try:
            have = json.load(open(cache_path, "r", encoding="utf-8"))
        except Exception:
            have = {}
    todo = [f for f in texts if texts[f][1] not in have]
    total = len(texts)
    done = total - len(todo)
    if progress:
        progress(done, total, "开始")
    i = 0
    while i < len(todo):
        batch = todo[i:i + 10]
        try:
            vecs = _post_embeddings(url, model, key, [texts[f][0] for f in batch])
            if len(vecs) != len(batch):
                raise RuntimeError("返回数量不对")
            for f, v in zip(batch, vecs):
                if v:
                    have[texts[f][1]] = [round(x, 5) for x in v]
                    done += 1
        except Exception:
            for f in batch:
                try:
                    v = _post_embeddings(url, model, key, [texts[f][0]])[0]
                    have[texts[f][1]] = [round(x, 5) for x in v]
                    done += 1
                except Exception:
                    pass
        i += len(batch)
        if i % 50 == 0:
            try:
                json.dump(have, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
            if progress:
                progress(done, total, "编码中")
    try:
        json.dump(have, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    order = [f for f in texts if texts[f][1] in have and len(have[texts[f][1]]) == 2048]
    if not order:
        return {"ok": False, "error": "没有生成任何向量"}
    buf = bytearray()
    for f in order:
        buf.extend(struct.pack("<2048f", *have[texts[f][1]]))
    tmp_bin, tmp_meta = bin_path + ".tmp", out_meta + ".tmp"
    with open(tmp_bin, "wb") as fh:
        fh.write(buf)
    with open(tmp_meta, "w", encoding="utf-8") as fh:
        json.dump({"model": model, "dim": 2048, "kind": "cards", "files": order}, fh, ensure_ascii=False)
    os.replace(tmp_bin, bin_path)
    os.replace(tmp_meta, out_meta)
    if progress:
        progress(done, total, "完成")
    return {"ok": True, "indexed": len(order), "total": total}


def build_text_index(data_dir, cfg, progress=None):
    """按「标签+备注+场景」重建表情包向量索引；可重复执行，已有的不重复花钱。"""
    st = os.path.join(data_dir, "stickers")
    meta_path = os.path.join(st, "stickers.jsonl")
    cache_path = os.path.join(st, "sticker_text_build.json")
    bin_path = os.path.join(st, "sticker_vectors.bin")
    out_meta = os.path.join(st, "sticker_vectors.json")
    model = str(cfg.get("sticker_embed_model") or "doubao-embedding-vision-250515")
    url = "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings"
    key = _cfg_key(cfg)
    if not key:
        return {"ok": False, "error": "缺少 API Key"}

    items = []
    with open(meta_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    texts = {}
    for m in items:
        f = str(m.get("file") or "")
        if not f:
            continue
        # v0.55.8：图已经删了（回收站/手动删）就别再进索引，否则会出现
        # 「语义索引 552 条」但库里只有 550 张。
        if not os.path.exists(os.path.join(st, "images", f)):
            continue
        parts = ["、".join(m.get("tags") or []), m.get("note") or "", m.get("scene") or ""]
        t = "。".join(p for p in parts if p)[:300]
        if t:
            texts[f] = t

    have = {}
    if os.path.exists(cache_path):
        try:
            have = json.load(open(cache_path, "r", encoding="utf-8"))
        except Exception:
            have = {}
    todo = [f for f in texts if f not in have]
    total = len(texts)
    done = len(have)
    if progress:
        progress(done, total, "开始")
    i = 0
    while i < len(todo):
        batch = todo[i:i + 10]
        try:
            vecs = _post_embeddings(url, model, key, [texts[f] for f in batch])
            if len(vecs) != len(batch):
                raise RuntimeError("返回数量不对 %d/%d" % (len(vecs), len(batch)))
            for f, v in zip(batch, vecs):
                if v:
                    have[f] = [round(x, 5) for x in v]
                    done += 1
        except Exception as e:
            if len(batch) > 1:
                # 一批失败就退成单条，避免整批重来
                for f in batch:
                    try:
                        v = _post_embeddings(url, model, key, [texts[f]])[0]
                        have[f] = [round(x, 5) for x in v]
                        done += 1
                    except Exception:
                        pass
            else:
                _ = e
        i += len(batch)
        if i % 30 == 0:
            try:
                json.dump(have, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
            if progress:
                progress(done, total, "编码中")

    try:
        json.dump(have, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    order = [f for f in texts if f in have and len(have[f]) == 2048]
    if not order:
        return {"ok": False, "error": "没有生成任何向量"}
    buf = bytearray()
    for f in order:
        buf.extend(struct.pack("<2048f", *have[f]))
    tmp_bin = bin_path + ".tmp"
    tmp_meta = out_meta + ".tmp"
    with open(tmp_bin, "wb") as fh:
        fh.write(buf)
    with open(tmp_meta, "w", encoding="utf-8") as fh:
        json.dump({"model": model, "dim": 2048, "kind": "text", "files": order}, fh, ensure_ascii=False)
    os.replace(tmp_bin, bin_path)
    os.replace(tmp_meta, out_meta)
    if progress:
        progress(done, total, "完成")
    return {"ok": True, "indexed": len(order), "total": total, "skipped": total - len(order)}


def embed_texts(cfg, texts):
    """通用文本向量（记忆/表情包共用）；失败返回空列表，调用方回退关键词。"""
    key = _cfg_key(cfg)
    if not key or not texts:
        return []
    model = str(cfg.get("sticker_embed_model") or "doubao-embedding-vision-250515")
    url = "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings"
    out = []
    for i in range(0, len(texts), 10):
        batch = [str(t)[:800] for t in texts[i:i + 10]]
        try:
            vecs = _post_embeddings(url, model, key, batch)
        except Exception:
            return []
        if len(vecs) != len(batch):
            return []
        out.extend(vecs)
    return out
