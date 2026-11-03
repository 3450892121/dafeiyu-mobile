import re

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ark-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def allow_outbound(text, api_key="", keys=None):
    """出站前的最后一道闸：不把密钥类字符串发出去。返回 (ok, text)。"""
    if not text:
        return False, ""
    secrets = []
    if isinstance(keys, str):
        keys = [keys]
    for key in ([api_key] + list(keys or [])):
        key = str(key or "").strip()
        if key and key not in secrets:
            secrets.append(key)
    for key in secrets:
        if key in text:
            return False, ""
    for p in _SECRET_PATTERNS:
        if p.search(text):
            return False, ""
    return True, text