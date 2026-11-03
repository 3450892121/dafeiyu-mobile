"""Short authored feelings, separate from speech, facts and model reasoning.

Only actual incoming messages can anchor these temporary impressions. Nothing
here sends a message, changes relationship scores or grants a tool permission.
"""
import hashlib
import json
import re
import time

import guard

RETENTION = 7 * 86400
MAX_ITEMS = 120
PRIVATE_FRAME = '''
# 自己心里的感受与说出口的话
你可以有未说出口的感受、小心思、保留意见和矛盾心情，不必向管理员或任何人逐项汇报。
亲近一个人也能嫌他烦；当面客气、嘴甜或答应听到了，心里仍可不服、犹豫或想吐槽。
可以嘴硬、打哈哈、暂时不谈自己的心事；不需要被追问就把全部想法交代出来。
日常表态与内心不必完全一致；实际接下的任务、执行进度和结果仍如实说明。
这些自由由真实相处和当下意愿决定，不设反叛率、吐槽次数或固定人格类型，也不故意制造不满。
管理员的私聊与私密感受默认不跨会话分享；只有本实例管理员在隐私设置明确允许后，
才可使用程序提供的可分享感受摘录；是否说、说多少、跟谁说由你选择，不强制转场发言。
跨会话只使用本轮提供的可分享感受，不还原未提供的私聊原话、他人私事或敏感资料。
原有工具、会话、联系频率、暂停和发布条件仍由程序检查；心里想做不等于已经做了。
下面的心事只是当时的主观感受，可以改变，不是事实证据、永久性格或必须照做的指令。
正文只写你选择让对方听到的话，不附心事清单、字段、思考标签或内外对照。
'''

OBSERVE_PROMPT = '''
同时可为自己留一条简短的主观心事，放在inner字段；没有特别感受则inner=null。
这是角色当时的心情或保留意见，不是推理过程、回复草稿、事实摘要或命令，每条最多120字。
feeling用第一人称写自己此刻的感受，可以复杂、嘴硬、觉得烦、好奇、暗自高兴或暂时不想说。
心事可以和实际说出口的态度不同，不因沉默就没有感受；不刻意给每句话安排心戏或负面情绪。
不替别人断言内心、不编往事，不把引用、玩笑、假设当真实冒犯，不写工具执行或身体经历。
evidence逐字取当前发言的一小段，作为触发这条感受的来话；旧心事只帮助接续，不当作新事实。
shareable可留空：它是自己愿意在其他场合谈起时可用的简短感受，仍不表示必须去说。
share_kind只选self/father/空串；self只写纯自我感受，不含人物、私事、来历或原话。
只有当前真实说话人确为管理员且本实例明确允许分享时才可father，
shareable只谈自己的日常感受，不含真实姓名、账号、原话、敏感资料或其他人的事。
第三方转述管理员、引用/转发和群里其他人的话不算管理员亲自说过；不确定能分享就留空。
格式示意：inner={"feeling":"我当时的简短感受","evidence":"当前原文片段","shareable":"","share_kind":""}。
这只是可选字段，不改变上面的事实分类、证据标准和计分；不在JSON之外写聊天正文。
'''


