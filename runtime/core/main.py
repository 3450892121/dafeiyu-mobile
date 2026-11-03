import atexit, base64, hashlib, hmac, ipaddress, io, json, os, queue, random, re, shutil, signal, socket, subprocess, tempfile, threading, time, urllib.parse, urllib.request, uuid, zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import Config
from onebot import OneBot, text_seg, image_seg, face_seg, at_seg
from model import ModelPool
from dreaming import DreamRunner
from persona import Persona, emotion_nudge as persona_emotion_nudge
from store import Store, _clean_name
from stickers import Stickers
from sticker_index import StickerIndex, embed_texts
import qq_faces
from websearch import search_with_fallback, format_for_prompt as websearch_format
from version import VERSION, BUILD
from music import search_first as music_search, music_seg
from safety import allow_outbound
from util import log, log_exc, acquire_single_instance
from common import *
from common import _as_int, _int_list, _mime
import bot_rounds
import voice
import guard
import reply_autonomy
import private_contact
from blacklist import blocked, policy_scope, handle_command as handle_blacklist_command
from inbound_batch import InboundBatcher
from group_companion_state import GroupCompanionState
from flow_companion import CompanionMixin
from flow_context import BotVoiceNoteMixin, ContextMixin
from flow_stickers import StickerMixin, mood_sticker_keyword, _mood_sticker_gate, _FACE_STICKER_KW, _EMO_STICKER_RULES
from flow_proactive import ProactiveMixin
from flow_qzone import QzoneMixin
from flow_memory import MemoryMixin
from flow_reply import ReplyMixin, split_chunks, split_reply_units
from flow_media import MediaMixin
from video_browse import VideoBrowsingMixin
from flow_intent import IntentMixin
from identity import Identity
from self_runtime import SelfRuntimeMixin


# v0.53.0 日记口吻（研究后定的写法：不是摘要，是「今天最值得记的那几件事」）
DIARY_PROMPT = """你就是{name}，现在是晚上，给自己写今天（{day}）的日记。只写给自己看，不发表、不解释。
写法要求：
1. 第一人称，用你平时说话的自称和口气；像真人窝在被窝里写的日记，不是交作业。
2. 只挑今天最值得记的 1~3 件事：有情绪、有转折、有点好笑/出糗/被逗/被凶/被人放在心上都行。别按时间顺序一条条复述聊天，也别把每个群都写一遍。
3. 写你当时怎么想、心里什么感觉；可以自嘲、吐槽、惦记某个人、有点小情绪、偷偷承认嘴硬。
4. 不写总结句（「今天学到了」「今天很充实」这类），不写系统/技术/模型/记忆/提示词，不写「作为AI」。
5. 不编没发生的事；没出现过的名字别硬写。
6. 180~300 字，一到两段自然写下去；不用标题、不用列表、不用 Markdown、结尾别喊口号，淡淡的收住。
7. 开头直接写事，不要先写日期、天气、"今天是"这种起手。
8. 如果今天有心情变化（吃醋/不满/难过/开心/害羞），自然带进日记，别写成“心情：xx”的清单。
"""


class BodyTooLarge(Exception):
    pass


class HookServer(ThreadingHTTPServer):
    # Linux 上重启后旧监听会短时间处于 TIME_WAIT，不开复用会 bind 失败、大脑起不来；
    # 单实例由 util.acquire_single_instance 的文件锁保证，这里可以安全地开复用。
    allow_reuse_address = True
    allow_reuse_port = True
    daemon_threads = True


