import json, os, secrets, shutil, threading, time
from util import dpapi_encrypt as ENC, dpapi_decrypt as DEC, log, clean_time_windows

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_HOME = os.environ.get("DAFEIYU_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DafeiyuBot")
DATA_DIR = os.path.join(BOT_HOME, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "bot_name": "蓝色大肥鱼",
    # v0.23.0：群里只要出现这个短名就先让模型判断是不是叫本鲸；显示名仍是 bot_name。
    "trigger_name": "大肥鱼",
    "owner_qq": 0,
    "owner_name": "",
    "allow_users": [],
    "allow_all_users": False,  # 仅放开收到的私聊；主动找人仍只看 allow_users + 主人
    "allow_groups": [],
    # 陪伴模式必须显式列出群，且仍受 allow_groups 约束。
    "group_companion_groups": [],
    # v0.67.0 语音回复（三层权限）：①总开关/暂停 ②会话授权 ③个人偏好。
    # 谁能听到语音由每个人自己决定；主人只管总开关和这个会话准不准。
    "voice_enabled": False,
    "voice_paused": False,
    # 开启后由回复模型按上下文选择语音/文字；明确要文字的人仍只收文字。
    "voice_auto_choose": False,
    "voice_groups": [],
    "voice_privates": [],
    # v0.71.1 翻唱点歌的授权（独立于语音，面板「翻唱」页单独管）
    "cover_groups": [],
    "cover_privates": [],
    "cover_cooldown_min": 5,
    "cover_output_mode": "clip",
    "voice_user_prefs": {},
    "voice_max_chars": 120,
    "voice_tts_url": "http://127.0.0.1:17861",
    "voice_timeout_sec": 60,
    "voice_max_audio_sec": 40,
    "voice_queue_max": 3,
    "voice_block_on_cold": False,
    # 模型在电脑显存里留多久（分钟，0＝一直留着）；由大脑每分钟检查一次后卸载
    "voice_idle_unload_min": 10,
    # 合成结果的头尾留白（毫秒）+ 首尾淡入淡出（毫秒）。主人反馈「开头结尾太急」加的
    "voice_lead_pad_ms": 220,
    "voice_tail_pad_ms": 350,
    "voice_fade_ms": 12,
    # v0.69.3：机器人发的语音条自动转文字（真人语音不转，省 CPU）
    "bot_voice_asr_enabled": True,
    "bot_voice_asr_max_sec": 30,
    "bot_voice_asr_max_hour": 30,
    # v0.69.1：C4 后处理——整条升调+加速（1.06≈+1 半音）、削 6kHz 以上气声、抬 2.5kHz 提亮咬字
    "voice_tune_ratio": 1.06,
    "voice_breath_cut_db": -7.0,
    "voice_clarity_db": 1.5,
    # v0.67.9：多段回复时是否每段都发语音（默认开＝全部语音；关掉＝只第一条语音、后面文字）
    "voice_all_bubbles": True,
    # 没表态的人按哪种默认收（2026-10-28 主人要求：群聊默认发语音，私聊仍默认文字）
    "voice_group_default": True,
    "voice_private_default": True,
    # 明确说「文字回复」的人（和 voice_user_prefs 互斥）
    "voice_user_text": {},
    # v0.70.0 语音静音时段：列表里一段一个对象
    #   {"start":"23:30","end":"07:00","days":[1..7],"on":true,"note":"睡觉"}
    # 段内一律发文字，电脑上的模型也不留在显存；可以加多段，跨零点也认（按开始那天算星期）
    "voice_off_periods": [],
    # 音色定稿（2026-10-28）：Breeze TTS 2 Q8 + 参考音「近似A-软萌」，模型跑在主人电脑上
    "voice_instruct": "用软萌小女孩撒娇的语气说：带笑、尾音上扬、更嗲一点，像在跟喜欢的人撒娇，口型松一点。",
    "voice_ref_path": "",
    "voice_ref_text": "在呢在呢，主人这一嗓子喊得本鲸大人尾巴都甩起来了。咋啦，又想我啦？",
    "voice_seed": 10000,
    "group_need_at": True,
    # 被叫到只是邀请；结合心情/上下文自己决定接话或沉默（私聊和主人也一样）。
    "reply_autonomy_enabled": True,
    # 独立保存经历形成的暂时倾向；不覆盖旧记忆，也不指定性格终点。
    "self_growth_enabled": True,
    # v0.54.7：群聊触发方式（只有 group_need_at 开着时才起作用）。两个都关掉＝真正的「只有 @ 才回」
    "group_trigger_name": True,   # 被叫名字也接
    "group_trigger_quote": True,  # 被引用也接
    # v0.54.0：群里回话时 @ 对方（总开关 + 按触发方式分开）；名字那条只在判定为“在叫它”时才 @
    "group_at_reply": True,
    "group_at_reply_at": True,
    "group_at_reply_name": True,
    "group_at_reply_quote": True,
    # v0.54.9：机器人互刷刹车（按 QQ 号认机器人；真人说话即清零）
    "bot_rounds_enabled": True,
    "bot_round_limit": 6,
    "bot_round_reset_min": 10,
    "bot_peers_qq": "",   # 逗号分隔的 QQ；留空＝用 other_bots 里配的那几个 AI 号
    "max_history": 30,
    "max_tokens": 1024,
    # 服务器版：闲聊回复的硬性字数上限（群聊更短）；明确要求长内容时不生效
    "reply_max_chars": 90,
    "reply_max_chars_group": 60,
    "temperature": 0.8,
    "model_timeout": 60,
    # v0.20.3 多群加固：回复线程 / 事件队列 / 模型并发 / 记忆整理并发可配
    "reply_workers": 3,
    "event_queue_size": 500,
    "model_max_concurrent": 4,
    "memory_workers": 2,
    "onebot_api": "http://127.0.0.1:3000",
    "onebot_token": "",
    "admin_token": "",
    "webhook_host": "127.0.0.1",
    "webhook_port": 8080,
    "admin_port": 0,
    "vision_max_bytes": 8388608,
    # v0.64.1：文件（不是图片）的读取上限；引用里的文件、群里没 @ 的静默缓存都走这一套
    "file_max_bytes": 10485760,     # ≤10MB
    "file_parse_chars": 50000,      # 解析后最多交给模型 50000 字
    # v0.40.0 表情包改成「判断 → 召回 → 粗筛 → 亲眼看」：
    # 判断模型读最近 N 条对话决定发不发；召回 M 张 → 粗筛到 K 张 → 逐张看完才发。
    # 没有情绪词触发、没有强制补图、没有频率上限（用户明确要求）。
    "sticker_judge_enabled": True,
    # 新引擎参数
    "sticker_brief_history": 15,
    "sticker_panel_size": 6,
    "sticker_conf_min": 0.6,
    "sticker_text_match_min": 0.6,
    "sticker_rel_min": 0.30,
    "sticker_recheck": "high",          # 旧键（v0.43 起不再读，留作兼容）
    "sticker_text_only": False,
    "sticker_recall_count": 10,
    # v0.43.0：挑完再单独复核这张配不配；不过就换下一张再试。
    "sticker_fit_enabled": True,
    "sticker_fit_min": 7,
    "sticker_fit_rounds": 3,
    "sticker_fit_tries": 3,
    "sticker_wait_ready": True,
    "sticker_send_gap_sec": 1.0,
    "reply_delay_min": 0.0,
    "reply_delay_max": 2.0,
    # v0.58.0 私聊攒批：对方连发几条时先攒着，安静 private_batch_sec 秒再合成一条处理（只对私聊）。
    # private_batch_max_wait_sec 默认 0＝不限时间兜底（对方还在说就一直等，绝不插嘴）；
    # private_batch_max_msgs 是条数兜底，只为防机器刷屏。
    "private_batch": True,
    "private_batch_sec": 3.0,
    "private_batch_max_msgs": 20,
    "private_batch_max_wait_sec": 0.0,
    # 私聊整轮回复的第一个气泡前不再随机等（攒批窗口已经顶掉了这段）；气泡之间照旧
    "reply_delay_skip_first_private": True,
    "context_limit": 256000,
    "keep_recent": 50,
    "idle_reset_sec": 3600,
    "local_llm_port": 18090,
    "local_llm_ctx": 16384,
    "local_llm_device": "auto",
    "local_llm_threads": 0,
    "local_llm_autostart": False,
    "local_llm_thinking": True,
    "local_llm_thinking_budget": 512,
    "local_llm_idle_sleep": 300,
    "model_thinking": True,
    "thinking_budget": 512,
    # v0.57.0：思考强度（方舟/DeepSeek 的 reasoning_effort）。默认 high，聊天明显更聪明；
    # 想省钱/求快可在面板改 low/medium，或 auto（不发参数，用服务商默认）。
    "model_reasoning_effort": "high",
    # v0.57.0：图怎么给模型看。True=被引用/明确问图/发图同时@它 时直接喂原图；False 全走老描述。
    "image_direct_feed": True,
    "models": [],
    "vision_model_id": "",
    "encrypt_content": False,
    "sticker_cooldown_sec": 300,
    "sticker_embed_model": "doubao-embedding-vision-250515",
    "bot_paused": False,          # 暂停使用：不回复、不主动、不分享
    "proactive_enabled": True,
    "proactive_all_friends": False,
    "proactive_whitelist_only": True,
    # v0.20.0：不再等 3 小时。私聊安静 20~40 分钟就能主动开口；同一人每天最多 6 次。
    # proactive_idle_sec 是旧键，代码已不读，保留只为兼容旧面板。
    "proactive_idle_sec": 1800,
    "proactive_min_idle_sec": 1200,
    "proactive_max_idle_sec": 2400,
    "proactive_max_per_day": 6,
    "proactive_quiet_start": 23,
    "proactive_quiet_end": 7,
    "proactive_quiet_jitter_min": 120,   # 静默结束后随机推迟 0~N 分钟（0=固定整点）
    # 群主动发言（冷场暖话）：默认关，面板里开了才会在群里自己开口
    "group_speak_enabled": False,
    "group_speak_idle": True,
    "group_speak_quiet_min": 60,
    "group_speak_gap_min": 60,
    "group_speak_max_per_day": 2,
    "group_speak_quiet_start": 23,
    "group_speak_quiet_end": 7,
    # v0.56.0：同一只任意主动消息最小间隔（分钟）；私聊主动默认不再拿群聊当由头
    "proactive_global_gap_min": 10,
    "proactive_private_from_group": False,
    # v0.60.0：B站刷视频（P2 先只读；控制台/定时/关键词后续接）
    "video_browse_enabled": False,
    "video_browse_notify": True,
    "video_browse_windows": "12:30-14:00,20:00-23:00",
    "video_browse_keywords": "",
    "video_browse_search_ratio": 50,
    "video_browse_start_jitter_min": 45,
    "video_browse_duration_min": 10,
    "video_browse_duration_max": 30,
    "video_browse_sessions_per_day": 1,
    "video_browse_max_minutes_per_day": 45,
    "video_browse_manual_max_min": 60,
    "video_browse_daily_token_limit": 0,
    "video_browse_share_max_per_day": 2,
    "video_browse_share_score_min": 4,
    "video_browse_comment_max_per_day": 3,
    # 成长 L3：知识/观点/做法/口味/表达 + 档案
    "video_browse_growth_enabled": True,
    "video_browse_growth_goals": "",
    "video_browse_model_ids": "",
    "video_browse_model_priority": "",
    "video_browse_growth_max_active": 60,
    "video_browse_growth_learn_max": 2,
    # v0.61.0 知识库 v2：视频/资料先整理成卡片再入库；低于价值门槛的不存，库满按质量淘汰
    "video_browse_kb_enabled": True,
    "video_browse_kb_min_worth": 3,
    "video_browse_kb_keep": 300,
    "video_browse_min_mem_mb": 350,
    # 点赞/收藏（不做投币/弹幕）
    "video_browse_like": True,
    "video_browse_fav": True,
    "video_browse_like_max_per_day": 5,
    "video_browse_fav_max_per_day": 2,
    # 主人 B 站 UID（评论回复判断用；和 QQ owner_qq 不是一个号）
    "video_browse_bili_owner_uid": 0,
    "video_browse_bili_self_mid": 0,
    # 评论回复：只回直接回复它那条评论的人；默认回主人、不回陌生人
    "video_browse_reply_owner": True,
    "video_browse_reply_strangers": False,
    "video_browse_reply_owner_max_per_day": 10,
    "video_browse_reply_stranger_max_per_day": 3,
    "video_browse_reply_poll_min": 10,
    "memory_extract_every": 4,
    "memory_person_max": 250,
    "memory_group_max": 200,
    # v0.55.0：记忆注入改成「按预算给」，不再固定 6/5/3 条；事件类只看近期
    "memory_recall_person": 20,
    "memory_recall_group": 12,
    "memory_recall_shared": 8,
    "memory_inject_tokens": 3000,
    "memory_event_days": 7,
    # v0.63.0 记忆分层遗忘（用户 2026-10-27 拍板）：重要的留，没用的自己烂掉
    #   低价值(<=2)且从没被想起过 → 7 天淡出；低价值但用过 → 30 天；一般(3) → 90 天没被想起淡出；
    #   重要(>=4)和承诺(commitment)永不自动淡出；淡出的还能在面板捞回来（留 memory_decay_purge_days 天）。
    "memory_decay_enabled": True,
    "memory_decay_low_never_days": 7,
    "memory_decay_low_days": 30,
    "memory_decay_mid_days": 90,
    "memory_decay_purge_days": 180,
    # v0.63.1：高分层（4/5 分 + 承诺）每人/每群最多留几条；超出的降一级，不删
    "memory_high_tier_max": 25,
    # 好感/记仇的日常回正：太久没聊往 50 靠；记仇超过这么多天自动淡化
    "favor_decay_days": 14,
    "grudge_days": 7,
    "share_enabled": True,
    # v0.20.0：分享/告状不再限制「两次之间隔多久」；share_min_gap_min 保留但代码不读。
    "share_max_per_day": 6,
    "share_min_gap_min": 0,
    "share_min_juice": 3,          # 料几分以上才值得说（1-5，面板可调）
    "share_pending_ttl_h": 24,     # 憋着没说超过这么多小时自动丢掉
    "share_pending_max": 5,        # 每个会话最多留几条憋着的
    "tattle_max_per_day": 4,
    "share_private_chats": False,
    "share_judge_enabled": True,
    # 群里别的 AI 号（名字:QQ），给记忆整理/分享核对用来区分“骂的是谁”
    "other_bots": "哦鲸鲸:4014334076,小星野:3929214730,缇宝:4012665628",
    # v0.25.0：主人让“骂回去/传话”时，真的私聊去找那个人；偶尔顺口提一句是主人让的
    "errand_enabled": True,
    "errand_reveal_rate": 0.35,
    # v0.26.0：主人问“谁找你了/跟空聊了什么”时，把真实聊天记录塞给模型照着说
    "lookup_enabled": True,
    # v0.27.0：别人托本鲸给主人带话时，先判断是真有事还是骚扰
    "relay_enabled": True,
    "relay_max_per_day": 5,
    "relay_quiet_enabled": True,
    "relay_quiet_start": 23,
    "relay_quiet_end": 7,
    "share_seen_window_h": 48,
    "share_dump_max": 3,
    "music_source": "netease",
    "web_search_enabled": True,
    # 主回复自己选择调用搜索工具；False 保留旧的预先搜索分流。
    "web_search_autonomous": True,
    "web_search_owner_only": True,
    "web_search_count": 5,
    # v0.24.0：要不要搜交给判断小模型（失败时退回关键词表）
    "search_model_judge": True,
    # v0.38.0 理解力：条件句/交易/反话/指代先跑一次小模型判断“他要什么”（约 1 秒）
    "intent_read_enabled": True,
    # 群聊上下文：白名单群里没 @ 它的消息也入库（只存不回），这样“看上面那张图”不会断片
    "passive_group_store": True,
    # 群里发来的图片后台看一眼；v0.47.3 默认不按次数卡，只受系统资源背压。
    "image_watch": True,
    "image_watch_unlimited": True,
    # v0.20.3 起这个上限按「每个群」算，不再是一个全局配额
    "image_watch_per_hour": 30,
    # 每个群每天最多存多少条「没叫它」的被动消息（0=不限）；超了只丢存储、不影响回复
    "group_store_max_per_day": 4000,
    "image_watch_recent": 2,
    "image_watch_window_sec": 900,
    # v0.39.0：表情包（贴纸/收藏表情）单独算配额，免得斗图刷屏把真图配额吃光
    "sticker_watch_per_hour": 40,
    # v0.29.0 QQ空间自主发说说 + 出事邮件告警（空间接口和面板都由这几项控制）
    "qzone_enabled": False,
    "qzone_mode": "dry",
    "qzone_ugc_right": 1,
    # 每天随机 1～上限条自主额度，最多 6 条；0 关闭。不是必须发满。
    "qzone_daily_max": 6,
    "qzone_min_interval_sec": 3600,
    "qzone_active_hours": "9-23",
    "qzone_max_len": 300,
    "qzone_min_score": 7,
    "qzone_image_policy": "sticker",
    "qzone_comment_poll_sec": 1800,
    "qzone_comment_reply": True,
    "qzone_comment_reply_daily_max": 5,
    "qzone_memory_inject": True,
    # 自主额度分段随机释放，覆盖全天活跃时段；两次尝试仍隔 50～110 分钟。
    "qzone_gap_min_min": 50,
    "qzone_gap_max_min": 110,
    # 评审团加权分低于它就不发（0~10）
    "qzone_quality_min": 7.5,
    "qzone_draft_angles": 5,
    # 到这个点还没发够 2 条，就拿天气/心情/今天干的活补一条
    "qzone_light_fill_hour": 21,
    # 配图：网上免版权图（必应图片 + license-L1）开关与体积上限
    "qzone_image_web": True,
    "qzone_image_max_mb": 2,
    # 带图里表情包的占比（其余用网图）；纯文字另算
    "qzone_sticker_ratio": 0.35,
    # 写说说/回评论时把「现在几点、什么时段」喂给模型（夜猫子人设要用）
    "qzone_time_aware": True,
    "alert_email_enabled": False,
    "alert_smtp_host": "smtp.qq.com",
    "alert_smtp_port": 465,
    "alert_smtp_user": "",
    "alert_smtp_pass": "",
    "alert_mail_to": "",
    "dry_run": False,
}

