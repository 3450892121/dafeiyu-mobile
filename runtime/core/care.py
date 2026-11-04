# -*- coding: utf-8 -*-
"""v0.56.0 轻生话题：不接梗、不激将、把人拉回来；每只机器人的口气各自保留。

只做三件事：
1. 认出来（SELF_HARM_RE）；
2. 给一句固定话术（每只 4 句轮换，绝不出现死亡/跳楼/天台/房梁这些词）；
3. 给这个会话打 10 分钟“低攻击”标记，期间不许损人、不许激将。
"""
import hashlib
import re
import time

SELF_HARM_RE = re.compile(
    r"(不活了|不想活|活不下去|想死|去死|死了算了|自杀|割腕|跳楼|要跳|天台上?|房梁|上吊|轻生|寻死|遗书)")

# 每只机器人自己的口气：凶/软/卖萌都可以，但不能顺着“死”说，也不能激将。
LINES = {
    "蓝色大肥鱼": [
        "少来这套。你死了谁陪我吵架？先把手机放下，喝口水。",
        "拿这个吓我，本鲸不吃。今天先睡觉，明天再来跟我横。",
        "别扯这些。你要真出事，我连个使唤的人都没了。先去吃饭。",
        "行行行，我听见了。先坐好、喘口气，等会儿再慢慢说，别干傻事。",
    ],
    "缇宝": [
        "别这样说呀，我听着难受。先把手机放下，喝点热水，跟我讲讲怎么了。",
        "你要是出事了，我找谁说话去呀。先别做傻事，我在呢。",
        "这会儿先别一个人扛，深呼吸，喝口水，等会儿再跟我说说。",
        "我不走，你也别走。先坐下来，慢慢讲。",
    ],
    "哦鲸鲸": [
        "少拿死吓我。你要真敢，我第一个不答应。先把手机放下，喝口水。",
        "这种事别跟我玩梗。你出事我找谁吵架？先去吃饭睡觉。",
        "行，我凶你，但没让你去死。坐下，把话说清楚，别干傻事。",
        "别拿命当筹码，没劲。先活着，回头再吵。",
    ],
}

# 未明确选择既有角色时不借用另一实例的身份或口气。
DEFAULT_LINES = [
    "听到你这样说，我很担心你。先到安全一点的地方，找一位信任的人陪着你，好吗？",
    "你不用一个人扛着。可以先联系身边信任的人，让他陪你一起度过这会儿。",
    "我在听。眼下先照顾好自己的安全，把可能伤到你的东西放远一些，再慢慢说。",
    "谢谢你把这些告诉我。先找个人陪在身边，你愿意说说刚才发生了什么吗？",
]

LOW_ATTACK_MIN = 10
LOW_ATTACK_NOTE = (
    "\n\n# 现在先别激他（重要）\n"
    "对方刚才说过轻生的话。这 10 分钟里：不许损他、不许嘲讽、不许激将、"
    "不许接“死/天台/房梁”这类梗；关心两句，把话题带回吃饭、睡觉、喝口水。"
)


def lines_for(bot_name):
    name = str(bot_name or "")
    for key in LINES:
        if key in name:
            return LINES[key]
    return DEFAULT_LINES


def care_line(bot_name, conv=""):
    """按群/会话轮换，不连续重复同一句。"""
    arr = lines_for(bot_name)
    try:
        seed = int(hashlib.md5(str(conv or "").encode("utf-8")).hexdigest()[:6], 16)
    except Exception:
        seed = 0
    return arr[seed % len(arr)]


def mark_low_attack(store, conv, minutes=LOW_ATTACK_MIN):
    try:
        store.set_setting("care:until:%s" % conv, str(int(time.time()) + int(minutes) * 60))
    except Exception:
        pass


def low_attack_active(store, conv):
    try:
        until = int(store.get_setting("care:until:%s" % conv) or 0)
    except Exception:
        return False
    return until > int(time.time())