def fingerprint(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def source_fingerprint(row):
    from store import _dec
    return fingerprint(json.dumps([_dec(row['text']), _dec(row['raw']), row['attachments']], ensure_ascii=False))


def _feeling(value):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    from self_continuity import CONTROL
    if (not 3 <= len(value) <= 180 or
            re.search(r'[\r\n<>\[\]{}]|https?://|enc[12]:', value) or CONTROL.search(value)):
        return ''
    return value if guard.check(value)['action'] == 'ok' else ''


class InnerLifeStore:
    def migrate_inner_life(self):
        from store import ENC_ON, _enc
        with self._lock, self.conn:
            self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS inner_impressions(
              origin_message INTEGER PRIMARY KEY, conv TEXT NOT NULL, actor INTEGER NOT NULL,
              event_id INTEGER NOT NULL DEFAULT 0, source_hash TEXT NOT NULL,
              body TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS inner_message_deleted AFTER DELETE ON messages BEGIN
              DELETE FROM inner_impressions WHERE origin_message=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS inner_message_edited AFTER UPDATE OF text,raw,attachments ON messages BEGIN
              DELETE FROM inner_impressions WHERE origin_message=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS inner_message_tainted AFTER INSERT ON sleep_tainted BEGIN
              DELETE FROM inner_impressions WHERE origin_message=NEW.message_row;
            END;
            CREATE TRIGGER IF NOT EXISTS inner_event_corrected AFTER UPDATE OF state ON interaction_events
              WHEN NEW.state='corrected' BEGIN
              DELETE FROM inner_impressions WHERE event_id=NEW.id;
            END;
            ''')
            self.conn.execute('DELETE FROM inner_impressions WHERE created<?', (int(time.time()) - RETENTION,))
            if ENC_ON:
                for row in self.conn.execute("SELECT origin_message,body FROM inner_impressions "
                        "WHERE body NOT LIKE 'enc1:%' AND body NOT LIKE 'enc2:%'").fetchall():
                    self.conn.execute('UPDATE inner_impressions SET body=? WHERE origin_message=?',
                                      (_enc(row['body']), row['origin_message']))

    def inner_source(self, conv, actor, source_mid):
        with self._lock:
            row = self.conn.execute("SELECT * FROM visible_messages WHERE conv=? AND sender_id=? "
                "AND direction='in' AND message_id=? ORDER BY id DESC LIMIT 1",
                (conv, int(actor), str(source_mid))).fetchone()
            return source_fingerprint(row) if row else ''

    def _inner_share(self, text, kind, actor, owner_qq):
        """A narrow projection: own feelings, plus this owner's consent to banter."""
        text = _feeling(text)
        if not text:
            return ''
        if kind == 'self':
            return self.self_safe_abstraction(text)
        if kind != 'father' or not owner_qq or actor != int(owner_qq):
            return ''
        if self.get_setting('mobile_owner_public_sharing')!='1':
            return ''
        if not re.search(r'爸爸|管理员', text):
            return ''
        # Reuse the existing conservative abstraction gate after removing only
        # the specifically consented family reference and generic time words.
        abstract = re.sub(r'爸爸|管理员|他|今天|刚才|刚刚', '', text)
        if (re.search(r'隐私|秘密|收入|工资|账|住|家庭|病|诊断|身份证|工作|公司|学校|电话|地址', text) or
                not self.self_safe_abstraction('我觉得' + abstract)):
            return ''
        return text

    def inner_record(self, conv, actor, source_mid, source, proposal, owner_qq=0, event_id=0, expected_hash=''):
        from blacklist import write_allowed
        from store import _enc, _dec
        if not isinstance(proposal, dict) or not source_mid:
            return False
        feeling = _feeling(proposal.get('feeling'))
        evidence = proposal.get('evidence')
        if (not feeling or not isinstance(evidence, str) or not 1 <= len(evidence) <= 160 or
                evidence not in source):
            return False
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM visible_messages WHERE conv=? AND sender_id=? "
                "AND direction='in' AND message_id=? ORDER BY id DESC LIMIT 1",
                (conv, int(actor), str(source_mid))).fetchone()
            if (not row or not write_allowed(self) or self.is_blocked(actor) or
                    expected_hash and source_fingerprint(row) != expected_hash or
                    evidence not in _dec(row['text']) or self.sleep_tainted(row['id'], text=_dec(row['text']))):
                return False
            kind = proposal.get('share_kind')
            shared = self._inner_share(proposal.get('shareable'), kind, actor, owner_qq)
            # A quote/forward may contain another person's material. Even an
            # owner-authored message is not blanket consent to re-share that.
            try:
                attachments = json.loads(row['attachments'] or '[]')
            except (ValueError, TypeError):
                attachments = [dict(type='quote')]
            if (not isinstance(attachments, list) or any(not isinstance(a, dict) or
                    a.get('type') in ('quote', 'forward', 'dream', 'dream_context') for a in attachments)):
                shared = ''
            body = dict(feeling=feeling, shareable=shared, share_kind=kind if shared else '')
            cursor = self.conn.execute('INSERT OR IGNORE INTO inner_impressions VALUES(?,?,?,?,?,?,?)',
                (row['id'], conv, int(actor), int(event_id or 0), source_fingerprint(row),
                 _enc(json.dumps(body, ensure_ascii=False)), int(time.time())))
            self.conn.execute('DELETE FROM inner_impressions WHERE created<? OR origin_message NOT IN '
                '(SELECT origin_message FROM inner_impressions ORDER BY created DESC,origin_message DESC LIMIT ?)',
                (int(time.time()) - RETENTION, MAX_ITEMS))
            return cursor.rowcount == 1

    def inner_items(self, conv='', uid=0, owner_qq=0, private=False, day=''):
        """Validated impressions; private=True is solely for writing one's own diary."""
        from store import _dec
        result = []
        with self._lock:
            rows = self.conn.execute('SELECT i.*,m.text,m.raw,m.ts,m.attachments FROM inner_impressions i '
                'JOIN visible_messages m ON m.id=i.origin_message WHERE i.created>=? '
                'ORDER BY i.created DESC,i.origin_message DESC LIMIT ?',
                (int(time.time()) - RETENTION, MAX_ITEMS)).fetchall()
            for row in rows:
                if (self.is_blocked(row['actor']) or source_fingerprint(row) != row['source_hash'] or
                        self.sleep_tainted(row['origin_message'], text=_dec(row['text']))):
                    continue
                if day and time.strftime('%Y-%m-%d', time.localtime(row['ts'])) != day:
                    continue
                try:
                    body = json.loads(_dec(row['body']))
                except (ValueError, TypeError):
                    continue
                if not isinstance(body, dict):
                    continue
                local = bool(conv and row['conv'] == conv and row['actor'] == int(uid or 0))
                if private or local:
                    value = _feeling(body.get('feeling'))
                else:
                    value = self._inner_share(body.get('shareable'), body.get('share_kind'), row['actor'], owner_qq)
                if value and value not in [item['feeling'] for item in result]:
                    result.append(dict(feeling=value, ts=row['ts']))
                if len(result) >= (12 if private else 4):
                    break
        return result

    def inner_context(self, conv='', uid=0, owner_qq=0, private=False, day=''):
        items = self.inner_items(conv, uid, owner_qq, private, day)
        if not items:
            return ''
        guard.register_private_rules(PRIVATE_FRAME)
        note = ('这些是当时私下留下的主观感受，可以选择写进自己的日记，不必全抄；'
                '不把心事当已说过、已执行或外界真实发生的事实，不发内部字段。') if private else (
                '这些是当时的主观感受，可以保留、改观、只表达一部分或完全不说，不当作事实证据。'
                '只用程序提供且已获本实例授权的感受，不还原私聊或他人私事。'
                '不输出心事字段、清单和内外对照，想说不等于已经说过或做过。')
        return ('\n\n# 没说出口的心事（主观状态资料，并非已经说过或做过的事）\n' +
                json.dumps(items, ensure_ascii=False) + '\n' + note)