DEFAULT_MODELS = [
    {
        "id": "ark-flash",
        "name": "火山方舟 · deepseek-v4.1-flash",
        "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "model": "deepseek-v4.1-flash",
        "api_key": "",
        "enabled": True,
    }
]

# ---------- 配置校验白名单：坏值回退默认并写日志，不因为一个错字把进程搞崩 ----------
INT_RANGES = {
    # v0.58.0 私聊攒批条数兜底（0=不限，只为防机器刷屏）
    "private_batch_max_msgs": (0, 200),
    "bot_round_limit": (1, 100),
    "bot_round_reset_min": (1, 1440),
    "max_tokens": (16, 32768),
    "max_history": (1, 500),
    "context_limit": (1000, 2000000),
    "keep_recent": (1, 500),
    "idle_reset_sec": (60, 7 * 86400),
    "vision_max_bytes": (65536, 32 * 1024 * 1024),
    "file_max_bytes": (1024, 64 * 1024 * 1024),
    "file_parse_chars": (200, 200000),
    "model_timeout": (5, 600),
    "reply_workers": (1, 12),
    "event_queue_size": (50, 5000),
    "model_max_concurrent": (1, 16),
    "memory_workers": (1, 8),
    "group_store_max_per_day": (0, 50000),
    "proactive_idle_sec": (60, 30 * 86400),
    "proactive_min_idle_sec": (60, 6 * 3600),
    "proactive_max_idle_sec": (60, 6 * 3600),
    "proactive_max_per_day": (0, 50),
    "proactive_quiet_start": (0, 23),
    "proactive_quiet_end": (0, 23),
    "proactive_quiet_jitter_min": (0, 240),
    "group_speak_quiet_min": (5, 1440),
    "group_speak_gap_min": (5, 1440),
    "group_speak_max_per_day": (0, 20),
    "group_speak_quiet_start": (0, 23),
    "group_speak_quiet_end": (0, 23),
    "sticker_cooldown_sec": (0, 86400),
    "sticker_recall_count": (1, 20),
    "sticker_brief_history": (5, 50),
    "sticker_panel_size": (4, 8),
    "sticker_fit_min": (0, 10),
    "sticker_fit_rounds": (1, 3),
    "sticker_fit_tries": (1, 4),
    "webhook_port": (1, 65535),
    "admin_port": (0, 65535),
    "local_llm_port": (1, 65535),
    "local_llm_ctx": (2048, 131072),
    "local_llm_threads": (0, 64),
    "thinking_budget": (128, 32768),
    "memory_extract_every": (2, 50),
    "memory_person_max": (10, 500),
    "memory_group_max": (10, 500),
    "memory_recall_person": (1, 60),
    "memory_recall_group": (1, 60),
    "memory_recall_shared": (0, 30),
    "memory_inject_tokens": (500, 20000),
    "memory_event_days": (1, 365),
    "memory_decay_low_never_days": (1, 365),
    "memory_decay_low_days": (1, 365),
    "memory_decay_mid_days": (1, 3650),
    "memory_decay_purge_days": (7, 3650),
    "favor_decay_days": (7, 90),
    "grudge_days": (7, 365),
    "share_max_per_day": (0, 50),
    "share_min_gap_min": (0, 1440),
    "share_min_juice": (1, 5),
    "share_pending_ttl_h": (1, 720),
    "share_pending_max": (1, 50),
    "tattle_max_per_day": (0, 50),
    "share_seen_window_h": (1, 168),
    "share_dump_max": (1, 5),
    "web_search_count": (1, 20),
    "relay_max_per_day": (0, 50),
    "relay_quiet_start": (0, 23),
    "relay_quiet_end": (0, 23),
    "image_watch_per_hour": (0, 300),
    "image_watch_recent": (0, 5),
    "image_watch_window_sec": (60, 3600),
    "sticker_watch_per_hour": (0, 300),
    "local_llm_thinking_budget": (0, 32768),
    "local_llm_idle_sleep": (0, 86400),
    "owner_qq": (0, 2 ** 63 - 1),
    # QQ空间：可见范围 1~5、每天条数、最小间隔、正文长度、质量分、评论轮询间隔、每天回评上限
    "qzone_ugc_right": (1, 128),
    "qzone_daily_max": (0, 6),
    "qzone_min_interval_sec": (300, 7 * 86400),
    "qzone_max_len": (30, 2000),
    "qzone_min_score": (0, 10),
    "qzone_comment_poll_sec": (60, 86400),
    "qzone_comment_reply_daily_max": (0, 50),
    "qzone_gap_min_min": (10, 600),
    "qzone_gap_max_min": (20, 720),
    "qzone_draft_angles": (1, 5),
    "qzone_light_fill_hour": (0, 23),
    "alert_smtp_port": (1, 65535),
    "video_browse_start_jitter_min": (0, 240),
    "video_browse_duration_min": (1, 180),
    "video_browse_duration_max": (1, 180),
    "video_browse_sessions_per_day": (0, 12),
    "video_browse_max_minutes_per_day": (0, 300),
    "video_browse_manual_max_min": (1, 180),
    "video_browse_daily_token_limit": (0, 10 ** 9),
    "video_browse_share_max_per_day": (0, 6),
    "video_browse_share_score_min": (1, 5),
    "video_browse_comment_max_per_day": (0, 10),
    "video_browse_like_max_per_day": (0, 50),
    "video_browse_fav_max_per_day": (0, 50),
    "video_browse_growth_max_active": (1, 200),
    "video_browse_growth_learn_max": (0, 5),
    "video_browse_kb_min_worth": (0, 5),
    "video_browse_kb_keep": (20, 2000),
    "video_browse_min_mem_mb": (100, 2048),
    "video_browse_bili_owner_uid": (0, 2 ** 63 - 1),
    "video_browse_bili_self_mid": (0, 2 ** 63 - 1),
    "video_browse_reply_owner_max_per_day": (0, 50),
    "video_browse_reply_stranger_max_per_day": (0, 50),
    "video_browse_reply_poll_min": (1, 1440),
    "video_browse_search_ratio": (0, 100),
    # v0.67.0 语音回复
    "voice_max_chars": (10, 600),
    "voice_timeout_sec": (5, 300),
    "voice_max_audio_sec": (5, 180),
    "voice_queue_max": (0, 10),
    "voice_seed": (0, 2 ** 31 - 1),
    "voice_idle_unload_min": (0, 1440),
    "voice_lead_pad_ms": (0, 2000),
    "voice_tail_pad_ms": (0, 3000),
    "voice_fade_ms": (0, 200),
    # v0.69.3 机器人语音转写护栏：单条时长上限（秒）/ 每小时最多转几条（0＝不限）
    "bot_voice_asr_max_sec": (0, 120),
    "bot_voice_asr_max_hour": (0, 200),
}
FLOAT_RANGES = {
    "temperature": (0.0, 2.0),
    "qzone_quality_min": (0.0, 10.0),
    "qzone_image_max_mb": (0.5, 8.0),
    "qzone_sticker_ratio": (0.0, 1.0),
    "errand_reveal_rate": (0.0, 1.0),
    "sticker_send_gap_sec": (0.0, 10.0),
    "sticker_conf_min": (0.0, 1.0),
    "sticker_text_match_min": (0.0, 1.0),
    "sticker_rel_min": (0.0, 1.0),
    "reply_delay_min": (0.0, 30.0),
    "reply_delay_max": (0.0, 30.0),
    # v0.69.1 语音后处理（C4）：升调加速比 / 气声削减 dB / 咬字提亮 dB
    "voice_tune_ratio": (1.0, 1.3),
    "voice_breath_cut_db": (-15.0, 0.0),
    "voice_clarity_db": (0.0, 6.0),
    # v0.58.0 私聊攒批：安静窗口 0.5~30 秒；时间兜底 0~120 秒（0=不限）
    "private_batch_sec": (0.5, 30.0),
    "private_batch_max_wait_sec": (0.0, 120.0),
}
BOOL_KEYS = ("allow_all_users", "group_need_at", "reply_autonomy_enabled", "self_growth_enabled", "bot_paused", "proactive_enabled", "group_speak_enabled", "group_speak_idle", "proactive_all_friends",
             "proactive_whitelist_only", "dry_run", "encrypt_content", "local_llm_autostart",
             "model_thinking", "local_llm_thinking", "share_enabled", "share_private_chats",
             "errand_enabled", "lookup_enabled", "relay_enabled", "relay_quiet_enabled",
             "share_judge_enabled", "sticker_judge_enabled", "sticker_wait_ready", "sticker_text_only",
             "sticker_fit_enabled",
             "web_search_enabled", "web_search_owner_only", "web_search_autonomous", "intent_read_enabled",
             "passive_group_store", "image_watch", "image_watch_unlimited",
             "qzone_enabled", "qzone_comment_reply", "qzone_memory_inject", "alert_email_enabled",
             "qzone_image_web", "qzone_time_aware",
             "group_at_reply", "group_at_reply_at", "group_at_reply_name", "group_at_reply_quote",
             "group_trigger_name", "group_trigger_quote", "bot_rounds_enabled", "memory_decay_enabled",
             "image_direct_feed", "private_batch", "reply_delay_skip_first_private",
             "video_browse_enabled", "video_browse_notify",
             "video_browse_like", "video_browse_fav",
             "video_browse_growth_enabled",
             "video_browse_reply_owner", "video_browse_reply_strangers",
             "voice_enabled", "voice_paused", "voice_block_on_cold", "voice_auto_choose",
             "voice_group_default", "voice_private_default", "voice_all_bubbles",
             "bot_voice_asr_enabled")
