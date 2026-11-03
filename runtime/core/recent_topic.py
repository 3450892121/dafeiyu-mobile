# -*- coding: utf-8 -*-
"""Short-lived, conversation-scoped topic context; never issues messages."""
import json
import threading
import time

from store import _enc, _dec, _clean_name

WINDOW = 48 * 3600
MIN_REFRESH = 120


def _key(conv):
    return "recent_topic:" + str(conv)


def read(store, conv, now=None):
    now = int(time.time() if now is None else now)
    try:
        value = json.loads(_dec(store.get_setting(_key(conv)) or "") or "{}")
        start = max(int(store.get_setting("ctx_start:" + conv) or 0),
                    int(store.get_setting("recent_topic_start:" + conv) or 0))
        generation = int(store.get_setting("recent_topic_generation:" + conv) or 0)
        if (int(value.get("start") or 0) != start or
                int(value.get("generation") or 0) != generation or
                not 0 <= now - int(value.get("ts") or 0) <= WINDOW):
            return ""
        return str(value.get("text") or "")[:600]
    except (TypeError, ValueError, AttributeError):
        return ""


def schedule(bot, conv):
    """Refresh in background from this conversation's recent messages only."""
    now = int(time.time())
    try:
        raw = json.loads(_dec(bot.store.get_setting(_key(conv)) or "") or "{}")
        start = max(int(bot.store.get_setting("ctx_start:" + conv) or 0),
                    int(bot.store.get_setting("recent_topic_start:" + conv) or 0))
        generation = int(bot.store.get_setting("recent_topic_generation:" + conv) or 0)
        same = int(raw.get("start") or 0) == start and int(raw.get("generation") or 0) == generation
        last_id = int(raw.get("last_id") or 0) if same else 0
        if now - int(raw.get("ts") or 0) < MIN_REFRESH:
            return
        with bot.store._lock:
            rows = bot.store.conn.execute(
                "SELECT id FROM visible_messages WHERE conv=? AND id>? AND ts>=? ORDER BY id DESC LIMIT 50",
                (conv, max(start, last_id), now - WINDOW)).fetchall()
        if len(rows) < 4:
            return
        last = int(rows[0][0])
        # 近期话题只看最新一小段；长期前情由会话摘要和长期记忆负责。
        history = bot.store.messages_for_context(conv, 6, self_id=bot._self_qq(),
                                                 bot_names=bot._bot_names(), since=now - WINDOW, after_id=start)
        label = "群消息（方括号里是发言人）"
        if str(conv).startswith("p:"):
            uid = int(str(conv).split(":", 1)[1])
            label = (_clean_name(bot.store.person_name(uid), 20) or "私聊用户") + "(QQ%d)" % uid
        transcript = "\n".join(("本机器人（你）" if h.get("role") == "assistant" else label) + "：" +
                               str(h.get("content") or "")[:250] for h in history)[-10000:]
        if not transcript:
            return
        lock_key = "recent_topic:" + conv
        with bot.summarizing_lock:
            if lock_key in bot.summarizing:
                return
            bot.summarizing.add(lock_key)

        def run():
            try:
                summary = bot.pool.summarize_recent_topic(transcript)
                current_start = max(int(bot.store.get_setting("ctx_start:" + conv) or 0),
                                    int(bot.store.get_setting("recent_topic_start:" + conv) or 0))
                if (summary and current_start == start and
                        int(bot.store.get_setting("recent_topic_generation:" + conv) or 0) == generation):
                    value = {"ts": now, "start": start, "generation": generation,
                             "last_id": last, "text": str(summary)[:600]}
                    bot.store.set_setting(_key(conv), _enc(json.dumps(value, ensure_ascii=False)))
            except Exception:
                pass
            finally:
                with bot.summarizing_lock:
                    bot.summarizing.discard(lock_key)
        threading.Thread(target=run, name="recent-topic", daemon=True).start()
    except Exception:
        return
