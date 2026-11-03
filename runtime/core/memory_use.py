"""Conservative textual mention evidence, never count retrieval as actual use."""
import re
import threading
import time

local = threading.local()


def begin():
    local.candidates = []


def candidate(table, item):
    rows = getattr(local, 'candidates', None)
    if rows is not None:
        rows.append((table, int(item['id']), str(item.get('text') or '')))


def finish(store, candidates, conv, mid, reply):
    # An exact distinctive fragment is evidence of mention, not proof of indirect reasoning.
    plain = re.sub(r'\s+', '', reply)
    for table, ident, text in candidates:
        fragments = re.findall(r'[\u4e00-\u9fffA-Za-z0-9]{6,}', text)
        if any(any(f[i:i+6] in plain for i in range(len(f)-5)) for f in fragments):
            store.record_memory_mention(table, ident, conv, mid)


def may_topup(meta, now=None):
    now = int(now or time.time())
    important = meta.get('protected') or meta.get('kind') == 'commitment' or int(meta.get('importance') or 0)>=4
    recent_event = meta.get('kind') == 'event' and now-int(meta.get('content_updated_at') or meta.get('created') or 0) < 48*3600
    if not important and not recent_event:
        return False
    # Storage permanence and unsolicited mention eligibility are separate decisions.
    return now-int(meta.get('last_mentioned_at') or 0)>=86400 and now-int(meta.get('last_background_at') or 0)>=1800
