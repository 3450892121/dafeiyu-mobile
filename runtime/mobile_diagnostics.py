"""Content-free diagnostics for this instance, available even while QQ is offline."""
import hmac
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time


class HookHealth:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.value = {'pid': os.getpid(), 'started_at': int(time.time()),
                      'statuses': {}, 'messages': {'private': 0, 'group': 0}}

    def record(self, status, event=None):
        # Telemetry must not affect acceptance, expose event data, or rotate any credential.
        try:
            with self.lock:
                key = str(status)
                self.value['statuses'][key] = self.value['statuses'].get(key, 0) + 1
                if status == 204 and isinstance(event, dict) and event.get('post_type') == 'message':
                    kind = event.get('message_type')
                    if kind in self.value['messages']:
                        self.value['messages'][kind] += 1
                self.value['updated_at'] = int(time.time())
                name = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                                     prefix='.hook-health-', delete=False) as output:
                        name = output.name
                        json.dump(self.value, output)
                    os.replace(name, self.directory / 'mobile-hook-health.json')
                finally:
                    if name and os.path.exists(name):
                        os.unlink(name)
        except Exception:
            pass


def _json(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _recent_log(path, since):
    try:
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - 200000))
            text = source.read(200000).decode('utf-8', 'replace')
    except OSError:
        return ''
    out = []
    keep = False
    for line in text.splitlines():
        stamp = re.search(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', line[:100])
        if stamp:
            try:
                keep = time.mktime(time.strptime(stamp.group(), '%Y-%m-%d %H:%M:%S')) >= since
            except ValueError:
                keep = False
        if keep:
            out.append(line)
    return '\n'.join(out)


def pipeline_status(home, cfg):
    home = Path(home)
    directory = home / 'data'
    state_file = directory / 'status.json'
    state = _json(state_file)
    # Use the last snapshot's mtime, not now: a stopped brain has an old uptime.
    try:
        since = max(0, int(state_file.stat().st_mtime) - max(0, int(state['uptime'])) - 5)
    except (OSError, KeyError, TypeError, ValueError):
        since = max(0, int(time.time()) - 86400)
    result = {'ok': True, 'since': since, 'database_available': False,
              'brain_paused': bool(cfg.get('bot_paused')),
              'conversations': [], 'model_ok': 0, 'model_failed': 0, 'gates': {},
              'callbacks': {}, 'recent_flags': []}
    # Only configured private contacts and groups; no contact names or message content.
    scopes = []
    for kind, values in (('private', [cfg.get('owner_qq'), *(cfg.get('allow_users') or [])]),
                         ('group', cfg.get('allow_groups') or [])):
        for value in values:
            if str(value).isdigit() and int(value) > 0:
                item = (kind, int(value))
                if item not in scopes:
                    scopes.append(item)
    database = directory / 'bot.db'
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
            connection.execute('PRAGMA query_only=ON')
            for kind, number in scopes[:30]:
                conv = ('p:' if kind == 'private' else 'g:') + str(number)
                row = {'kind': kind, 'account': number}
                for direction in ('in', 'out'):
                    count, latest = connection.execute(
                        'SELECT COUNT(*),MAX(ts) FROM messages WHERE conv=? AND direction=? AND ts>=?',
                        (conv, direction, since)).fetchone()
                    row[direction] = count
                    row['last_' + direction] = latest or 0
                for action in ('reply', 'quiet'):
                    row[action] = connection.execute(
                        "SELECT COUNT(*) FROM logs WHERE ts>=? AND tag='autonomy' AND detail LIKE ?",
                        (since, conv + ' ' + action + ' %')).fetchone()[0]
                result['conversations'].append(row)
            for ok, key in ((1, 'model_ok'), (0, 'model_failed')):
                result[key] = connection.execute('SELECT COUNT(*) FROM usage_records WHERE ts>=? AND ok=?',
                                                 (since, ok)).fetchone()[0]
            for tag in ('skip_group', 'skip_private', 'skip_group_noat', 'autonomy_format'):
                result['gates'][tag] = connection.execute('SELECT COUNT(*) FROM logs WHERE ts>=? AND tag=?',
                                                         (since, tag)).fetchone()[0]
            result['database_available'] = True
    except (sqlite3.Error, OSError):
        result['conversations'] = []
        result['recent_flags'].append('收发统计暂不可读')
    health = _json(directory / 'mobile-hook-health.json')
    if health.get('pid') == state.get('pid') and isinstance(health.get('statuses'), dict):
        result['callbacks'] = {key: value for key, value in health['statuses'].items()
                               if key in ('204', '400', '401', '413', '503')
                               and type(value) is int and value >= 0}
    uin = str(cfg.get('login_uin') or '')
    if re.fullmatch(r'[1-9][0-9]{4,11}', uin):
        path = home / 'Napcat/opt/QQ/resources/app/app_launcher/napcat/config' / ('onebot11_' + uin + '.json')
        network = _json(path).get('network') or {}
        clients = network.get('httpClients') or []
        expected = 'http://127.0.0.1:33113/'
        client = next((c for c in clients if isinstance(c, dict) and c.get('enable') and c.get('url') == expected), {})
        result['callback_configured'] = bool(client)
        token = cfg.get('onebot_token')
        result['callback_token_matches'] = bool(token and client.get('token') and
                                                hmac.compare_digest(str(token), str(client['token'])))
    else:
        result.update(callback_configured=False, callback_token_matches=False)
    text = _recent_log(directory / 'bot.log', since)
    patterns = {'消息处理异常': '[worker] EXC', '消息队列已满': '[queue] 满了',
                '模型请求超时': 'TimeoutError', '模型请求失败': '全部失败',
                '缺少运行模块': 'ModuleNotFoundError', '数据库访问异常': 'OperationalError'}
    result['recent_flags'].extend(label for label, pattern in patterns.items() if pattern in text)
    return result