class Bot(SelfRuntimeMixin, CompanionMixin, ContextMixin, BotVoiceNoteMixin, StickerMixin, ProactiveMixin, QzoneMixin, MemoryMixin, ReplyMixin, MediaMixin, VideoBrowsingMixin, IntentMixin):
    instance = None
    # v0.63.2：单实例锁句柄。在 __init__ 最前面就抢，拿不到直接退出——
    # 以前锁放在 start() 里，重复启动的进程会先把库打开、先写一行假的 [boot] pid= 再退出。
    _boot_lock = None

    @staticmethod
    def _boot_lock_name():
        # v0.66.0：锁名按 BOT_HOME 区分，第二只小号才有自己的锁（以前固定名字，第二只直接被拒）。
        # 单实例部署行为不变（还是同一台机器只有一个大脑在跑）。
        return "Global\\DafeiyuBrain_" + hashlib.sha1(BOT_HOME.encode("utf-8")).hexdigest()[:12]

    def _acquire_boot_lock(self):
        """抢单实例锁；抢不到就立刻 log + 退出（绝不碰数据库、不写 boot 日志）。"""
        mutex = acquire_single_instance(self._boot_lock_name())
        if mutex is None:
            log("boot", "已有实例在运行，拒绝启动 pid=%d（未打开数据库）" % os.getpid())
            raise SystemExit(1)
        self._boot_lock = mutex
        return mutex

    def __init__(self):
        self._acquire_boot_lock()
        Bot.instance = self
        os.makedirs(MEDIA_DIR, exist_ok=True)
        os.makedirs(FILES_DIR, exist_ok=True)
        self.cfg = Config()
        self.store = Store(os.path.join(DATA_DIR, "bot.db"))
        self.bot_rounds = bot_rounds.BotRoundGuard()
        self._companion_state = GroupCompanionState()
        self._companion_lock = threading.Lock()
        self._companion_timers = {}
        self.onebot = OneBot(self.cfg.get("onebot_api"), self.cfg.get("onebot_token"))
        self.onebot.deny_user = self.store.is_blocked
        self.pool = ModelPool(self.cfg, usage_sink=self.store.add_usage)
        self.persona = Persona(PROJECT_ROOT, BOT_HOME, self.cfg)
        self._self_initialize()
        dream_lock = os.path.join(os.path.dirname(self.cfg.shared_models_path()), "state", "dream.lock")
        self.dreams = DreamRunner(self, dream_lock)
        meta = self._sticker_meta()
        self.stickers = Stickers(meta, os.path.join(os.path.dirname(meta), "images"))
        self._desc_path = os.path.join(os.path.dirname(meta), "sticker_desc.json")
        self._desc_cache = {}
        try:
            self._desc_cache = json.load(open(self._desc_path, "r", encoding="utf-8"))
        except Exception:
            self._desc_cache = {}
        self.sticker_index = None
        try:
            self.sticker_index = StickerIndex(DATA_DIR, self.cfg)
            log("sticker", "语义索引%s（%d 张）" % ("就绪" if self.sticker_index.ok else "缺失", len(self.sticker_index.files)))
        except Exception as e:
            log("sticker", "图像索引初始化失败 %r" % (e,))
        self.self_id = 0
        self.identity = Identity(self.store, self.onebot, self.self_id, self.cfg)
        self.started_at = int(time.time())
        self.seen = set()
        self.seen_order = deque(maxlen=5000)
        self.seen_lock = threading.Lock()
        # v0.55.0：把持久去重表装回内存（以前重启就丢，重投的消息会被再处理一次）
        try:
            for _mid in self.store.seen_recent(5000):
                self.seen.add(_mid)
                self.seen_order.append(_mid)
        except Exception:
            pass
        self.conv_locks = {}
        self.conv_locks_lock = threading.Lock()
        # v0.20.3 多群加固：队列/线程数可配；500 条够多个群短时爆发
        self.q = queue.Queue(maxsize=max(50, min(5000, int(self.cfg.get("event_queue_size") or 500))))
        self.worker_count = max(1, min(12, int(self.cfg.get("reply_workers") or 3)))
        self.queue_drops = 0
        self._queue_lock = threading.Lock()
        # v0.58.0 私聊攒批：连发几条 → 等安静 private_batch_sec 秒 → 合成一条再处理。
        # 只对私聊生效；群聊走 handle() 原路，一行没动。on_flush 只做「回队列」。
        self.batcher = InboundBatcher(
            debounce_sec=float(self.cfg.get("private_batch_sec") or 3.0),
            max_msgs=int(self.cfg.get("private_batch_max_msgs") or 0),
            max_wait_sec=float(self.cfg.get("private_batch_max_wait_sec") or 0.0),
            on_flush=self._enqueue_merged)
        # 2026-10-25 SIGTERM 优雅退出：先停止接新事件，再给 worker 一点时间 drain 队列，最后关库
        self.stopping = threading.Event()
        self._drain_done = threading.Event()
        self._srv = None
        self.reply_stats = {}
        self.reply_stats_lock = threading.Lock()
        self.last_sticker = {}
        self.recent_stickers = {}
        self.last_face_swap = {}
        self.last_sticker_at = {}
        self.sticker_streak = {}
        self.sticker_mood_streak = {}
        self.last_file = {}
        self.proactive_due = {}
        self.proactive_seen = {}
        self._song_wait = {}  # v0.65.0：问过「想听哪首」的会话 → 时间戳（5 分钟内有效）
        self._song_pick = {}  # v0.65.5：点歌候选列表 → 会话 → {at, uid, kw, songs}（等回序号）
        self.mem_counter = {}
        self.summarizing = set()
        self.summarizing_lock = threading.Lock()
        self.mem_pending = {}
        self.mem_last_activity = {}
        self.mem_meta = {}
        self.consolidating = set()
        self.share_lock = threading.Lock()
        self.card_last = {}
        # 被动群消息的每天配额（防止多群把库撑爆；超了只丢存储，不影响回复）
        self.passive_day = {}
        self.passive_lock = threading.Lock()
        self.passive_drop_logged = set()
        self.img_q = queue.Queue(maxsize=300)
        try:
            self.store.set_setting("memory_person_max", str(int(self.cfg.get("memory_person_max") or 80)))
            self.store.set_setting("memory_group_max", str(int(self.cfg.get("memory_group_max") or 120)))
            log("memory", "v2 初始化 %s" % (self.store.migrate_memory_v2(),))
        except Exception as e:
            log("memory", "v2 初始化失败 %r" % (e,))
        self.day_done = set()
        self._reminder_fail = {}
        self._reminder_giveup = set()
        self._last_ok_at = 0
        self.asr_sem = threading.Semaphore(1)
        log("boot", "pid=%d %s stickers=%d 简报=%s条 召回=%s 拼图=%s 复核=%s %.1f分/%s遍/备%s 等图=%s 间隔=%ss workers=%d queue=%d api=%s models=%s" % (
            os.getpid(), VERSION, len(self.stickers),
            self.cfg.get("sticker_brief_history") or 15,
            self.cfg.get("sticker_recall_count"), self.cfg.get("sticker_panel_size"),
            "开" if self.cfg.get("sticker_fit_enabled") else "关",
            float(self.cfg.get("sticker_fit_min") or 7), self.cfg.get("sticker_fit_rounds"),
            self.cfg.get("sticker_fit_tries"),
            "开" if self.cfg.get("sticker_wait_ready") else "关",
            self.cfg.get("sticker_send_gap_sec"),
            self.worker_count, self.q.maxsize,
            self.cfg.get("onebot_api"), ",".join(m.get("id", "") for m in self.cfg.models())))

    def start(self):
        # v0.63.2：锁已经在 __init__ 最前面拿到了（见 _acquire_boot_lock），这里只做兜底校验，
        # 保证「没拿到锁就绝不往下跑」。以前这里才抢锁，结果重复启动的进程会先把库打开、
        # 先写一行假的 [boot] pid= 日志再退出，日志里留幽灵 pid、还会把看门狗的宽限期往后推。
        mutex = self._boot_lock or self._acquire_boot_lock()
        if mutex is None:
            log("boot", "已有实例在运行，拒绝启动 pid=%d" % os.getpid())
            raise SystemExit(1)
        port = int(self.cfg.get("webhook_port") or 8080)
        srv = None
        last = None
        for _ in range(15):
            try:
                srv = HookServer((WEBHOOK_HOST, port), HookHandler)
                break
            except OSError as e:
                last = e
                time.sleep(1)
        if srv is None:
            log("boot", "webhook 绑定 %s:%d 失败：%r" % (WEBHOOK_HOST, port, last))
            raise SystemExit(1)
        srv.daemon_threads = True
        self._srv = srv
        # systemd 停服务发的是 SIGTERM；不接的话 Python 直接死，队列里的事件全丢，
        # WAL 也来不及收尾。装上优雅退出，配合控制台 15 秒等待。
        self._install_signal_handlers()
        pid_path = os.path.join(DATA_DIR, "brain.pid")
        try:
            with open(pid_path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            atexit.register(self._cleanup_pid_file, pid_path)
        except Exception as e:
            log("boot", "写 brain.pid 失败 %r" % (e,))
        threading.Thread(target=self._login_loop, name="login", daemon=True).start()
        threading.Thread(target=self._reminder_loop, name="reminder", daemon=True).start()
        threading.Thread(target=self._diary_loop, name="diary", daemon=True).start()
        threading.Thread(target=self.dreams.loop, name="dream", daemon=True).start()
        threading.Thread(target=self._self_loop, name="self_continuity", daemon=True).start()
        threading.Thread(target=self._proactive_loop, name="proactive", daemon=True).start()
        threading.Thread(target=self._group_speak_loop, name="gspeak", daemon=True).start()
        threading.Thread(target=self._qzone_loop, name="qzone", daemon=True).start()
        threading.Thread(target=self._qzone_comment_loop, name="qzone_comments", daemon=True).start()
        threading.Thread(target=self._memory_loop, name="memory", daemon=True).start()
        threading.Thread(target=self._image_loop, name="image", daemon=True).start()
        threading.Thread(target=self._status_loop, name="status", daemon=True).start()
        threading.Thread(target=self._maintenance_loop, name="maintenance", daemon=True).start()
        threading.Thread(target=self._mem_loop, name="mem", daemon=True).start()
        threading.Thread(target=self._video_browse_loop, name="vbrowse_loop", daemon=True).start()
        # v0.67.0 语音：按面板设的空闲分钟数卸载电脑上的 TTS 模型（玩游戏点暂停也立刻卸）
        threading.Thread(target=self._voice_idle_loop, name="voiceidle", daemon=True).start()
        for i in range(self.worker_count):
            threading.Thread(target=self._worker, name="worker-%d" % i, daemon=True).start()
        log("boot", "webhook listening on %s:%d pid=%d" % (WEBHOOK_HOST, port, os.getpid()))
        srv.serve_forever()
        # serve_forever 只在收到退出信号（srv.shutdown）后返回；等 drain 线程收尾再关库
        self._drain_done.wait(2)
        self._shutdown_store()

    STOP_DRAIN_SEC = 12

    def _install_signal_handlers(self):
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except Exception as e:
            log("boot", "安装 SIGTERM/SIGINT 处理失败 %r" % (e,))

    def _on_signal(self, signum, frame):
        """SIGTERM：停止接新事件 → drain 队列 → 关库（最多等 STOP_DRAIN_SEC 秒）。

        原来是默认处理直接退出，重启/看门狗那一下队列里的事件会丢，
        正在写的 SQLite 也来不及 checkpoint。控制台现在会等 15 秒再 SIGKILL。
        """
        if self.stopping.is_set():
            return
        log("boot", "收到信号 %s：停止接新事件，drain 队列（最多 %d 秒）" % (signum, self.STOP_DRAIN_SEC))
        self.stopping.set()
        threading.Thread(target=self._drain_and_stop, name="drain", daemon=True).start()

    def _drain_and_stop(self):
        deadline = time.time() + self.STOP_DRAIN_SEC
        # v0.58.0：先把还在攒批窗口里的私聊冲回队列，再等 drain（否则这几条就跟着进程没了）。
        # 合并事件是「已经收下的事件」，所以走 _enqueue_merged（不受 stopping 门影响）。
        try:
            n = self.batcher.flush_all()
            if n:
                log("batch", "退出前冲掉 %d 个攒批池" % n)
        except Exception as e:
            log("batch", "退出前冲攒批失败 %r" % (e,))
        try:
            while time.time() < deadline:
                try:
                    left = int(self.q.unfinished_tasks)
                except Exception:
                    left = 0
                if left <= 0:
                    break
                time.sleep(0.2)
            try:
                left = int(self.q.unfinished_tasks)
            except Exception:
                left = 0
            if left > 0:
                log("boot", "drain 超时，还有 %d 条没处理完（剩余会被 SIGKILL 打断）" % left)
            else:
                log("boot", "drain 完成")
        except Exception as e:
            log("boot", "drain 异常 %r" % (e,))
        finally:
            srv = self._srv
            if srv is not None:
                try:
                    srv.shutdown()
                except Exception:
                    pass
            # 放在 shutdown 之后：主线程等到这个事件时，serve_forever 已经在退出路上了
            self._drain_done.set()

    def _shutdown_store(self):
        """关库前 checkpoint WAL，尽量别把 -wal/-shm 留成脏文件。"""
        try:
            with self.store._lock:
                self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.store.conn.commit()
        except Exception as e:
            log("boot", "关库前 WAL checkpoint 失败 %r" % (e,))
        try:
            with self.store._lock:
                self.store.conn.close()
            log("boot", "数据库已关闭")
        except Exception as e:
            log("boot", "关库失败 %r" % (e,))

    def _cleanup_pid_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if int(f.read().strip() or 0) == os.getpid():
                    os.remove(path)
        except Exception:
            pass

    def _note_latency(self, conv, seconds):
        try:
            ms = int(max(0.0, float(seconds)) * 1000)
            with self.reply_stats_lock:
                st = self.reply_stats.get(conv) or {"count": 0, "sum": 0, "last": 0, "max": 0}
                st["count"] += 1
                st["sum"] += ms
                st["last"] = ms
                st["max"] = max(int(st.get("max") or 0), ms)
                self.reply_stats[conv] = st
                if len(self.reply_stats) > 40:
                    oldest = sorted(self.reply_stats.items(), key=lambda kv: kv[1].get("last", 0))[:10]
                    for k, _ in oldest:
                        self.reply_stats.pop(k, None)
        except Exception:
            pass

    def _latency_snapshot(self):
        try:
            out = {}
            with self.reply_stats_lock:
                for conv, st in list(self.reply_stats.items())[:40]:
                    n = max(1, int(st.get("count") or 1))
                    out[conv] = {"last_ms": int(st.get("last") or 0),
                                 "avg_ms": int((st.get("sum") or 0) / n),
                                 "max_ms": int(st.get("max") or 0),
                                 "count": int(st.get("count") or 0)}
            return out
        except Exception:
            return {}

    def status(self):
        return {"version": VERSION, "build": BUILD,
                "self_id": self.self_id, "queue": self.q.qsize(), "queue_size": self.q.maxsize,
                "queue_drops": self.queue_drops, "workers": self.worker_count,
                "uptime": int(time.time()) - self.started_at,
                "model_ok": self.pool.last_ok, "model_error": str(self.pool.last_error or "")[:200],
                "model_rate_limited": int(getattr(self.pool, "rate_limit_count", 0) or 0),
                "reply_latency": self._latency_snapshot(),
                "rss_mb": round(self._rss_mb(), 1),
                "pending_pools": self.batcher.pending(),
                "disk": self._disk_info(),
                "last_ok_at": self._last_ok_at, "pid": os.getpid()}

    def _status_loop(self):
        path = os.path.join(DATA_DIR, "status.json")
        while True:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.status(), f, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception as e:
                log("status", "写 status.json 失败 %r" % (e,))
            time.sleep(5)

    # ---------- 内存守护（服务器版 v0.45.1） ----------
    # 病根：webhook/表情包复核会反复开线程，glibc 给线程配的内存池（arena）只增不退，
    # 图片解码用的大块内存留在池子里，进程 RSS 就台阶式上涨。这里做三件事：
    # 1) 每小时记一条 RSS 到 data/mem.csv（留 7 天），能看见长期趋势；
    # 2) 每 30 分钟 malloc_trim(0)，把池子里的空闲页还给系统；
    # 3) RSS 连续 3 次超过 mem_restart_mb（默认 600）就主动退出，让控制台 8 秒内拉起。
    MEM_RESTART_MB = 600

    def _rss_mb(self):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0
        except Exception:
            pass
        return 0.0

    def _malloc_trim(self):
        try:
            import ctypes
            return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
        except Exception:
            return False

    def _mem_append(self, rss):
        path = os.path.join(DATA_DIR, "mem.csv")
        try:
            rows = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    rows = f.read().splitlines()
            cutoff = int(time.time()) - 7 * 86400
            keep = []
            for r in rows:
                if not r or r.startswith("ts,"):
                    continue
                try:
                    if int(r.split(",", 1)[0]) >= cutoff:
                        keep.append(r)
                except Exception:
                    pass
            keep.append("%d,%s,%.1f" % (int(time.time()), time.strftime("%Y-%m-%d %H:%M:%S"), rss))
            keep = keep[-24 * 8:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("ts,time,rss_mb\n" + "\n".join(keep) + "\n")
            os.replace(tmp, path)
        except Exception as e:
            log("mem", "写 mem.csv 失败 %r" % (e,))

    def _mem_loop(self):
        if not os.path.exists("/proc/self/status"):
            return
        try:
            limit = int(self.cfg.get("mem_restart_mb") or self.MEM_RESTART_MB)
        except Exception:
            limit = self.MEM_RESTART_MB
        over = 0
        last_hour = ""
        last_trim = 0
        while True:
            try:
                rss = self._rss_mb()
                hour = time.strftime("%Y-%m-%d %H")
                if rss > 0 and hour != last_hour:
                    last_hour = hour
                    self._mem_append(rss)
                    log("mem", "RSS %.0fMB（上限 %sMB）" % (rss, limit if limit > 0 else "关"))
                if limit > 0 and rss >= limit:
                    over += 1
                    log("mem", "RSS %.0fMB 超过 %dMB（第 %d/3 次）" % (rss, limit, over))
                    if over >= 3:
                        try:
                            self.store.add_log("mem", "RSS %.0fMB 连续超限，主动重启" % rss, False)
                        except Exception:
                            pass
                        log("mem", "连续 3 次超限，主动退出，让控制台重启")
                        try:
                            os.remove(os.path.join(DATA_DIR, "brain.pid"))
                        except Exception:
                            pass
                        time.sleep(1)
                        os._exit(0)
                else:
                    over = 0
                now = time.time()
                if now - last_trim >= 1800:
                    last_trim = now
                    if self._malloc_trim():
                        log("mem", "malloc_trim 归还空闲内存，当前 RSS %.0fMB" % rss)
            except Exception as e:
                log_exc("mem", e)
            time.sleep(300)

    # ---------- 长期清理：日志轮转 / 磁盘水位 / qzone 配图 ----------
    # 服务器日志以前只轮转 bot.log，console/service/watchdog/napcat 一路长到几百 MB。
    LOG_ROTATE_MAX = 5 * 1024 * 1024
    LOG_ROTATE_KEEP = 2 * 1024 * 1024
    LOG_ROTATE_NAMES = ("console.log", "service.log", "watchdog.log", "napcat.log")
    DISK_LOW_PCT = 10.0

    def _disk_info(self):
        """给 status.json / 管家用的磁盘水位（DATA_DIR 所在分区）。"""
        try:
            u = shutil.disk_usage(DATA_DIR)
            total_mb = int(u.total // (1024 * 1024))
            free_mb = int(u.free // (1024 * 1024))
            free_pct = round(u.free * 100.0 / u.total, 1) if u.total else 0.0
            return {"total_mb": total_mb, "free_mb": free_mb, "free_pct": free_pct}
        except Exception:
            return {}

    def _rotate_side_logs(self):
        """按大小轮转 console/service/watchdog/napcat 日志，保留一份 .1。

        这些文件被 console / systemd / xvfb 以追加方式长期打开，所以用「读出来 → 写 .1 →
        原地截断」的 copytruncate，而不是 rename：rename 之后写入方还指着旧 inode，
        日志会继续写进 .1，真正的日志文件反而没人写。
        """
        for name in self.LOG_ROTATE_NAMES:
            path = os.path.join(DATA_DIR, name)
            try:
                if not os.path.exists(path):
                    continue
                size = os.path.getsize(path)
                if size < self.LOG_ROTATE_MAX:
                    continue
                # 只读末尾 KEEP 字节写 .1：日志可能已经很大，整读会白白吃内存（2G 机器）
                keep = min(size, self.LOG_ROTATE_KEEP)
                with open(path, "rb") as f:
                    try:
                        f.seek(-keep, os.SEEK_END)
                    except OSError:
                        f.seek(0)
                    data = f.read(keep)
                tmp = path + ".1.tmp"
                with open(tmp, "wb") as f:
                    f.write(data[-self.LOG_ROTATE_KEEP:])
                os.replace(tmp, path + ".1")
                with open(path, "wb"):
                    pass
                log("maint", "日志轮转 %s（%.1fMB，保留 .1 最后 2MB）" % (name, size / 1048576.0))
            except Exception as e:
                log("maint", "日志轮转失败 %s %r" % (name, e))

    def _disk_low_check(self):
        info = self._disk_info()
        pct = info.get("free_pct")
        if pct is None or pct >= self.DISK_LOW_PCT:
            return
        msg = "磁盘可用 %.1f%%（剩余 %d MB），低于 %.0f%% 警戒线" % (
            pct, info.get("free_mb") or 0, self.DISK_LOW_PCT)
        log("disk", msg)
        try:
            # 库日志每小时最多记一次，别每轮维护刷屏
            last = int(self.store.get_setting("disk_low_at") or 0)
            if time.time() - last > 3600:
                self.store.add_log("disk", msg, False)
                self.store.set_setting("disk_low_at", str(int(time.time())))
        except Exception as e:
            log("disk", "写磁盘告警失败 %r" % (e,))

    def _voice_idle_loop(self):
        """v0.67.0：每分钟看一次电脑上的 TTS 模型该不该卸（暂停/空闲超时）。"""
        while True:
            try:
                voice.idle_tick(self.cfg)
            except Exception as e:
                log("voice", "空闲卸载检查失败 %r" % (e,))
            time.sleep(60)

    def _maintenance_loop(self):
        fn = getattr(self.store, "maintenance", None)
        if not fn:
            log("maintenance", "store.maintenance 未实现，跳过")
            return
        while True:
            try:
                try:
                    self._rotate_side_logs()
                except Exception as e:
                    log("maint", "日志轮转异常 %r" % (e,))
                try:
                    self._disk_low_check()
                except Exception as e:
                    log("disk", "磁盘水位检查异常 %r" % (e,))
                try:
                    n = self.store.expire_shares(int(self.cfg.get("share_seen_window_h") or 48))
                    if n:
                        log("share", "%d 条分享超过窗口没动静，记为没看到" % n)
                except Exception as e:
                    log("share", "过期处理失败 %r" % (e,))
                try:
                    self.backfill_memory_vectors()
                except Exception as e:
                    log("memory", "向量补齐失败 %r" % (e,))
                try:
                    self.cleanup_duplicate_memories()
                except Exception as e:
                    log("memory", "重复记忆清理失败 %r" % (e,))
                try:
                    moved = self.store.favor_decay(int(self.cfg.get("favor_decay_days") or 14))
                    gone = self.store.decay_grudges(int(self.cfg.get("grudge_days") or 30))
                    if moved or gone:
                        log("relation", "好感回正 %d 人，淡化记仇 %d 条" % (moved, gone))
                except Exception as e:
                    log("relation", "日常回正失败 %r" % (e,))
                # v0.63.1：高分层（4/5 分 + 承诺）封顶——它们永不自动淡出，不封顶迟早吃满回想预算。
                # 超出的按「最久没被想起」降级（4->3、5->4、承诺 5->4->3），正文不动、不删除；承诺保到最后。
                try:
                    _cap = self.store.cap_high_tier(keep_max=int(self.cfg.get("memory_high_tier_max") or 25))
                    if _cap.get("demoted"):
                        log("memory", "高分层封顶：降级 %d 条（超每人/每群 %s 条上限）"
                            % (_cap.get("demoted"), self.cfg.get("memory_high_tier_max") or 25))
                except Exception as e:
                    log("memory", "高分层封顶失败 %r" % (e,))
                # v0.63.0：记忆分层遗忘（每天一次）。低价值 7/30 天、一般 90 天，
                # 重要(>=4)和承诺永不自动淡出；淡出的还能在面板捞回来。
                try:
                    if self.cfg.get("memory_decay_enabled", True):
                        _dm = self.store.decay_memories(
                            low_never_days=int(self.cfg.get("memory_decay_low_never_days") or 7),
                            low_days=int(self.cfg.get("memory_decay_low_days") or 30),
                            mid_days=int(self.cfg.get("memory_decay_mid_days") or 90))
                        if _dm.get("low") or _dm.get("mid"):
                            log("memory", "记忆淡出：低价值 %d 条，一般 %d 条" % (_dm.get("low", 0), _dm.get("mid", 0)))
                            self.store.add_log("memory_decay", "淡出 %d 条" % (_dm.get("low", 0) + _dm.get("mid", 0)))
                except Exception as e:
                    log("memory", "记忆衰减失败 %r" % (e,))
                res = fn()
                log("maintenance", "完成 %s" % (res if isinstance(res, dict) else ""))
            except Exception as e:
                log_exc("maintenance", e)
            time.sleep(24 * 3600)

    # ---------- 启动补消息（重启/看门狗窗口） ----------
    # 2026-10-25 修 P0-B：以前 _catchup 只入库不回复，窗口里的 @/私聊永远没有回；
    # 超过 180 秒的事件还在 handle() 里直接丢。现在：
    # - 缺失且 30 分钟内、@/私聊/叫名字的消息，生成带 _replay 标记的事件入队补答；
    # - 补答前查 message_id，已处理过的（入库过）不再答，避免重复；
    # - 不补答的普通消息仍然入库，保证上下文不断片；
    # - 平台重投/重放仍走 180 秒门，正常离线补推放宽到 30 分钟（见 _event_too_old）。
    # v0.55.0：补拉窗口 30 分钟 -> 6 小时，单次 50 条 -> 300 条，最多翻 4 页
    CATCHUP_WINDOW_SEC = 6 * 3600
    CATCHUP_REPLAY_MAX = 60
    CATCHUP_REPLAY_PER_CONV = 20
    CATCHUP_HISTORY_LIMIT = 1000
    CATCHUP_MAX_PAGES = 2
    CATCHUP_MAX_MESSAGES = 1200
    # v0.55.1：6 小时窗口只用来「补上下文」；真正补答限定在 30 分钟内，
    # 否则重启后会对几小时前的话突然回一句，很穿越。
    CATCHUP_REPLY_MAX_AGE_SEC = 30 * 60
    # 干跑：只拉、只入库、只打日志，不真的补答（验收用；默认关）
    CATCHUP_DRY = os.environ.get("DAFEIYU_CATCHUP_DRY") == "1"

    @staticmethod
    def _history_segs(m):
        """历史消息的 message 可能是数组，也可能只有 raw_message；统一成数组，别让 _parse 崩。"""
        segs = m.get("message")
        if isinstance(segs, list):
            return [s for s in segs if isinstance(s, dict)]
        raw = m.get("raw_message")
        if raw:
            return [{"type": "text", "data": {"text": str(raw)}}]
        return []

    def _catchup_wants_reply(self, segs, text, is_group):
        """这条漏掉的消息值不值得补答：私聊一律；群里只补 @/叫名字（和正常门控一致）。"""
        if not is_group:
            return True, "私聊"
        try:
            if not self.cfg.get("group_need_at"):
                # 配置成“群里不用 @ 也回”时，正常流程会回所有群消息，补推不能反而漏掉
                return True, "群聊不要求@"

            addressed, why = self._addressed(segs, text, None, is_group=True)
            if addressed:
                return True, why
            if self._cfg_on("group_trigger_name", True) and self._name_mentioned(text):
                return True, "叫了名字（补答时再判断）"
            return False, why
        except Exception as e:
            log("catchup", "判断要不要补答失败 %r" % (e,))
            return False, "判断异常"

    def _catchup_history(self, is_group, key):
        """v0.55.0：按 6 小时窗口翻页拉历史。

        一次先拿 CATCHUP_HISTORY_LIMIT 条；最早那条还在窗口内、又没拉满上限，
        就用 message_seq 往前翻（最多 CATCHUP_MAX_PAGES 页 / CATCHUP_MAX_MESSAGES 条）。
        私聊接口没有 message_seq，只拉一页。
        """
        cutoff = time.time() - self.CATCHUP_WINDOW_SEC
        got = []
        seq = None
        for page in range(self.CATCHUP_MAX_PAGES):
            try:
                if is_group:
                    h = self.onebot.get_group_msg_history(key, self.CATCHUP_HISTORY_LIMIT, message_seq=seq) or {}
                else:
                    h = self.onebot.get_friend_msg_history(key, self.CATCHUP_HISTORY_LIMIT) or {}
            except Exception as e:
                log("catchup", "拉历史失败 %r" % (e,))
                break
            msgs = [m for m in (h.get("messages") or []) if isinstance(m, dict)]
            if not msgs:
                break
            # v0.55.0：message_seq 翻页实测不生效（拿回的还是最新一页），按 message_id 去重，
            # 一页没有新东西就停，避免重复计数和多余请求。
            known = set(_as_int(m.get("message_id")) for m in got)
            fresh = [m for m in msgs if _as_int(m.get("message_id")) not in known]
            if not fresh:
                break
            got = fresh + got
            oldest = _as_int(msgs[0].get("time"))
            if oldest and oldest < cutoff:
                break
            if len(got) >= self.CATCHUP_MAX_MESSAGES:
                break
            if not is_group:
                break
            seq = (_as_int(msgs[0].get("message_seq")) or _as_int(msgs[0].get("real_id"))
                   or _as_int(msgs[0].get("message_id")))
            if not seq:
                log("catchup", "翻页拿不到 message_seq，停在第 %d 页" % (page + 1))
                break
        if len(got) > 1:
            log("catchup", "拉回 %d 条（%s %s）" % (len(got), "群" if is_group else "私聊", key))
        return got

    def _catchup(self):
        """启动时补重启/看门狗窗口漏掉的消息：该回的入队补答，其余的入库补上下文。"""
        total = 0
        queued = 0
        skipped = 0
        over_limit = 0
        img_desc_done = 0
        replay_events = []
        cutoff = time.time() - self.CATCHUP_WINDOW_SEC

        def collect(conv, is_group, gid, uid, m):
            nonlocal total, skipped, over_limit, img_desc_done
            if _as_int(m.get("time")) < cutoff:
                return
            sender = m.get("sender") or {}
            uid = (_as_int(m.get("user_id"))
                   or _as_int(sender.get("user_id")) or uid)
            if uid == self.self_id or blocked(self, uid):
                return
            mid = _as_int(m.get("message_id"))
            if not mid:
                return
            # v0.55.10：除了「库里已入库」，再查一遍持久去重表——重启后补拉时，
            # 只靠 messages 表会漏（入库可能因发送失败/提前 return 没走到），
            # 结果同一句话每次重启都被当新消息补答一遍（群里就是莫名多发两条）。
            try:
                _seen = self.store.has_message_id(mid) or (
                    self.store.seen_has(mid) if hasattr(self.store, "seen_has") else False)
            except Exception:
                _seen = True  # 查不了就当处理过：宁可漏答，也不重复刷屏
            if _seen:
                skipped += 1
                return
            name = sender.get("card") or sender.get("nickname") or str(uid)
            segs = self._history_segs(m)
            text, images, _, files, records = self._parse(segs)
            text = self._bot_voice_note_text(text, records, uid,
                                             (m or {}).get("message_id"), ("g:%d" % gid) if is_group else ("p:%d" % uid))
            if not text.strip() and not images:
                return
            if images and not text.strip():
                text = "[图片]"
            want, _why = self._catchup_wants_reply(segs, text, is_group)
            if want and (_as_int(m.get("time")) or 0) < (time.time() - self.CATCHUP_REPLY_MAX_AGE_SEC):
                # 太老：只入库补上下文，不补答（避免重启后回几小时前的话）
                want = False
                _why = "超过 30 分钟，只入库"
            if want and len(replay_events) < self.CATCHUP_REPLAY_MAX:
                # v0.55.10：入队前先记进持久去重表，免得这一轮没答完又重启时重复补答
                try:
                    self.store.seen_add(mid, _as_int(m.get("time")) or int(time.time()))
                except Exception:
                    pass
                if self.CATCHUP_DRY:
                    total += 1
                    log("catchup", "[dry] 本应补答 %s mid=%s" % (conv, mid))
                    return
                replay_events.append((
                    {"post_type": "message",
                     "message_type": "group" if is_group else "private",
                     "group_id": gid if is_group else 0,
                     "user_id": uid,
                     "message_id": mid,
                     "time": _as_int(m.get("time")),
                     "self_id": self.self_id,
                     "sender": sender,
                     "message": segs,
                     # _replay=补推重放；_catchup=已查过 message_id，按普通离线补推的
                     # 30 分钟口径处理（平台重投才走 180 秒门）。
                     "_replay": True,
                     "_catchup": True},
                    conv, uid, name, _as_int(m.get("time")) or int(time.time()), text))
                total += 1
                return
            if want:
                over_limit += 1
            # 不需要回的（或补答名额已满）：入库补上下文，保持原来的行为
            if images and img_desc_done < 2:
                try:
                    desc = self._describe(images[0])
                    if desc:
                        if text.strip() in ("", "[图片]"):
                            text = "[图片]（画面：%s）" % desc[:160]
                        else:
                            text = text + "（画面：%s）" % desc[:160]
                        img_desc_done += 1
                except Exception as e:
                    log("catchup", "看图失败 %r" % (e,))
            fwd = self._forward_from_segs(segs)
            atts = [{"type": "forward", "note": fwd, "ignored_users": self.store.blacklist_users()}] if fwd else []
            self.store.add_message(conv, uid, name, "in", text, "", mid, atts,
                                   ts=_as_int(m.get("time")) or None)
            total += 1

        try:
            for gid in _int_list(self.cfg.get("allow_groups")):
                try:
                    for m in self._catchup_history(True, gid):
                        collect("g:%d" % gid, True, gid, 0, m)
                except Exception as e:
                    log("catchup", "群 %s 失败 %r" % (gid, e))
            # 私聊：最近聊过的 10 个 + 白名单/主人，最多 20 个会话（漏掉的私聊也能补回）
            uids = []
            try:
                with self.store._lock:
                    rows = self.store.conn.execute(
                        "SELECT conv FROM visible_messages WHERE conv LIKE 'p:%' GROUP BY conv "
                        "ORDER BY MAX(id) DESC LIMIT 10").fetchall()
                for r in rows:
                    u = _as_int(str(r["conv"]).split(":")[-1])
                    if u and u not in uids:
                        uids.append(u)
            except Exception:
                pass
            for u in _int_list(self.cfg.get("allow_users")) + [_as_int(self.cfg.get("owner_qq"))]:
                if u and u not in uids:
                    uids.append(u)
            for uid in uids[:20]:
                try:
                    for m in self._catchup_history(False, uid):
                        collect("p:%d" % uid, False, 0, uid, m)
                except Exception as e:
                    log("catchup", "私聊 %s 失败 %r" % (uid, e))
        except Exception as e:
            log("catchup", "失败 %r" % (e,))

        # v0.55.1：补答前先确认协议端真的能用——重启时 NapCat 往往还没就绪，
        # 以前会在这时把补答全部打空（日志里一串 ConnectionRefused）。
        if replay_events:
            _deadline = time.time() + 90
            while time.time() < _deadline:
                try:
                    _info = self.onebot.get_login_info() or {}
                    if _as_int(_info.get("user_id")):
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                log("catchup", "协议端 90 秒内没就绪，本次只入库不补答")
                for ev, conv, uid, name, ts, text in replay_events:
                    try:
                        self.store.add_message(conv, uid, name, "in", text, "", ev.get("message_id"), [], ts=ts)
                    except Exception:
                        pass
                replay_events = []
        # 按时间顺序补答；同一会话最多 20 条，防止重启后对着旧消息连发
        replay_events.sort(key=lambda x: x[0].get("time") or 0)
        per_conv = {}
        for ev, conv, uid, name, ts, text in replay_events:
            if per_conv.get(conv, 0) >= self.CATCHUP_REPLAY_PER_CONV:
                over_limit += 1
                continue
            ok = False
            try:
                ok = bool(self.enqueue(ev))
            except Exception as e:
                log("catchup", "补答入队失败 %r" % (e,))
            if ok:
                queued += 1
                per_conv[conv] = per_conv.get(conv, 0) + 1
                log("catchup", "补答入队 %s mid=%s" % (conv, ev.get("message_id")))
            else:
                # 队列满/正在退出：至少把消息入库，这次不回，别让上下文缺一条
                try:
                    self.store.add_message(conv, uid, name, "in", text, "", ev.get("message_id"), [], ts=ts)
                except Exception as e:
                    log("catchup", "补答失败后入库也失败 %r" % (e,))
        log("catchup", "补回 %d 条，补答入队 %d 条，已处理跳过 %d 条%s%s" % (
            total, queued, skipped,
            ("，补答超上限 %d 条只入库" % over_limit) if over_limit else "",
            "（干跑：只有日志，没有真的补答）" if self.CATCHUP_DRY else ""))

    def _sync_whitelist_names(self):
        """只把白名单好友/主人的昵称补进 people；不把无关 QQ 好友拉进列表。"""
        try:
            if not self.self_id:
                return
            retry_at = _as_int(self.store.get_setting("whitelist_retry_at"))
            if retry_at and time.time() < retry_at:
                return
            allow = set(_int_list(self.cfg.get("allow_users")))
            owner = _as_int(self.cfg.get("owner_qq"))
            if owner:
                allow.add(owner)
            if not allow:
                return
            sig = ",".join(str(x) for x in sorted(allow))
            last = _as_int(self.store.get_setting("whitelist_names_at"))
            last_sig = self.store.get_setting("whitelist_sig") or ""
            if sig == last_sig and time.time() - last < 86400:
                return
            fl = self.onebot.get_friend_list() or []
            n = 0
            for f in fl:
                if not isinstance(f, dict):
                    continue
                qq = _as_int(f.get("user_id"))
                if qq in allow:
                    self.store.upsert_person(qq, f.get("remark") or f.get("nickname") or "", bump=False)
                    n += 1
            self.store.set_setting("whitelist_names_at", str(int(time.time())))
            self.store.set_setting("whitelist_sig", sig)
            self.store.set_setting("whitelist_retry_at", "0")
            gone = self.store.prune_people_scope(allow, _int_list(self.cfg.get("allow_groups")), owner)
            log("friends", "白名单昵称同步 %d 个，清理无关 %d 个" % (n, gone))
        except Exception as e:
            try:
                self.store.set_setting("whitelist_retry_at", str(int(time.time()) + 300))
            except Exception:
                pass
            log("friends", "白名单昵称同步失败（5 分钟后重试）%r" % (e,))

    def _login_loop(self):
        while True:
            try:
                info = self.onebot.get_login_info() or {}
                uid = _as_int(info.get("user_id"))
                if not uid:
                    log("login", "get_login_info 未返回 user_id，5 秒后重试")
                    time.sleep(5)
                    continue
                self.self_id = uid
                log("login", "self_id=%s nickname=%s" % (self.self_id, info.get("nickname")))
                self.store.add_log("login", json.dumps(info, ensure_ascii=False))
                try:
                    self._catchup()
                except Exception as e:
                    log("catchup", "启动补消息失败 %r" % (e,))
                threading.Thread(target=self._backfill, daemon=True).start()
                threading.Thread(target=self._sync_whitelist_names, daemon=True).start()
                return
            except Exception as e:
                log("login", "NapCat 还没就绪：%r，5 秒后重试" % (e,))
                time.sleep(5)

    # ---------- 群历史回填 ----------
    def _backfill(self):
        try:
            if str(self.store.get_setting("backfill_done") or "") == "1":
                return
            failed = False
            for gid in _int_list(self.cfg.get("allow_groups")):
                try:
                    h = self.onebot.get_group_msg_history(gid, 30) or {}
                    for m in (h.get("messages") or []):
                        sender = m.get("sender") or {}
                        sid = _as_int(sender.get("user_id"))
                        if blocked(self, sid):
                            continue
                        direction = "out" if sid == self.self_id else "in"
                        text = m.get("raw_message") or ""
                        if text:
                            self.store.add_message("g:%d" % gid, sid, sender.get("card") or sender.get("nickname") or "",
                                                   direction, text, "", m.get("message_id") or 0)
                    self.store.add_log("backfill", "group %s ok" % gid)
                except Exception as e:
                    failed = True
                    log("backfill", "群 %s 失败 %r" % (gid, e))
            if failed:
                log("backfill", "有群失败，暂不写 backfill_done，下次重启再试")
            else:
                self.store.set_setting("backfill_done", "1")
        except Exception as e:
            log_exc("backfill", e)

    # ---------- 提醒 / 日记 / 记忆 ----------
    def _reminder_line(self, r):
        """v0.56.0 到点说人话：用自己的口气喊人，失败才用兜底。"""
        conv = str(r.get("conv") or "")
        what = str(r.get("text") or "").strip()
        tgt = str(r.get("target_name") or "").strip()
        try:
            if not tgt and conv.startswith("p:"):
                tgt = self._person_name(int(conv[2:])) or ""
        except Exception:
            pass
        try:
            sys_p = self.persona.system_prompt(False, tgt or "对方", conv.startswith("g:"),
                                               now_line=now_line())
            prompt = ("现在是%s。你之前答应到点叫%s：%s。\n"
                      "用你自己的口气说一句喊他的话，8~40 字；"
                      "这条里带了具体的事（考什么/开什么会/吃什么药）就把那件事说出来，"
                      "别只说“该动了”；"
                      "不许写“提醒你”三个字；不许用对方叫你的称呼；只输出这一句。") % (
                now_line(), ("「%s」" % tgt) if tgt else "对方", what or "到点了")
            rr = self.pool.chat(sys_p, [], prompt, max_tokens=100, temperature=0.8, thinking=False)
            if getattr(rr, "ok", False):
                line = guard.scrub(str(getattr(rr, "text") or "").strip())[:80]
                if line and guard.check(line)["action"] == "ok":
                    return line
        except Exception as e:
            log("reminder", "生成文案失败 %r" % (e,))
        return ("到点了，%s。%s" % (tgt or "你", what or "该动了")).strip()

    def _deliver_due_reminders(self):
        """把到点的提醒发出去（单独一个方法，自测可以直接调一轮，不用起死循环）。"""
        for r in self.store.due_reminders():
                    if r["id"] in self._reminder_giveup:
                        continue
                    conv = r["conv"]
                    target = conv.split(":")
                    line = self._reminder_line(r)
                    ok = False
                    if len(target) == 2 and target[0] == "g":
                        segs = []
                        uid = _as_int(r.get("target_uid"))
                        if uid:
                            segs.append(at_seg(int(uid)))
                        segs.append(text_seg(line))
                        ok = self._send(True, int(target[1]), 0, segs)
                    elif len(target) == 2 and target[0] == "p":
                        ok = self._send(False, 0, int(target[1]), [text_seg(line)])
                    if ok:
                        self.store.mark_reminder(r["id"])
                        try:
                            self.store.add_message(conv, self.self_id, self.cfg.get("bot_name"), "out",
                                                   line, "", 0)
                            self.store.touch_session(conv)
                        except Exception:
                            pass
                        self._reminder_fail.pop(r["id"], None)
                        log("reminder", "sent=True conv=%s" % conv)
                    else:
                        attempts = self._reminder_fail.get(r["id"], 0) + 1
                        if attempts >= 5:
                            # 失败不 mark，只在本进程内停止重试；DB 里仍保留，重启可再试
                            self._reminder_fail.pop(r["id"], None)
                            self._reminder_giveup.add(r["id"])
                            log("reminder", "5 次失败，本进程暂停重试 conv=%s" % conv)
                        else:
                            self._reminder_fail[r["id"]] = attempts
                            log("reminder", "发送失败 conv=%s attempts=%d/5，稍后重试" % (conv, attempts))

    def _reminder_loop(self):
        while True:
            try:
                self._deliver_due_reminders()
            except Exception as e:
                log_exc("reminder", e)
            time.sleep(20)

    def _diary_loop(self):
        while True:
            try:
                now = time.localtime()
                day = time.strftime("%Y-%m-%d", now)
                if now.tm_hour == 23 and now.tm_min >= 50 and day not in self.day_done:
                    self.day_done.add(day)
                    self._write_diaries(day)
            except Exception as e:
                log_exc("diary", e)
            time.sleep(120)

    DAY_DIARY_CONV = "__day__"

    def _diary_transcript(self, day):
        """把这一天的所有会话按时间拼成一份；日记是「我的一天」，不再按会话分开写。"""
        rows = self.store.day_messages(day)
        if not rows:
            return "", 0
        names = {}
        try:
            names = {str(c.get("conv")): str(c.get("name") or "") for c in (self.store.conv_names() or [])}
        except Exception:
            names = {}
        lines = []
        for r in rows:
            t = " ".join(str(r.get("text") or "").split())
            if not t:
                continue
            conv = str(r.get("conv") or "")
            if r.get("direction") == "out":
                who = "我"
            else:
                who = str(r.get("sender_name") or "").strip() or "对方"
            if conv.startswith("g:"):
                tag = "［群 %s］" % (names.get(conv) or conv[2:])
            elif names.get(conv):
                tag = "［私聊 %s］" % names.get(conv)
            else:
                tag = ""
            lines.append("%s%s：%s" % (tag, who, t[:150]))
        whole = chr(10).join(lines)
        if len(whole) > 14000:
            # 一天消息多的时候：留开头一小时和后面大部分，中间标省略，别把早上全丢掉
            head = chr(10).join(lines[:20])[:3000]
            tail = ""
            for line in reversed(lines[20:]):
                tail = line + chr(10) + tail
                if len(tail) > 11000:
                    break
            omitted = max(0, len(lines) - 20 - len(tail.splitlines()))
            whole = head + chr(10) + "……（中间省略约 " + str(omitted) + " 条：都在这天里，只是没全贴出来）……" + chr(10) + tail
        return whole, len(lines)

    def _mood_arc_for_day(self, day):
        """v0.64.0：把今天每小时的快心情按顺序拼成一条线，给日记当素材。"""
        try:
            start = int(time.mktime(time.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")))
            end = start + 86400
            fn = getattr(self.store, "emotion_history_between", None)
            evs = fn(start, end, 0) if callable(fn) else []
        except Exception:
            evs = []
        if not evs:
            return ""
        names = {"jealousy": "吃醋", "upset": "不满", "sad": "难过", "happy": "开心", "shy": "害羞"}
        seq = [names.get(str(e.get("kind") or ""), "") for e in evs if e.get("kind")]
        seq = [x for x in seq if x]
        if not seq:
            return ""
        shown = seq[:12]
        line = "今天的心情变化（只给自己看，不用写成清单）：" + " → ".join(shown)
        if len(seq) > len(shown):
            line += " 等"
        return line + "。\n"

    def _write_diaries(self, day, overwrite=False):
        """v0.53.0：一天只写一篇，第一人称，写「我今天经历了什么」。

        以前是每个会话（每个群/每个私聊）各写一篇，一天下来好几条，读起来像聊天摘要。
        现在合成一篇：只挑 1~3 件真有情绪的事，不流水账。
        """
        try:
            if not self.store.day_messages(day):
                return
        except Exception:
            return
        if not overwrite:
            try:
                # v0.53.0：那天已经有日记（不管以前是按群写的还是新的全局日记）就不再写第二篇
                if any(str(d.get("day")) == day for d in self.store.list_diaries(200)):
                    return
            except Exception as e:
                log("diary", "读取已有日记失败，跳过本次写入 %r" % (e,))
                return
        text, n = self._diary_transcript(day)
        if not text:
            return
        name = str(self.cfg.get("bot_name") or "机器人")
        mood_line = self._mood_arc_for_day(day)
        if getattr(self.persona, 'unified_self', False):
            mood_line += self.store.inner_context(private=True, day=day)
        prompt = DIARY_PROMPT.format(name=name, day=day) + chr(10) + (mood_line or "") + "今天的聊天（%d 条，按时间）：" % n + chr(10) + text
        r = self.pool.chat("你就是%s本人，正在写只有自己看的日记。" % name + self.store.self_context(), [], prompt,
                           max_tokens=1100, temperature=0.75, thinking=False)
        body = (r.text or "").strip() if r.ok else ""
        if not body:
            log("diary", "写日记失败：%s" % (str(getattr(self.pool, "last_error", "") or "")[:160]))
            return
        self.store.upsert_diary(self.DAY_DIARY_CONV, day, body[:2000])
        log("diary", "写好 %s 的日记：%d 字" % (day, len(body)))

    def _backfill_kb_vectors(self):
        try:
            total = 0
            model = str(self.cfg.get("sticker_embed_model") or "")
            for _ in range(10):
                rows = self.store.kb_chunks_missing_vec(500)
                if not rows:
                    break
                vs = embed_texts(self.cfg, [r["text"] for r in rows])
                if len(vs) != len(rows):
                    break
                for r, v in zip(rows, vs):
                    self.store.set_kb_vec(r["id"], v, model)
                total += len(rows)
            if total:
                log("kb", "补知识库向量 %d 段" % total)
        except Exception as e:
            log("kb", "补知识库向量失败 %r" % (e,))

    def _worker(self):
        while True:
            try:
                ev = self.q.get()
            except Exception:
                continue
            try:
                # v0.58.0 私聊攒批：该攒的立刻进池返回（不在 worker 里 sleep，否则会卡住别的会话）；
                # 安静的定时器到点后会经 _enqueue_merged 把合并事件放回队列，再由某个 worker 走 handle。
                if self._should_batch(ev):
                    self._feed_batch(ev)
                else:
                    self.handle(ev)
            except Exception as e:
                log_exc("worker", e)
            finally:
                self.q.task_done()

    def enqueue(self, ev):
        if blocked(self, ev.get("user_id")):
            return True
        if ev.get("post_type") == "message" and not ev.get("message_sent"):
            self.dreams.touch()
            self._self_last_activity=time.monotonic()
        if self.stopping.is_set():
            # 优雅退出中：回 503 让 NapCat 保留事件，下次启动由 catchup 补，别静默丢
            return False
        try:
            self.q.put_nowait(ev)
            return True
        except queue.Full:
            with self._queue_lock:
                self.queue_drops += 1
                n = self.queue_drops
            log("queue", "满了，丢弃一条（累计 %d）" % n)
            return False
        except Exception as e:
            log_exc("queue", e)
            return False

    # ---------- 私聊攒批（v0.58.0） ----------
    # 02 没有独立的 /slash 指令；这里的「指令」是 _reply 里的自然语言命令分支（写文档/提醒/发空间…）。
    # 指令不进池：命令要按原话立刻执行，不能被后面几句合并改掉含义。
    # 词表和 flow_reply.py 的命令分支（约 1670 行、1849 行提醒）保持同一套。
    _CMD_HEAD_RE = re.compile(
        r"^(?:/\S|拉黑|取消拉黑|解除拉黑|写文档|文档|做表格|表格|做PPT|做ppt|PPT|画一张|画一幅|画个|画张|画|帮我画|给我画|"
        r"提醒|入库|存资料|加入知识库|发空间|空间文案|发图|发网图|写日记|补写日记)")
    # 混合版（官方通道）的 8 位认主口令；协议版目前没有认主流程，这里只是照口径留个口子，别让它被攒批改写。
    _CLAIM_RE = re.compile(r"^认主\s*\d{8}$")

    @staticmethod
    def _plain_text(segs):
        """只取 text 段的字（不查库、不调模型），给「要不要攒」这类门控判断用。"""
        return "".join(str((s.get("data") or {}).get("text") or "")
                       for s in (segs or [])
                       if isinstance(s, dict) and s.get("type") == "text").strip()

    def _sync_batcher(self):
        """面板改完配置约 60 秒内热生效：每次进池前按当前 cfg 对齐一次参数。"""
        cfg = self.cfg
        self.batcher.configure(
            debounce_sec=float(cfg.get("private_batch_sec") or 3.0),
            max_msgs=int(cfg.get("private_batch_max_msgs") or 0),
            max_wait_sec=float(cfg.get("private_batch_max_wait_sec") or 0.0))

    def _private_allowed(self, uid):
        """收到的私聊权限；不用于主动聊天候选池。好友身份不是回复前提。"""
        uid = _as_int(uid)
        return uid > 0 and not blocked(self, uid) and (bool(self.cfg.get("allow_all_users"))
                            or uid == _as_int(self.cfg.get("owner_qq"))
                            or uid in _int_list(self.cfg.get("allow_users")))

    def _should_batch(self, ev):
        """这条消息要不要进攒批池。按当前私聊回复权限放行。"""
        if not isinstance(ev, dict):
            return False
        if blocked(self, ev.get("user_id")):
            return False
        if ev.get("post_type") != "message":
            return False
        if ev.get("message_sent"):
            return False
        # 测试事件 / 启动补答 不进池：补答是重启后补上下文的，必须原样快过
        if ev.get("_test") or ev.get("test_mode") or ev.get("_catchup"):
            return False
        # 已经合并过的事件不再进池（防止合并事件又被兜一圈反复攒）
        if ev.get("_batched"):
            return False
        if ev.get("message_type") != "private":
            return False
        try:
            if not self._cfg_on("private_batch", True):
                return False
            if self.cfg.get("bot_paused"):
                return False
        except Exception:
            return False
        sid = _as_int(ev.get("self_id"))
        if not sid:
            return False
        # 协议端真实推送：self_id 必须是本机小号（self_id 还没就绪时不拦，handle 里还会再查一次）
        if self.self_id and sid != self.self_id:
            return False
        uid = _as_int(ev.get("user_id"))
        if not uid or uid == (self.self_id or sid):
            return False
        # 指令 / 认主口令不进池（见上面 _CMD_HEAD_RE / _CLAIM_RE 的说明）
        _text = self._plain_text(ev.get("message") or [])
        if self._CLAIM_RE.match(_text) or self._CMD_HEAD_RE.match(_text):
            return False
        return self._private_allowed(uid)

    def _feed_batch(self, ev):
        """私聊消息进池（立刻返回，不等）。到条数上限时 batcher 会立即回调入队。"""
        uid = _as_int(ev.get("user_id"))
        if blocked(self, uid):
            return
        try:
            self._sync_batcher()
        except Exception:
            pass
        self.batcher.feed("p:%d" % uid, ev)

    def _enqueue_merged(self, merged):
        """攒批合并后的事件回队列。on_flush 在定时器线程里跑，这里只做入队，不做任何耗时操作。

        故意不走 enqueue()：enqueue 在 stopping 时回 False（那是给 NapCat 留事件用的），
        但合并事件是「已经收下的事件」，退出 drain 时也得处理掉，不能静默丢。
        """
        if blocked(self, merged.get("user_id")):
            return True
        try:
            self.q.put_nowait(merged)
            return True
        except queue.Full:
            with self._queue_lock:
                self.queue_drops += 1
                n = self.queue_drops
            log("queue", "满了，丢弃一条（攒批合并，累计 %d）" % n)
            return False
        except Exception as e:
            log_exc("queue", e)
            return False

    def _conv_lock(self, conv):
        with self.conv_locks_lock:
            lk = self.conv_locks.get(conv)
            if lk is None:
                lk = threading.Lock()
                self.conv_locks[conv] = lk
            return lk

    def _mark_seen(self, mid):
        with self.seen_lock:
            if mid in self.seen:
                return False
            self.seen.add(mid)
            self.seen_order.append(mid)
            while len(self.seen) > self.seen_order.maxlen:
                old = self.seen_order.popleft()
                self.seen.discard(old)
        # v0.55.0：同时写库，重启后仍然去重
        try:
            self.store.seen_add(mid)
        except Exception:
            pass
        return True

    # ---- 事件时效（2026-10-25） ----
    # 30 分钟：正常离线补推（NapCat 重连后才送到）也要回，不能因为晚几分钟就丢。
    EVENT_MAX_AGE_SEC = 30 * 60
    # 180 秒：只留给「平台重投 / 其它重放」这类带旧 time 的事件。启动补推带 _catchup，
    # 已经查过库里有没有处理过，按普通离线补推的 30 分钟算；否则重启窗口 3~30 分钟的
    # @ 和私聊还是补不回来（P0-B 的原病）。
    REPLAY_MAX_AGE_SEC = 180

    def _event_too_old(self, ev):
        """返回 (是否过期, 事件年龄秒, 采用的时效上限)。"""
        ts = _as_int(ev.get("time"))
        if not ts:
            return False, 0.0, self.EVENT_MAX_AGE_SEC
        age = max(0.0, time.time() - ts)
        replay = bool(ev.get("_redelivery") or ev.get("replay"))
        if ev.get("_replay") and not ev.get("_catchup"):
            replay = True
        if ev.get("_catchup"):
            # v0.55.0：启动补拉是 6 小时窗口，补答事件必须按同一窗口放行，
            # 否则拉回来 6 小时的消息会被 30 分钟门整批丢掉。
            return age > self.CATCHUP_WINDOW_SEC, age, self.CATCHUP_WINDOW_SEC
        limit = self.REPLAY_MAX_AGE_SEC if replay else self.EVENT_MAX_AGE_SEC
        return age > limit, age, limit

    # ---------- 面板「为什么没回」试算（v0.55.1，不调模型、不发消息、不写库） ----------
    def explain_gate(self, is_group, gid, uid, name, text, quote=None):
        """把群/私聊门控逐条走一遍，返回每一步的判断结果。

        口径与 handle() 一致；**不调用模型**（名字判断那一步只标"需要模型"），
        不改互刷计数（用 BotRoundGuard.peek），不写库。
        """
        steps = []

        def add(ok, label, detail=""):
            steps.append({"ok": ok, "label": label, "detail": str(detail)[:160]})

        cfg = self.cfg
        uid = _as_int(uid)
        gid = _as_int(gid)
        text = str(text or "")
        if blocked(self, uid):
            add(False, "黑名单：完全忽略", "不回复、不入库、不进入上下文")
            return {"reply": False, "steps": steps}
        if cfg.get("bot_paused"):
            add(False, "机器人处于「停用」", "面板标题栏「恢复使用」后才会回")
            return {"reply": False, "steps": steps}
        add(True, "机器人未停用")
        if not is_group:
            owner = _as_int(cfg.get("owner_qq"))
            allow = _int_list(cfg.get("allow_users"))
            ok = self._private_allowed(uid)
            add(ok, "私聊回复权限", "QQ %s；允许所有人=%s，主人=%s，白名单 %d 人" % (
                uid, bool(cfg.get("allow_all_users")), owner or "未设置", len(allow)))
            if ok and reply_autonomy.enabled(cfg):
                add(None, "自主接话", "允许进入流程，由它结合心情和上下文决定是否接话；试算不调模型")
            return {"reply": bool(ok), "maybe": bool(ok and reply_autonomy.enabled(cfg)), "steps": steps}
        allow_g = _int_list(cfg.get("allow_groups"))
        in_list = gid in allow_g
        add(in_list, "群在白名单里", "群 %s；白名单共 %d 个群" % (gid, len(allow_g)))
        if not in_list:
            return {"reply": False, "steps": steps}
        segs = [{"type": "text", "data": {"text": text}}]
        addressed, reason = self._addressed(segs, text, quote, is_group=True)
        need = bool(cfg.get("group_need_at"))
        name_needs_model = bool(need and not addressed and self._cfg_on("group_trigger_name", True)
                                and self._name_mentioned(text))
        if addressed:
            add(True, "被 @ / 被引用 / 叫到本鲸", reason)
        elif name_needs_model:
            add(None, "被 @ / 被引用 / 叫到本鲸", (reason or "") + "；正文里有名字，真实运行要再让模型判断一次")
        else:
            add(False, "被 @ / 被引用 / 叫到本鲸", reason)
        if not addressed and self._at_blocked(segs):
            add(False, "只 @ 了别人 / @全体", "不算在叫它")
            return {"reply": False, "maybe": False, "steps": steps}
        if need and not addressed and not name_needs_model:
            add(False, "群门控：没有叫它 → 只入库、不回复", "group_need_at 打开时，三种触发之一才行")
            return {"reply": False, "maybe": False, "steps": steps}
        if name_needs_model:
            add(None, "群门控：是否进入流程取决于名字判断", "确实是在叫它才进入流程；自主接话开启时还会自己决定要不要接")
            return {"reply": True, "maybe": True, "steps": steps}
        try:
            allow_round, streak, _ = self.bot_rounds.peek(cfg, gid, uid, counts=bool(addressed) or not need)
            add(allow_round, "机器人互刷刹车",
                "这个机器人连续 %d 条（上限 %s）；真人说一句会清零"
                % (streak, _as_int(cfg.get("bot_round_limit")) or 6))
        except Exception as e:
            add(None, "互刷刹车检查失败", repr(e)[:80])
        if reply_autonomy.enabled(cfg):
            add(None, "自主接话", "被叫到也是邀请；它可以接话、发呆或保持安静，试算不调模型")
        reply = all(s["ok"] is not False for s in steps)
        maybe = any(s["ok"] is None for s in steps)
        return {"reply": reply, "maybe": maybe, "steps": steps}

    @policy_scope
    def handle(self, ev):
        if blocked(self, ev.get("user_id")):
            return
        if ev.get('post_type')=='notice':
            return self._handle_poke(ev)
        if ev.get("post_type") != "message":
            return
        if ev.get("message_sent"):
            return
        if self.cfg.get("bot_paused"):
            log("paused", "暂停使用中，丢弃一条 mid=%s" % ev.get("message_id"))
            return
        # 自测事件：直接用模型级脚本验证（notes/*自测.py），别把测试内容写进真库和聊天记录
        if ev.get("_test") or ev.get("test_mode"):
            log("skip", "测试事件，直接丢弃（不回复、不写库）")
            return
        mid = ev.get("message_id")
        # 防呆：只接受协议端真实推送（必须带 self_id，且等于本机小号）
        sid = _as_int(ev.get("self_id"))
        if not sid or (self.self_id and sid != self.self_id):
            log("skip", "丢弃没有 self_id 的伪造事件 mid=%s" % mid)
            return
        uid = _as_int(ev.get("user_id"))
        if uid and uid == self.self_id:
            return
        # 幂等：库里已有这个 message_id 说明已经处理/入库过（重启后平台重投、或补推重复），
        # 不再回复，避免同一条 @ 被答两遍。只对“不是刚到的新消息”查库：
        # 刚到的实时事件可能被 _backfill 抢先入库了同一条，按库去重会把它误吞掉；
        # 60 秒内的重复由内存 seen 兜住。_catchup 的补推在入队前已经查过一次库，
        # 这里必须放行，否则 _backfill 在 worker 回复前把同一条插进库，补答就会被吞掉。
        ts = _as_int(ev.get("time"))
        age = (time.time() - ts) if ts else 0.0
        if (mid and age > 60 and not ev.get("_catchup")
                and self.store.has_message_id(_as_int(mid))):
            log("skip", "重复消息 mid=%s（库里已有，不再回复）" % mid)
            return
        too_old, age, limit = self._event_too_old(ev)
        if too_old:
            log("skip", "过期消息 mid=%s age=%.0fs limit=%ds" % (mid, age, limit))
            return
        is_group = ev.get("message_type") == "group"
        if is_group and not self.self_id:
            log("skip", "self_id 未就绪，群消息暂不处理")
            return
        if not self._mark_seen(mid):
            return
        # v0.58.0 私聊攒批：合并事件只带最后一条的 message_id，同批其它编号也得标 seen。
        # 否则平台重投前面那几条时会当成新消息，把同一轮话再答一遍。
        _bids = ev.get("_batch_ids") or []
        if _bids:
            for _bid in _bids:
                if _bid and _bid != mid:
                    self._mark_seen(_bid)
            try:
                _bn = int(ev.get("_batched") or len(_bids))
            except Exception:
                _bn = len(_bids)
            if _bn > 1:
                _blen = sum(len(str((s.get("data") or {}).get("text") or ""))
                            for s in (ev.get("message") or [])
                            if isinstance(s, dict) and s.get("type") == "text")
                log("batch", "攒批 %d 条合并处理（pid=%d 会话=p:%d 字数=%d）"
                    % (_bn, os.getpid(), _as_int(ev.get("user_id")), _blen))

        gid = _as_int(ev.get("group_id")) if is_group else 0
        conv = ("g:%d" % gid) if is_group else ("p:%d" % uid)
        sender = ev.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or str(uid)
        if handle_blacklist_command(self, ev, uid, gid, is_group):
            return
        segs = ev.get("message") or []
        text, images, mentions_me, files, records = self._parse(segs)
        # v0.69.3：机器人发的语音条自动转文字，别的模型才「听得见」说了什么
        text = self._bot_voice_note_text(text, records, uid,
                                         ev.get("message_id"), conv)
        quote = self._quote_info(self._reply_id(segs), uid, name)
        # 群聊门控只决定是否把消息交给它；被叫到/私聊也由自主接话决定要不要聊。
        # 没 @ 别人时，短名命中先判断是不是叫本鲸；只 @ 别人或 @全体不算邀请。
        addressed, addr_reason = self._addressed(segs, text, quote, is_group=is_group)
        at_kind = ""
        types = [s.get("type") for s in segs if isinstance(s, dict)]
        has_text = any((s.get("type") == "text" and str((s.get("data") or {}).get("text") or "").strip())
                       for s in segs if isinstance(s, dict))
        image_segs = [s for s in segs if isinstance(s, dict) and s.get("type") in ("image", "mface")]
        sticker_segs = [s for s in image_segs if self._is_sticker_seg(s)]
        sticker_only = (bool(sticker_segs) and len(sticker_segs) == len(image_segs)
                        and not has_text and not files and not records)
        owner_qq = _as_int(self.cfg.get("owner_qq"))
        is_owner = bool(owner_qq) and uid == owner_qq

        # 白名单：只有主人 / 白名单好友 / 白名单群
        if is_group:
            if gid not in _int_list(self.cfg.get("allow_groups")):
                self.store.add_log("skip_group", "gid=%s name=%s" % (gid, name), False)
                return
            ev["_forward_text"] = self._forward_from_segs(segs)
            companion_generation = self._companion_observe(gid)
            if not (ev.get("_catchup") or ev.get("_replay")):
                private_contact.observe(self, "p:%d" % uid, text, ts, mid, private=False)
            # 群聊门控：主人也要 @ 或叫名字；只 @ 别人 / @全体 不回。
            self._remember_sender(conv, uid, name, ev)
            # v0.23.0：短名「大肥鱼」和群里的同名号撞名，命中后先让模型判断
            # 是在叫本鲸/问本鲸的事，还是只是提到；判断失败按不回处理。
            if (self.cfg.get("group_need_at") and not addressed
                    and not self._at_blocked(segs) and self._cfg_on("group_trigger_name", True)
                    and self._name_mentioned(text)):
                judged, jreason = self._judge_name_mention(conv, uid, name, text, segs, quote)
                if judged:
                    addressed, addr_reason = True, "名字判断：" + jreason
                else:
                    addr_reason = "名字判断：" + jreason
            # v0.54.15：主人对着“本鲸刚发的表情包”说不想要时，不 @ 也算在跟本鲸说话
            # （只认主人；只在本鲸说完、还没别人插话的 5 分钟内；正文要提到“表情”）。
            if (self.cfg.get("group_need_at") and not addressed and is_owner
                    and self._sticker_bad_hit(text, strict=True)
                    and self._self_sticker_recent(conv, 300)):
                addressed, addr_reason = True, "表情包纠错"
            # v0.65.5：刚点歌列了候选，对方回序号不 @ 也算在跟本鲸说话
            # （只认点歌那个人、只认 1~N、5 分钟内；别人回数字不抢答）
            if (self.cfg.get("group_need_at") and not addressed
                    and self._song_pick_hit(conv, uid, text)):
                addressed, addr_reason = True, "点歌选序号"
            if companion_generation:
                self._companion_quiet_request(gid, text, addressed)
            if addressed:
                at_kind = group_at_kind(addr_reason)
            # v0.54.9：机器人互刷刹车（只对名单里的 QQ；真人一句就清零）
            if is_group:
                _counts = bool(addressed) or not self.cfg.get("group_need_at")
                _allow, _streak, _reset_from = self.bot_rounds.check(self.cfg, gid, uid, counts=_counts)
                if not _allow:
                    self.store.add_log("bot_rounds", "gid=%s 与QQ%d连续%d轮（上限%d），这条不回"
                                       % (gid, uid, _streak, _as_int(self.cfg.get("bot_round_limit")) or 6), False)
                    self._store_passive(conv, uid, name, text, images, files, records, ev, quote=quote)
                    return
                if _reset_from:
                    self.store.add_log("bot_rounds", "真人开口，互刷计数清零（之前 %d 轮）" % _reset_from, True)
            if self.cfg.get("group_need_at") and not addressed:
                # 关键是“只存不回”：不叫它的消息也入库，它才知道“上面那两张图”是什么。
                self._store_passive(conv, uid, name, text, images, files, records, ev, quote=quote)
                if mid and self.store.has_message_id(_as_int(mid)):
                    self._companion_schedule(gid, uid, text, segs, companion_generation, quote)
                self.store.add_log("skip_group_noat", "gid=%s name=%s reason=%s"
                                   % (gid, name, addr_reason[:40]), False)
                return
        else:
            if not self._private_allowed(uid):
                self.store.add_log("skip_private", "uid=%s name=%s" % (uid, name), False)
                return
            ev["_forward_text"] = self._forward_from_segs(segs)
            self._remember_sender(conv, uid, name, ev)
        if not (ev.get("_catchup") or ev.get("_replay")):
            if not is_group:
                private_contact.observe(self, conv, text, ts, mid)
        if not text.strip() and not images:
            return

        t0 = time.time()
        try:
            with self._conv_lock(conv):
                self._reply(conv, uid, gid, name, is_owner, is_group, text, images, ev,
                            files=files, records=records, quote=quote, sticker_only=sticker_only,
                            at_kind=at_kind)
                if is_group and self._companion_enabled(gid) and self._group_speak_last_from_bot(conv):
                    self._companion_state.awaken(gid)
        finally:
            self._note_latency(conv, time.time() - t0)


class HookHandler(BaseHTTPRequestHandler):
    def _authorized(self):
        try:
            token = Bot.instance.cfg.get("onebot_token") if Bot.instance else ""
        except Exception:
            token = ""
        if not token:
            return False
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/hook/"):
            return hmac.compare_digest(path[len("/hook/"):], token)
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:].strip(), token)
        return False

    def _read_body(self, limit=4 * 1024 * 1024):
        """兼容 Content-Length 与 chunked（NapCat 用 chunked 发事件），先限额再分配。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in te:
                out = bytearray()
                while True:
                    line = self.rfile.readline(1024)
                    if not line:
                        break
                    try:
                        size = int(line.strip().split(b";")[0] or b"0", 16)
                    except Exception:
                        raise ValueError("bad chunk size")
                    if size < 0:
                        raise ValueError("negative chunk size")
                    if size == 0:
                        self.rfile.readline(1024)
                        break
                    if len(out) + size > limit:
                        raise BodyTooLarge()
                    chunk = self.rfile.read(size)
                    if not chunk:
                        break
                    out.extend(chunk)
                    if len(chunk) != size:
                        break
                    self.rfile.read(2)
                return bytes(out)
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0:
                raise ValueError("negative content length")
            if n > limit:
                raise BodyTooLarge()
            return self.rfile.read(n) if n else b""
        except BodyTooLarge:
            raise
        except ValueError:
            raise
        except Exception as e:
            log("hook", "读取请求体失败 %r" % (e,))
            return b""

    def do_POST(self):
        try:
            self.connection.settimeout(10)
        except Exception:
            pass
        try:
            hook_path = urllib.parse.urlsplit(self.path).path
            if hook_path.startswith("/hook/"):
                hook_path = "/hook/<hidden>"
            log("hook", "收到回调 path=%s len=%s peer=%s" % (hook_path,
                                                          self.headers.get("Content-Length"), self.client_address[0]))
        except Exception:
            pass
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        try:
            body = self._read_body()
        except BodyTooLarge:
            self.send_response(413)
            self.end_headers()
            return
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        if not body:
            self.send_response(204)
            self.end_headers()
            return
        try:
            ev = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        instance = Bot.instance
        ok = instance.enqueue(ev) if instance else False
        self.send_response(204 if ok else 503)
        self.end_headers()

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/probe_gate":
            if not self._authorized():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"token"}')
                return
            try:
                q = urllib.parse.parse_qs(parts.query)
                one = lambda k, d="": (q.get(k) or [d])[0]
                bot = Bot.instance
                if not bot:
                    raise RuntimeError("bot 还没启动")
                res = bot.explain_gate(
                    is_group=one("is_group", "1") not in ("0", "false", "False"),
                    gid=one("gid", "0"),
                    uid=one("uid", "0"),
                    name=one("name", ""),
                    text=one("text", ""))
                body = json.dumps({"ok": True, "result": res}, ensure_ascii=False).encode("utf-8")
            except Exception as e:
                body = json.dumps({"ok": False, "error": repr(e)[:200]}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "bot": "dafeiyu-pc"}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        return



if __name__ == "__main__":
    Bot().start()
