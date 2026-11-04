"""Both production guards and real send methods, with fictional notes and fake transports."""
import ast
import importlib.util
import datetime
import json
import os
from pathlib import Path
import sys
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
APPS = (Path(__file__).resolve().parents[2] / 'runtime' / 'core',)
sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(APPS[0]))


def load_guard(app):
    spec = importlib.util.spec_from_file_location('recipient_note_guard', app / 'guard.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tests(unittest.TestCase):
    def each(self):
        return ((app, load_guard(app)) for app in APPS)

    def test_reported_note_is_removed_and_the_reply_stays(self):
        # Fictional QQ and source ids keep the real incident out of test fixtures.
        note = '（此回复的接话对象：浅忆(QQ123456)，对应来话#42）'
        for app, guard in self.each():
            with self.subTest(app=str(app)):
                self.assertEqual('行吧，下次记住。', guard.scrub('行吧，下次记住。 ' + note))
                self.assertEqual('', guard.scrub(note))
                self.assertEqual('drop', guard.check(note)['action'])

    def test_nested_brackets_variants_and_truncation(self):
        notes = ('（此回复的接话对象：甲(别名)(QQ123456)，对应来话#42）',
                 '(此回复的接话对象:甲(QQ123456),对应来话#42)',
                 '【此回复的接话对象：甲[别名](QQ123456)，对应来话#42】',
                 '[本条回复的接话对象：甲(QQ123456)，对应来话#42]',
                 '（这次回复的接话对象：甲(QQ123456)，对应来话#42）',
                 '（此回复的接话对象：甲(QQ123456)，对应来话#42',
                 '（此回复的接话对象：甲(QQ123')
        for app, guard in self.each():
            for note in notes:
                with self.subTest(app=str(app), note=note):
                    self.assertEqual('记住啦。', guard.scrub('记住啦。 ' + note))
                    self.assertEqual('drop', guard.check(note)['action'])
            self.assertEqual('甲。乙。', guard.scrub('甲。' + notes[0] + '乙。' + notes[1]))

    def test_unwrapped_or_malformed_note_is_blocked(self):
        for app, guard in self.each():
            for note in ('此回复的接话对象：甲(QQ123456)，对应来话#42',
                         '记住啦。（注记：此回复的接话对象：甲(QQ123456)）'):
                with self.subTest(app=str(app), note=note):
                    self.assertEqual('内部回复归属注记', guard.check(note)['reason'])

    def test_ordinary_qq_numbers_parentheses_and_discussion_survive(self):
        replies = ('你可以找甲(QQ123456)。', '我在回甲的话（你刚才也看到了）。',
                   '对应来话#42是截图里的字样。', '“此回复的接话对象”这几个字是说明。',
                   '这次接话对象：甲，先听他说。', '别忘了（真的）（下次记得）。',
                   '例子是[甲(QQ123456)]，编号是#42。')
        for app, guard in self.each():
            for text in replies:
                with self.subTest(app=str(app), text=text):
                    self.assertEqual(text, guard.scrub(text))
                    self.assertEqual('ok', guard.check(text)['action'])

    def test_actual_tts_cleaner_removes_note_but_keeps_ordinary_qq_parentheses(self):
        import re
        tree=ast.parse((APPS[0]/'voice.py').read_text(encoding='utf-8'))
        selected=[n for n in tree.body if
            (isinstance(n,ast.FunctionDef) and n.name=='clean_for_tts') or
            (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_DROP_RES' for t in n.targets))]
        ns=dict(re=re,qq_faces=SimpleNamespace(strip=lambda text:text),strip_choice=lambda text:text)
        exec(compile(ast.Module(body=selected,type_ignores=[]),'<actual-note-tts>','exec'),ns)
        with patch.dict(sys.modules,{'guard':load_guard(APPS[0])}):
            self.assertEqual('记住啦。',ns['clean_for_tts']('记住啦。（此回复的接话对象：甲(QQ123456)，对应来话#42）'))
            self.assertEqual('记住啦（QQ123456是你提到的号码）。',
                ns['clean_for_tts']('记住啦（QQ123456是你提到的号码）。'))

    def test_companion_gets_attribution_as_system_data_and_keeps_assistant_prose_clean(self):
        # Run the production method to the fake quiet decision: no send, timers or model API.
        import flow_companion
        history=[dict(role='assistant',content='你的相机修好再带来',source_id=2,sender_id=99,
            response_to=dict(source_id=1,who_qq=10,who_name='甲'))]
        current=dict(direction='in',sender_id=20,sender_name='乙',text='我也有相机')
        pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True,text='{"act":"quiet"}')))
        bot=SimpleNamespace(self_id=99,cfg={'self_growth_enabled':False},pool=pool,
            store=SimpleNamespace(messages_for_context=lambda *a,**k:history,
                thread_rows=lambda *a:[current],get_setting=lambda *a:'',add_log=Mock(),
                get_person_relation_effective=lambda *a:dict(favor=50,trust=30)),
            persona=SimpleNamespace(system_prompt=lambda *a,**k:'虚构人设'),
            _companion_enabled=lambda *a:True,_is_group_speak_quiet=lambda:False,
            _group_speak_last_from_bot=lambda *a:False,_proactive_now_line=lambda:'测试时间',
            _companion_state=SimpleNamespace(snapshot=lambda *a:(3,True,True),eligible=lambda *a:True),
            _json_from_text=json.loads, _tool_defs=lambda *a,**k:[])
        with patch.object(flow_companion.reply_autonomy,'resting',return_value=False), \
             patch.dict(sys.modules,{'recent_topic':SimpleNamespace(read=lambda *a:'',schedule=lambda *a:None)}):
            flow_companion.CompanionMixin._companion_run.__wrapped__(bot,7,3)
        pool.chat.assert_called_once()
        system, sent_history, _=pool.chat.call_args.args
        self.assertIn('"speaker": 99',system)
        self.assertIn('"who_qq": 10',system)
        self.assertEqual('你的相机修好再带来',sent_history[0]['content'])

    def sender(self, app, guard):
        tree = ast.parse((app / 'flow_reply.py').read_text(encoding='utf-8'))
        methods = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        selected = [methods['_send']]
        if '_send_detail' in methods:
            selected.append(methods['_send_detail'])
        namespace = dict(guard=guard, log=Mock(), blocked=lambda *a: False,
                         policy_changed=lambda *a: False, note_delivery=Mock(),
                         is_connection_refused=lambda e: False,
                         at_seg=lambda qq: dict(type='at', data=dict(qq=str(qq))))
        exec(compile(ast.Module(body=selected, type_ignores=[]), '<actual-note-send>', 'exec'), namespace)
        send = Mock(return_value={'message_id': 77})
        bot = SimpleNamespace(cfg={}, store=SimpleNamespace(add_log=Mock()),
                              onebot=SimpleNamespace(send_private=send, send_group=send),
                              _take_group_at=lambda *a: None,
                              _text_segs=lambda text: [dict(type='text', data=dict(text=text))])
        for node in selected:
            setattr(bot, node.name, MethodType(namespace[node.name], bot))
        return bot, send

    def test_real_send_gate_cleans_group_and_private_text_and_drops_note_only(self):
        note = '（此回复的接话对象：甲(QQ123456)，对应来话#42）'
        for app, guard in self.each():
            for is_group in (False, True):
                for raw, expected in (('记住啦。' + note, '记住啦。'), (note, None),
                                      ('此回复的接话对象：甲(QQ123456)，对应来话#42', None)):
                    with self.subTest(app=str(app), group=is_group, text=raw):
                        bot, send = self.sender(app, guard)
                        self.assertEqual(expected is not None, bot._send(
                            is_group, 7, 10, [dict(type='text', data=dict(text=raw))]))
                        if expected is None:
                            send.assert_not_called()
                        else:
                            send.assert_called_once_with(7 if is_group else 10,
                                [dict(type='text', data=dict(text=expected))])


if __name__ == '__main__':
    unittest.main()
