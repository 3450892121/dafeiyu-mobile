"""Prefer less-used alternatives within a relevant band, retaining raw relevance."""
import json, os, random


def file_name(value):
    return str(value or '').replace('\\', '/').rsplit('/', 1)[-1]


def usage_counts(usage):
    allc, recent = usage or ({}, {})
    def clean(counts):
        out = {}
        for key, value in (counts or {}).items():
            name = file_name(key)
            if name:
                out[name] = out.get(name, 0) + max(0, int(value or 0))
        return out
    return clean(allc), clean(recent)


def rank(candidates, usage=None, limit=6, min_score=0.0, seed=None):
    """A fresh image gets a chance only if its semantic score is close enough.

    Return original scores so a frequently used, relevant picture does not fail
    the relevance gate merely because its diversity rank is lower.
    """
    limit = max(1, int(limit))
    unique = {}
    for score, item in candidates:
        name = file_name(item.get('file'))
        if name and float(score) >= min_score and (name not in unique or score > unique[name][0]):
            unique[name] = (float(score), item)
    if not unique:
        return []
    best = max(score for score, _ in unique.values())
    close = [x for x in unique.values() if x[0] >= max(min_score, best*.80)]
    rng = random.Random(seed)
    rng.shuffle(close)
    allc, recent = usage_counts(usage)
    def counts(row):
        name = file_name(row[1].get('file'))
        return allc.get(name, 0), recent.get(name, 0)
    def adjusted(row):
        u7, u24 = counts(row)
        return row[0]/(1+.08*u7+.45*u24)
    ranked = sorted(close, key=adjusted, reverse=True)
    # Reserve opportunities for underused relevant images in the actual panel,
    # rather than appending them beyond the displayed first six candidates.
    fresh = sorted(close, key=lambda row: (counts(row)[1], counts(row)[0], -row[0]))
    out, names = [], set()
    def add(row):
        name = file_name(row[1].get('file'))
        if name not in names and len(out) < limit:
            out.append(row); names.add(name)
    for row in ranked[:max(1, limit//3)]:
        add(row)
    for row in fresh[:max(1, limit//3)]:
        add(row)
    # Keep the strongest semantic match available to the visual picker too.
    add(max(close, key=lambda row: row[0]))
    for row in ranked:
        add(row)
    return out


def cached_aliases(data_dir):
    """Read existing fingerprints only; never decode the whole library in a reply."""
    try:
        with open(os.path.join(data_dir, 'stickers', 'sticker_hash.json'), encoding='utf8') as fh:
            hashes = json.load(fh)
        groups = {}
        for name, fp in hashes.items():
            if isinstance(fp, dict):
                key = fp.get('pixels') or fp.get('sha1')
                if key:
                    groups.setdefault(key, set()).add(name)
        return {name: group for group in groups.values() for name in group}
    except Exception:
        return {}