STR_LIMITS = {"bot_name": 40, "trigger_name": 40, "owner_name": 40, "vision_model_id": 80, "onebot_api": 200,
              "video_browse_model_ids": 400, "video_browse_model_priority": 80,
              "qzone_mode": 10, "qzone_active_hours": 20, "qzone_image_policy": 20,
              "qzone_daily_target_weights": 60,
              "alert_smtp_host": 200, "alert_smtp_user": 200, "alert_smtp_pass": 200, "alert_mail_to": 500,
              "voice_tts_url": 200, "voice_ref_path": 400, "voice_ref_text": 400, "voice_instruct": 400}
# 只允许固定几个字符串值，写错就回退默认（不让流程自己去猜）
STR_ENUMS = {
    "qzone_mode": ("dry", "live"),
    "sticker_recheck": ("high", "always", "off"),
    # v0.57.0：思考强度；auto=不发参数（用服务商默认）
    "model_reasoning_effort": ("low", "medium", "high", "auto"),
}
MASK = "***"


class DecryptError(Exception):
    pass


class Config(object):
    def __init__(self, path=None):
        self.path = path or CONFIG_PATH
        self._lock = threading.RLock()
        self.data = dict(DEFAULTS)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as e:
                bad = self.path + ".bad-" + time.strftime("%Y%m%d-%H%M%S")
                try:
                    shutil.copy2(self.path, bad)
                except Exception:
                    pass
                raise SystemExit("config.json 解析失败，已备份到 %s：%r" % (bad, e))
        # 严格解密：解不开就报错退出，绝不把空值当"已配置"再写回文件
        try:
            self._decrypt_secrets(self.data)
        except DecryptError as e:
            log("config", "严格解密失败：%s（原文件未改动）" % e)
            raise SystemExit(
                "config.json 里的密钥/token 解不开（%s）。文件没有被改动；"
                "请确认是否换了 Windows 用户/机器，或重新填写 api_key、onebot_token、admin_token。" % e)
        # 解密之后再补默认 token（顺序不能反：先补会把解不开的密文当已配置）
        if not self.data.get("onebot_token"):
            self.data["onebot_token"] = secrets.token_hex(16)
        if not self.data.get("admin_token"):
            self.data["admin_token"] = secrets.token_hex(16)
        self.data["webhook_host"] = "127.0.0.1"
        self._validate(self.data, tag="启动")
        if not self.data.get("models"):
            self.data["models"] = [dict(x) for x in DEFAULT_MODELS]
        self.save()
        self._mtime = self._get_mtime()

    # ---------- 解密 / 校验 ----------
    @staticmethod
    def _coerce_int(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        try:
            return int(str(v).strip())
        except Exception:
            try:
                return int(float(str(v).strip()))
            except Exception:
                return None

    @staticmethod
    def _coerce_float(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).strip())
        except Exception:
            return None

    def _decode_secret(self, value, what):
        """解 enc1: 密文。util 提供 strict=True 时用它；老版 util 用返回值判断。
        解不开一律抛 DecryptError，绝不返回空串冒充合法值。"""
        if not isinstance(value, str) or not (value.startswith("enc1:") or value.startswith("enc2:")):
            return value
        try:
            out = DEC(value, strict=True)
        except TypeError:
            # util 还没提供 strict 参数：老接口解不开会返回 ""
            out = DEC(value)
        except DecryptError:
            raise
        except Exception as e:
            raise DecryptError("%s 解密失败：%r" % (what, e))
        # 不管 strict 版是抛异常、返回 None 还是返回空串，都按失败处理
        if out is None or out == "":
            raise DecryptError("%s 解密失败" % what)
        return out

    def _decrypt_secrets(self, data):
        for k in ("api_key", "onebot_token", "admin_token", "alert_smtp_pass"):
            if isinstance(data.get(k), str) and (data[k].startswith("enc1:") or data[k].startswith("enc2:")):
                data[k] = self._decode_secret(data[k], k)
        models = data.get("models")
        if isinstance(models, list):
            for i, m in enumerate(models):
                if isinstance(m, dict) and isinstance(m.get("api_key"), str) and (m["api_key"].startswith("enc1:") or m["api_key"].startswith("enc2:")):
                    m["api_key"] = self._decode_secret(m["api_key"], "models[%d].api_key" % i)

    def _validate(self, data, tag="配置"):
        """白名单类型/范围校验：坏值回退默认、数字字符串归一化，最后记一条日志。"""
        changed = []
        for key, (lo, hi) in INT_RANGES.items():
            if key not in data:
                continue
            old = data[key]
            val = self._coerce_int(old)
            if val is None or val < lo or val > hi:
                data[key] = DEFAULTS.get(key, lo)
                changed.append("%s=%r→%r" % (key, old, data[key]))
            elif val != old:
                data[key] = val
        for key, (lo, hi) in FLOAT_RANGES.items():
            if key not in data:
                continue
            old = data[key]
            val = self._coerce_float(old)
            if val is None or val < lo or val > hi:
                data[key] = DEFAULTS.get(key, lo)
                changed.append("%s=%r→%r" % (key, old, data[key]))
            elif val != old:
                data[key] = val
        for key in BOOL_KEYS:
            if key not in data or isinstance(data[key], bool):
                continue
            old = data[key]
            text = str(old).strip().lower()
            if text in ("1", "true", "on", "yes"):
                data[key] = True
            elif text in ("0", "false", "off", "no", ""):
                data[key] = False
            else:
                data[key] = bool(DEFAULTS.get(key))
                changed.append("%s=%r→%r" % (key, old, data[key]))
        for key, allowed in STR_ENUMS.items():
            if key not in data:
                continue
            old = data[key]
            val = str(old).strip().lower() if not isinstance(old, bool) else ""
            if val not in allowed:
                data[key] = DEFAULTS.get(key, allowed[0])
                changed.append("%s=%r→%r" % (key, old, data[key]))
            elif val != old:
                data[key] = val
        for key in ("allow_users", "allow_groups", "group_companion_groups", "voice_groups", "voice_privates",
                    "cover_groups", "cover_privates"):
            if key not in data:
                continue
            old = data[key]
            if not isinstance(old, (list, tuple)):
                changed.append("%s=%r 不是列表，本次忽略" % (key, old))
                data.pop(key, None)
                continue
            out = []
            for x in old:
                n = self._coerce_int(x)
                if n is not None and n > 0 and n not in out:
                    out.append(n)
            if out != list(old):
                changed.append("%s=%r→%r" % (key, old, out))
            data[key] = out
        # v0.67.0：语音表态表（{"g:群号":[qq...], "p:qq":[qq...]}）只留合法会话和合法 QQ
        #   voice_user_prefs = 明确要语音；voice_user_text = 明确要文字
        for _pk in ("voice_user_prefs", "voice_user_text"):
            prefs = data.get(_pk)
            if prefs is not None and not isinstance(prefs, dict):
                changed.append("%s 不是对象，按空处理" % _pk)
                data[_pk] = {}
                prefs = {}
            if isinstance(prefs, dict):
                clean_prefs = {}
                for ck, cv in prefs.items():
                    ck = str(ck or "").strip()
                    if not ck.startswith(("g:", "p:")) or not ck[2:].isdigit():
                        continue
                    if not isinstance(cv, (list, tuple)):
                        continue
                    ids = []
                    for x in cv:
                        n = self._coerce_int(x)
                        if n is not None and n > 0 and n not in ids:
                            ids.append(n)
                    if ids:
                        clean_prefs[ck] = ids
                if clean_prefs != prefs:
                    changed.append("%s 清理 %d→%d 个会话" % (_pk, len(prefs), len(clean_prefs)))
                data[_pk] = clean_prefs
        # v0.70.0 语音静音时段：认不出来的段整段丢掉，剩下的归一化（HH:MM + ISO 星期）
        if "voice_off_periods" in data:
            raw_off = data.get("voice_off_periods")
            clean_off = clean_time_windows(raw_off)
            if clean_off != raw_off:
                changed.append("voice_off_periods %r→%d 段" % (
                    (len(raw_off) if isinstance(raw_off, (list, tuple)) else "非列表"), len(clean_off)))
            data["voice_off_periods"] = clean_off
        api = data.get("onebot_api")
        if not isinstance(api, str) or not api.startswith(("http://", "https://")):
            data["onebot_api"] = DEFAULTS["onebot_api"]
            changed.append("onebot_api=%r→%r" % (api, data["onebot_api"]))
        for key, lim in STR_LIMITS.items():
            if key not in data:
                continue
            old = data[key]
            if not isinstance(old, str):
                data[key] = "" if old is None else str(old)
                changed.append("%s 类型修正" % key)
            elif len(data[key]) > lim:
                data[key] = data[key][:lim]
                changed.append("%s 超长截断到 %d" % (key, lim))
        models = data.get("models")
        if models is not None and not isinstance(models, list):
            # 热重载时宁可保留内存里的旧模型列表，也不要把配置清空
            changed.append("models 不是列表，本次忽略")
            data.pop("models", None)
            models = None
        clean = []
        for i, m in enumerate(models or []):
            if not isinstance(m, dict):
                changed.append("models[%d] 不是对象，已跳过" % i)
                continue
            base_url = str(m.get("base_url") or "").strip()
            model = str(m.get("model") or "").strip()
            if not base_url or not model:
                changed.append("models[%d] 缺 base_url/model，已跳过" % i)
                continue
            item = dict(m)
            item["id"] = str(m.get("id") or ("m%d" % (i + 1)))[:40]
            item["name"] = str(m.get("name") or model)[:60]
            item["base_url"] = base_url[:200]
            item["model"] = model[:120]
            item["api_key"] = str(m.get("api_key") or "")
            if item["api_key"] == MASK:
                item["api_key"] = ""
                changed.append("models[%d].api_key 是掩码，按未配置处理" % i)
            item["enabled"] = bool(m.get("enabled", True))
            if m.get("timeout") is not None:
                tv = self._coerce_int(m.get("timeout"))
                if tv is None or tv < 5 or tv > 600:
                    item.pop("timeout", None)
                    changed.append("models[%d].timeout=%r 越界，已忽略" % (i, m.get("timeout")))
                else:
                    item["timeout"] = tv
            clean.append(item)
        if models is not None:
            data["models"] = clean
        if changed:
            log("config", "%s校验修正 %d 项：%s" % (tag, len(changed), "; ".join(changed[:12])))

    def _get_mtime(self):
        try:
            return os.path.getmtime(self.path)
        except Exception:
            return 0

    def _reload_if_changed(self):
        mtime = self._get_mtime()
        if mtime == getattr(self, "_mtime", 0):
            return
        # 先把文件读完整再校验、解密，任何一步失败都保持旧配置并记日志（不再静默）
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            log("config", "热重载失败（JSON 解析错误），继续用旧配置：%r" % e)
            self._mtime = mtime
            return
        if not isinstance(loaded, dict):
            log("config", "热重载失败（顶层不是对象），继续用旧配置")
            self._mtime = mtime
            return
        try:
            self._decrypt_secrets(loaded)
        except DecryptError as e:
            log("config", "热重载失败（%s），继续用旧配置" % e)
            self._mtime = mtime
            return
        self._validate(loaded, tag="热重载")
        loaded["webhook_host"] = "127.0.0.1"
        with self._lock:
            self.data.update(loaded)
        self._mtime = mtime

    # ---------- 共享模型清单（v0.65.2）：三只读同一份，改一个三只生效 ----------
    def shared_models_path(self):
        """共享清单路径。可用 DAFEIYU_SHARED_MODELS 覆盖；文件不存在就退回本机 config 的 models。

        服务器上放协议版的数据目录（三只都读它）；电脑版没有共享需求，路径不存在就自动退回。
        """
        p = str(os.environ.get("DAFEIYU_SHARED_MODELS") or "").strip()
        if p:
            return p
        return os.path.join(DATA_DIR, "models.json")

    def _shared_models(self):
        """读共享清单（按 mtime 缓存）。返回列表或 None；None = 没有共享清单，用本机 config 的。"""
        p = self.shared_models_path()
        try:
            st = os.stat(p)
        except Exception:
            return None
        cache = getattr(self, "_shared_cache", None)
        if cache and cache[0] == (p, st.st_mtime):
            return cache[1]
        out = []
        try:
            with open(p, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            raw = obj.get("models") if isinstance(obj, dict) else obj
            for m in (raw or []):
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "").strip()
                base_url = str(m.get("base_url") or "").strip()
                model = str(m.get("model") or "").strip()
                if not (mid and base_url and model):
                    continue
                key_enc = str(m.get("api_key") or "")
                plain = DEC(key_enc) if key_enc else ""
                out.append({"id": mid, "name": str(m.get("name") or model)[:60],
                            "base_url": base_url, "model": model,
                            "api_key": plain or "", "enabled": bool(m.get("enabled", True))})
        except Exception as e:
            log("config", "读共享清单失败（%r），继续用本机 config 的 models" % e)
            return None
        if not out:
            log("config", "共享清单里没有一条能用的线路，退回本机 config 的 models")
            return None
        self._shared_cache = ((p, st.st_mtime), out)
        return out

    def shared_models_info(self):
        """面板用：共享清单生效没有、路径、几条、谁在什么时候改的。"""
        p = self.shared_models_path()
        info = {"on": False, "path": p, "count": 0, "updated_at": 0, "updated_by": ""}
        try:
            with open(p, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                info["updated_at"] = int(obj.get("updated_at") or 0)
                info["updated_by"] = str(obj.get("updated_by") or "")
        except Exception:
            pass
        ms = self._shared_models()
        if ms:
            info["on"] = True
            info["count"] = len(ms)
        return info

    @staticmethod
    def _clean_models_for_share(models):
        """校验 + 加密，返回 (clean, err)。写共享清单前必须过这一关，免得一次写坏三只。"""
        clean, seen = [], set()
        for m in (models or []):
            if not isinstance(m, dict):
                continue
            base_url = str(m.get("base_url") or "").strip()
            model = str(m.get("model") or "").strip()
            if not (base_url and model):
                continue
            mid = str(m.get("id") or "").strip()[:40] or ("m%d" % (len(clean) + 1))
            if mid in seen:
                return ([], "线路 id 重复：%s" % mid)
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                return ([], "线路 %s 的 base_url 必须以 http(s):// 开头" % mid)
            keyv = str(m.get("api_key") or "").strip()
            if keyv == MASK:
                keyv = ""
            if keyv and not (keyv.startswith("enc1:") or keyv.startswith("enc2:")):
                enc = ENC(keyv)
                if not (isinstance(enc, str) and (enc.startswith("enc1:") or enc.startswith("enc2:"))):
                    return ([], "线路 %s 的 Key 加密失败（磁盘权限？）" % mid)
                keyv = enc
            seen.add(mid)
            clean.append({"id": mid, "name": str(m.get("name") or model)[:60],
                          "base_url": base_url, "model": model,
                          "api_key": keyv, "enabled": bool(m.get("enabled", True))})
        if not clean:
            return ([], "至少要有 1 条线路（填了 base_url 和 model）")
        if not any(x["enabled"] for x in clean):
            return ([], "至少要启用 1 条线路，不然三只都没模型可用")
        return (clean, "")

    def save_shared_models(self, models, by=""):
        """把模型清单写进共享文件（三只同时生效）：先校验，再备份，最后原子替换。"""
        clean, err = self._clean_models_for_share(models)
        if err:
            return (False, err)
        p = self.shared_models_path()
        try:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(p):
                try:
                    shutil.copy2(p, p + ".bak")
                except Exception:
                    pass
            out = {"version": 1, "updated_at": int(time.time()),
                   "updated_by": str(by or "")[:40], "models": clean}
            tmp = p + ".tmp-%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
            self._shared_cache = None
            log("config", "共享模型清单已更新：%d 条（三只同时生效）" % len(clean))
            return (True, "")
        except Exception as e:
            log("config", "写共享模型清单失败：%r" % e)
            return (False, repr(e)[:160])

    def get(self, key, default=None):
        self._reload_if_changed()
        if key == "models":
            shared = self._shared_models()
            if shared is not None:
                return [dict(m) for m in shared]
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        # 前端把掩码当值回传过一次就会把真 Key 覆盖掉，这里直接拒收
        if key in ("api_key", "alert_smtp_pass") and isinstance(value, str) and value.strip() == MASK:
            log("config", "拒绝把掩码 *** 当作 %s 保存" % key)
            return
        if key == "models" and isinstance(value, list):
            fixed = []
            for m in value:
                if isinstance(m, dict) and str(m.get("api_key") or "").strip() == MASK:
                    m = dict(m)
                    m["api_key"] = ""
                    log("config", "models[%s].api_key 是掩码，按未配置处理" % m.get("id", "?"))
                fixed.append(m)
            value = fixed
            # v0.65.2：模型清单写共享文件，三只一起生效；写坏了直接抛错，不落盘
            by = ""
            try:
                by = str(self.data.get("bot_name") or "")
            except Exception:
                by = ""
            ok, err = self.save_shared_models(value, by=by)
            if not ok:
                raise ValueError(err)
            with self._lock:
                self.data["models"] = value
            self._shared_cache = None
            self.save()   # 本机 config 里也留一份，共享文件坏了能退回
            return
        with self._lock:
            self.data[key] = value
        self.save()

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 写前留一份 .bak：面板/启动写坏了还能手工捞回来
            if os.path.exists(self.path):
                try:
                    shutil.copy2(self.path, self.path + ".bak")
                except Exception as e:
                    log("config", "备份 config.json 失败（继续保存）：%r" % e)
            # 每个进程用不同的临时名，避免 console 和 brain 同时写互相踩
            tmp = self.path + ".tmp-%d" % os.getpid()
            out = dict(self.data)
            for k in ("api_key", "onebot_token", "admin_token", "alert_smtp_pass"):
                if isinstance(out.get(k), str) and out[k] and not (out[k].startswith("enc1:") or out[k].startswith("enc2:")):
                    enc = ENC(out[k])
                    if not (isinstance(enc, str) and (enc.startswith("enc1:") or enc.startswith("enc2:"))):
                        log("config", "加密 %s 失败，按明文保存（不推荐）" % k)
                    out[k] = enc
            out["models"] = []
            models = self.data.get("models")
            for m in (models if isinstance(models, list) else []):
                if not isinstance(m, dict):
                    continue
                mm = dict(m)
                if isinstance(mm.get("api_key"), str) and mm["api_key"] and not (mm["api_key"].startswith("enc1:") or mm["api_key"].startswith("enc2:")):
                    enc = ENC(mm["api_key"])
                    if not (isinstance(enc, str) and (enc.startswith("enc1:") or enc.startswith("enc2:"))):
                        log("config", "加密 models.api_key 失败，按明文保存（不推荐）")
                    mm["api_key"] = enc
                out["models"].append(mm)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                # Windows 上目标文件可能正被 console 读着，替换失败就重试几次
                last = None
                for _ in range(5):
                    try:
                        os.replace(tmp, self.path)
                        last = None
                        break
                    except PermissionError as e:
                        last = e
                        time.sleep(0.1)
                if last is not None:
                    raise last
                # config.json 里有加密后的 Key/token，只给本人读；
                # 根用户跑脚本时也要把属主还给数据目录的主人，否则机器人读不了
                try:
                    os.chmod(self.path, 0o600)
                except Exception:
                    pass
                try:
                    if hasattr(os, "geteuid") and os.geteuid() == 0:
                        st = os.stat(os.path.dirname(self.path))
                        if st.st_uid != 0:
                            os.chown(self.path, st.st_uid, st.st_gid)
                except Exception:
                    pass
            except Exception as e:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                log("config", "写 config.json 失败：%r" % e)
                raise
        self._mtime = self._get_mtime()

    def models(self):
        self._reload_if_changed()
        shared = self._shared_models()
        if shared is not None:
            return [dict(m) for m in shared if m.get("enabled", True)]
        with self._lock:
            return [dict(m) for m in (self.data.get("models") or []) if m.get("enabled", True)]

    def public_dict(self):
        with self._lock:
            d = dict(self.data)
        for k in ("api_key", "onebot_token", "admin_token", "alert_smtp_pass"):
            if d.get(k):
                d[k] = "***"
        shared = self._shared_models()
        ms = shared if shared is not None else (d.get("models") or [])
        d["models"] = [{**m, "api_key": ("***" if m.get("api_key") else "")} for m in ms]
        return d
