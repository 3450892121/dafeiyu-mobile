import datetime as dt
import ast
import base64
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode

sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from dreaming import DreamRunner, background_slot, bot_key, in_window, local_day, validate_moments


class DreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, 'bot.db'))
        self.now = dt.datetime(2026, 11, 2, 4, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        self.bot = SimpleNamespace(store=self.store, cfg={'bot_name': '测试鲸'}, q=queue.Queue(),
                                   persona=SimpleNamespace(identity='测试鲸，爱吐槽，语气轻快', reload_if_changed=lambda: None))
        self.calls = []
        self.lock = os.path.join(self.tmp.name, 'shared', 'dream.lock')
        self.runner = DreamRunner(self.bot, self.lock, self.generate)
        self.runner.last_activity = self.now - 3600
        self.clock = patch('dreaming.time.time', return_value=self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.store.conn.close()
        self.tmp.cleanup()

    def message(self, text, conv='p:1', direction='in', attachments=None, ts=None):
        return self.store.add_message(conv, 1, '小甲', direction, text,
                                      attachments=attachments, ts=ts or self.now - 2000)

    def seed(self, conv='p:1'):
        return [self.message('这次考试总算通过了，约好的饭下周再吃', conv),
                self.message('食堂今天有小鱼形状的饭团，看起来挺好笑的', conv)]

    def generate(self, system, prompt, temperature):
        self.calls.append((system, prompt, temperature))
        if temperature < 0.5:
            data = json.loads(prompt.split('\n', 1)[1])
            rows = [r for r in data['sources'] if r['id'] in data['dream_source_ids']]
            return json.dumps({'moments': [{'source_id': r['id'], 'quote': r['text'], 'theme': '日常'} for r in rows], 'consolidations':[]}, ensure_ascii=False), 'fake'
        return '我梦见一群小鱼叼着试卷排队，食堂的窗口开在云上。轮到我的时候，阿姨递过来一个会唱歌的饭团，还非要我先跟它对唱。我刚开口，它就变成麦克风游走了，我踩着泡泡追了半天，最后只抓到一张写着下周见的饭票。', 'fake'

    def tick(self):
        return self.runner.tick(self.now)

    def test_preview_has_scoped_sources_and_no_real_state_changes(self):
        ids = self.seed()
        self.store.add_person_memory(1, '小甲下周约好吃饭', 'commitment', 5)
        tables = ('messages', 'person_memories', 'group_memories', 'person_relations', 'interaction_events', 'diaries', 'reminders')
        before = {t: [tuple(r) for r in self.store.conn.execute('SELECT * FROM '+t)] for t in tables}
        self.assertEqual('complete', self.tick())
        after = {t: [tuple(r) for r in self.store.conn.execute('SELECT * FROM '+t)] for t in tables}
        self.assertEqual(before, after)
        state = self.store.dream_state(self.now)
        self.assertEqual('learning', state['stage'])
        self.assertEqual('p:1', state['runs'][0]['scope'])
        self.assertEqual(ids, [s['id'] for s in state['runs'][0]['sources']])
        row = self.store.conn.execute('SELECT source_type,visibility FROM dream_entries').fetchone()
        self.assertEqual(('dream', 'scoped'), tuple(row))
        self.assertEqual(2, len(self.calls))

    def test_one_attempt_survives_restart_and_delete(self):
        self.seed(); self.assertEqual('complete', self.tick())
        self.store.delete_dream(1)
        restarted = DreamRunner(self.bot, self.lock, self.generate)
        restarted.last_activity = self.now-3600
        self.assertEqual('done_today', restarted.tick(self.now))
        self.assertEqual(2, len(self.calls))
        self.assertEqual([], self.store.dream_state(self.now)['runs'][0]['sources'])

    def test_no_material_or_single_message_uses_no_model(self):
        self.assertEqual('no_material', self.tick())
        self.message('只有一条真实消息不能凑出一场梦')
        self.assertEqual('no_material', self.tick())
        self.assertEqual([], self.calls)

    def test_scopes_are_not_combined(self):
        self.seed('p:1'); self.seed('g:2')
        self.message('另一个私聊中的重要秘密绝不应该出现在素材里', 'p:3')
        self.assertEqual('complete', self.tick())
        sources = self.store.dream_state(self.now)['runs'][0]['sources']
        self.assertEqual(1, len({s['conv'] for s in sources}))
        self.assertNotIn('秘密', self.calls[0][1])

    def test_outgoing_diary_quote_and_dream_are_not_evidence(self):
        self.message('我梦见主人给我一整座城堡')
        self.message('系统日记：今天所有人都很开心', '__day__')
        self.message('机器人自己的梦境绝不能再次当证据', direction='out')
        self.message('转述来的话不能当当前发言人的经历', attachments=[{'type':'reply','id':'x'}])
        self.message('引用来自别人的事情不作为素材', attachments=[{'type':'quote'}])
        self.assertEqual('no_material', self.tick())

    def test_duplicate_messages_do_not_make_two_sources(self):
        for _ in range(8): self.message('同一条话重复八遍也不是八个独立事件')
        self.assertEqual('no_material', self.tick())

    def test_queue_recent_activity_pause_disable_and_window(self):
        self.seed()
        self.bot.q.put('new')
        self.assertEqual('waiting', self.tick()); self.bot.q.get(); self.bot.q.task_done()
        self.runner.touch(); self.assertEqual('waiting', self.tick())
        self.runner.last_activity = self.now-3600
        self.bot.cfg['bot_paused'] = True; self.assertEqual('waiting', self.tick())
        self.bot.cfg['bot_paused'] = False
        self.store.save_dream_settings({'enabled':'0','start':'03:00','end':'06:00','idle_minutes':25})
        self.assertEqual('waiting', self.tick()); self.assertEqual([], self.calls)

    def test_shared_os_lock_blocks_other_runner(self):
        self.seed()
        with background_slot(self.lock) as acquired:
            self.assertTrue(acquired)
            self.assertEqual('busy', self.tick())
        self.assertEqual('complete', self.tick())

    def test_new_input_during_review_aborts_second_call(self):
        self.seed()
        def generate(*args):
            result = self.generate(*args); self.runner.touch(); return result
        self.runner.generate = generate
        self.assertEqual('interrupted', self.tick())
        self.assertEqual(1, len(self.calls)); self.assertEqual(0, self.store.conn.execute('SELECT count(*) FROM dream_entries').fetchone()[0])

    def test_disabling_during_generation_discards_result(self):
        self.seed()
        def generate(*args):
            result = self.generate(*args)
            if args[2] > 0.5:
                self.store.save_dream_settings({'enabled':'0','start':'03:00','end':'06:00','idle_minutes':25})
            return result
        self.runner.generate = generate
        self.assertEqual('interrupted', self.tick())
        self.assertEqual(0, self.store.conn.execute('SELECT count(*) FROM reflection_candidates').fetchone()[0])

    def test_source_edit_during_generation_discards_result(self):
        ids = self.seed()
        def generate(*args):
            result = self.generate(*args)
            self.store.conn.execute('UPDATE messages SET text=? WHERE id=?', ('纠正后的新原文不能被旧梦覆盖', ids[0])); self.store.conn.commit()
            return result
        self.runner.generate = generate
        self.assertEqual('interrupted', self.tick())

    def test_source_delete_after_generation_clears_all_copies(self):
        ids = self.seed(); self.assertEqual('complete', self.tick())
        self.store.conn.execute('DELETE FROM messages WHERE id=?', (ids[0],)); self.store.conn.commit()
        run = self.store.dream_state(self.now)['runs'][0]
        self.assertEqual('invalidated', run['status']); self.assertEqual('', run['text']); self.assertEqual([], run['sources'])
        self.assertEqual(0, self.store.conn.execute('SELECT count(*) FROM reflection_candidates').fetchone()[0])

    def test_failed_or_unsourced_model_output_is_not_retried(self):
        self.seed()
        self.runner.generate = lambda *a: ('{"moments":[{"source_id":999,"quote":"凭空编出的事件","theme":"日常"}]}','fake')
        self.assertEqual('failed', self.tick()); self.assertEqual('done_today', self.tick())
        self.assertEqual(0, self.store.conn.execute('SELECT count(*) FROM dream_entries').fetchone()[0])

    def test_model_exception_is_redacted(self):
        self.seed()
        def fail(*args): raise RuntimeError('SECRET api_key fake_secret')
        self.runner.generate = fail
        self.assertEqual('failed', self.tick())
        state = self.store.dream_state(self.now)
        self.assertNotIn('fake_secret', json.dumps(state)); self.assertEqual('failed', state['runs'][0]['reason'])

    def test_bad_dream_output_is_rejected(self):
        self.seed()
        def generate(*args): return self.generate(*args) if args[2]<0.5 else ('<think>泄漏思考</think>'*15, 'fake')
        self.runner.generate = generate
        self.assertEqual('failed', self.tick())

    def test_repeated_night_does_not_relearn_processed_material(self):
        self.seed(); self.assertEqual('complete', self.tick())
        self.store.conn.execute("UPDATE dream_runs SET day='2026-11-01'"); self.store.conn.commit()
        self.assertEqual('no_material', self.tick()); self.assertEqual(2, len(self.calls))

    def test_other_database_is_isolated(self):
        self.seed(); self.tick()
        other = Store(os.path.join(self.tmp.name, 'other', 'bot.db'))
        try:
            self.assertNotEqual(bot_key(self.store), bot_key(other))
            self.assertEqual([], other.dream_state(self.now)['runs'])
        finally: other.conn.close()

    def test_stale_claim_and_retention(self):
        self.seed(); self.tick()
        self.store.conn.execute("UPDATE dream_runs SET status='review',started=?", (self.now-1000,)); self.store.conn.commit()
        self.assertEqual('interrupted', self.store.dream_state(self.now)['runs'][0]['status'])
        self.assertEqual([], self.store.dream_state(self.now+31*86400)['runs'])

    def test_settings_validation_is_atomic_and_does_not_touch_config(self):
        before = self.store.dream_settings()
        for values in ({'enabled':'x','start':'03:00','end':'06:00'},
                       {'enabled':'1','start':'25:00','end':'06:00'},
                       {'enabled':'1','start':'03:00','end':'03:00'},
                       {'enabled':'1','start':'03:00','end':'06:00','idle_minutes':1}):
            with self.assertRaises((ValueError,TypeError)): self.store.save_dream_settings(values)
            self.assertEqual(before,self.store.dream_settings())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name,'config.json')))

    def test_timezone_and_cross_midnight(self):
        self.assertEqual('2026-11-02', local_day(self.now))
        self.assertTrue(in_window(self.now, {'start':'03:00','end':'06:00'}))
        self.assertTrue(in_window(self.now, {'start':'23:00','end':'05:00'}))
        self.assertFalse(in_window(self.now, {'start':'06:00','end':'10:00'}))
        self.assertEqual('2026-11-01',local_day(self.now-5*3600))

    def test_review_accepts_only_verbatim_evidence(self):
        sources=[{'id':1,'text':'小甲考试通过，周五去吃饭'}]
        for quote in ('小甲考试没通过', '小甲'):
            with self.assertRaises(ValueError):
                validate_moments(json.dumps({'moments':[{'source_id':1,'quote':quote,'theme':'进展'}]}),sources)

    def test_sensitive_outputs_use_existing_encryption_hook(self):
        self.seed()
        enc = lambda s: 'test-enc:' + base64.b64encode(s.encode()).decode()
        dec = lambda s: base64.b64decode(s[9:]).decode() if s.startswith('test-enc:') else s
        with patch('store._enc', side_effect=enc), patch('store._dec', side_effect=dec):
            self.assertEqual('complete', self.tick())
            self.assertIn('小鱼',self.store.dream_state(self.now)['runs'][0]['text'])
            for table,column in (('dream_runs','sources'),('dream_entries','text'),('reflection_candidates','quote')):
                value=self.store.conn.execute('SELECT '+column+' FROM '+table).fetchone()[0]
                self.assertTrue(value.startswith('test-enc:'))

    def test_real_console_routes_require_auth_and_origin(self):
        default=Path(__file__).resolve().parents[1]/'console.py'
        source=Path(os.environ.get('CONSOLE_TEST_PATH') or default).read_text(encoding='utf-8')
        tree=ast.parse(source)
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        handler=next(n for n in main.body if isinstance(n,ast.ClassDef) and n.name=='H')
        supervisor=SimpleNamespace(cfg={'admin_token':'test-only-token'},store=self.store,port=8090)
        namespace=dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler,sup=supervisor,json=json,hmac=hmac,
                       parse_qs=parse_qs,urlsplit=urlsplit,OFFICIAL=False,os=os,DATA_DIR=self.tmp.name)
        exec(compile(ast.Module(body=[handler],type_ignores=[]),'<console-http-test>','exec'),namespace)
        namespace['H'].log_message=lambda *args:None
        server=ThreadingHTTPServer(('127.0.0.1',0),namespace['H'])
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,form=None,auth=False,origin=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            headers={'Content-Type':'application/x-www-form-urlencoded'}
            if auth:headers['X-Admin-Token']='test-only-token'
            if origin:headers['Origin']=origin
            conn.request(method,path,urlencode(form) if form else None,headers)
            response=conn.getresponse();body=response.read();status=response.status;conn.close()
            return status,body
        try:
            self.assertEqual(403,request('GET','/api/dreams')[0])
            settings={'enabled':'0','start':'03:00','end':'06:00','idle_minutes':'25'}
            self.assertEqual(403,request('POST','/api/dreams/settings',settings)[0])
            self.assertEqual(403,request('POST','/api/dreams/settings',settings,True,'https://foreign.test')[0])
            self.assertEqual(200,request('POST','/api/dreams/settings',settings,True)[0])
            status,body=request('GET','/api/dreams',auth=True)
            self.assertEqual(200,status);self.assertFalse(json.loads(body)['settings']['enabled'])
            self.assertEqual(403,request('POST','/api/dreams/delete',{'id':'1'})[0])
            self.assertEqual(400,request('POST','/api/dreams/delete',{'id':'invalid'},True)[0])
        finally:
            server.shutdown();server.server_close();thread.join(5)


if __name__ == '__main__': unittest.main()
