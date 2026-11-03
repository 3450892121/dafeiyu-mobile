import json, os, random


class Stickers(object):
    """591 张表情包：按标签/备注召回，发本地图片文件。"""

    def __init__(self, meta_path, images_dir):
        self.items = []
        self.meta_path = meta_path
        self._mtime = 0
        self.images_dir = images_dir
        self.reload()

    def reload(self):
        """只加载一次；过滤已删除状态和不存在的文件，避免选到空路径。"""
        try:
            exists = os.path.exists(self.meta_path)
            mt = os.path.getmtime(self.meta_path) if exists else 0
            if mt == getattr(self, "_mtime", 0):
                return
            items = []
            if exists:
                try:
                    with open(self.meta_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                row = json.loads(line)
                            except Exception:
                                continue
                            if row.get("status") not in (None, "ok"):
                                continue
                            name = os.path.basename(str(row.get("file") or "").replace("\\", "/"))
                            if not name:
                                continue
                            if not os.path.isfile(os.path.join(self.images_dir, name)):
                                continue
                            row["file"] = name
                            items.append(row)
                except Exception:
                    return
            self.items = items
            self._mtime = mt
        except Exception:
            pass

    def __len__(self):
        self.reload()
        return len(self.items)

    def path(self, item):
        # 只允许 images_dir 下的文件名，避免元数据里塞绝对路径或 ..\ 逃逸
        name = os.path.basename(str(item.get("file") or "").replace("\\", "/"))
        return os.path.join(self.images_dir, name)

    def _score(self, item, words):
        text = " ".join(item.get("tags") or []) + " " + (item.get("note") or "") + " " + (item.get("scene") or "")
        s = 0
        for w in words:
            if w and w in text:
                s += 3 if w in (item.get("tags") or []) else 1
        return s

    def pick(self, keyword=None, exclude=None):
        if not self.items:
            return None
        exclude = exclude or set()
        words = []
        if keyword:
            words = [w for w in str(keyword).replace("，", ",").replace("、", ",").split(",") if w.strip()]
            words = [w.strip() for w in words]
        # 同义词小表
        syn = {"开心": ["开心", "高兴", "笑", "乐"], "无语": ["无语", "无奈", "沉默"], "生气": ["生气", "怒", "凶"],
               "可爱": ["可爱", "萌", "撒娇"], "晚安": ["晚安", "睡", "困"], "累": ["累", "困", "躺"],
               "委屈": ["委屈", "可怜", "哭"], "撒娇": ["撒娇", "贴贴", "抱"], "得意": ["得意", "嚣张", "笑"]}
        expanded = []
        for w in words:
            expanded.extend(syn.get(w, [w]))
        cands = [it for it in self.items if it.get("file") not in exclude]
        if not cands:
            cands = list(self.items)
        if expanded:
            scored = [(self._score(it, expanded), it) for it in cands]
            best = max(s for s, _ in scored)
            if best > 0:
                top = [it for s, it in scored if s == best]
                return random.choice(top)
        weights = [1.0 / (1.0 + max(0, int(it.get("uses") or 0))) ** .5 for it in cands]
        return random.choices(cands, weights=weights, k=1)[0]
