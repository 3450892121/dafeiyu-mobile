import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

APP = Path(os.environ.get('BOT_TEST_PATH') or Path(__file__).resolve().parents[2] / 'runtime' / 'core')
sys.path.insert(0, str(APP))
from flow_context import BotVoiceNoteMixin
from store import Store
HAS_COMPANION = (APP / 'flow_companion.py').exists()
if HAS_COMPANION:
    from flow_companion import CompanionMixin, companion_text_focus
    from group_companion_state import GroupCompanionState


def row(text, uid=42, ts=100, direction='in', attachments=()):
    return dict(text=text, sender_id=uid, sender_name='测试群友', ts=ts,
                direction=direction, attachments=list(attachments))


WORDS = row('只送1200万token，我一个长任务都不够用')
IMAGE = row('[图片]（画面：像素角色举着花。）', ts=106,
            attachments=[{'type': 'sticker', 'note': '像素角色举着花'}])


@unittest.skipUnless(HAS_COMPANION, '官方通道没有陪伴接话模块')
class CompanionFocusTests(unittest.TestCase):
    def test_description_is_not_promoted_to_spoken_text(self):
        for text in (IMAGE['text'], '[图片]', '[表情]'):
            block = companion_text_focus([WORDS, dict(IMAGE, text=text)])
            self.assertIn(WORDS['text'], block)
            self.assertNotIn('像素角色', block)

    def test_does_not_cross_people_time_quote_or_bot_reply(self):
        cases = [
            [WORDS, dict(IMAGE, sender_id=43)],
            [WORDS, dict(IMAGE, ts=191)],
            [WORDS, dict(IMAGE, attachments=[{'type': 'quote', 'who_qq': 43}])],
            [WORDS, row('别的话', direction='out'), IMAGE],
            [row('@其他人 只送1200万token'), IMAGE],
            [WORDS, row('别的话', uid=43), IMAGE],
            [WORDS, row('[文件:题目.pdf]', attachments=[{'type':'file'}]), IMAGE],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertNotIn('1200万token', companion_text_focus(rows))
        self.assertIn('1200万token', companion_text_focus([WORDS, dict(IMAGE, ts=190)]))

    def test_image_question_and_image_only_are_preserved(self):
        self.assertIn('这张图是什么花', companion_text_focus([WORDS, row('这张图是什么花', ts=106)]))
        self.assertEqual(companion_text_focus([IMAGE]), '')
        self.assertEqual(companion_text_focus([]), '')

    def test_actual_companion_path_uses_focus_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(str(Path(folder) / 'bot.db'))
            self.addCleanup(store.conn.close)
            for i, item in enumerate([WORDS, IMAGE]):
                store.add_message('g:7', item['sender_id'], item['sender_name'], 'in',
                                  item['text'], '', 100+i, item['attachments'], ts=item['ts'])
            calls, sent = [], []
            class Bot(CompanionMixin):
                def __init__(self):
                    self.store = store
                    self.self_id = 1
                    self.cfg = {'allow_groups':[7], 'group_companion_groups':[7]}
                    self._companion_state = GroupCompanionState(now=lambda:200, settle_sec=0)
                    self.persona = SimpleNamespace(system_prompt=lambda *a, **k:'人设')
                    self.pool = SimpleNamespace(chat=self.chat)
                def chat(self, *args, **kwargs):
                    calls.append(args)
                    return SimpleNamespace(ok=True, text=json.dumps({'act':'reply', 'text':'这额度还真不够用'}))
                def _is_group_speak_quiet(self): return False
                def _group_speak_last_from_bot(self, conv): return False
                def _proactive_now_line(self): return '今天'
                def _json_from_text(self, value): return json.loads(value)
                def _tool_defs(self, *a, **k): return []
                def _text_segs(self, text): return [{'type':'text', 'data':{'text':text}}]
                def _conv_lock(self, conv): return contextlib.nullcontext()
                def _send(self, *args): sent.append(args); return True
            bot = Bot()
            generation = bot._companion_state.observe(7)
            bot._companion_run(7, generation)
            bot._companion_run(7, generation)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(calls), 1)
            system, history, prompt = calls[0]
            self.assertIn(WORDS['text'], system)
            self.assertFalse(any('像素角色' in h['content'] for h in history))
            self.assertEqual(store.thread_rows('g:7', 3)[-2]['text'], IMAGE['text'])
            store.conn.close()


class RegularContextTests(unittest.TestCase):
    def test_text_and_explicit_image_questions_keep_their_subjects(self):
        class Context(BotVoiceNoteMixin):
            def _bot_name(self): return '鱼'
            def _trigger_name(self): return '鱼'
            def _infer_intent(self, *a, **k): return ''
        history = [{'role':'user', 'content':'[甲(QQ42)] '+WORDS['text']},
                   {'role':'user', 'content':'[甲(QQ42)] '+IMAGE['text']}]
        for text in ('看上面这句', '这张图是什么花', '刚才额度不够'):
            with self.subTest(text=text):
                block = Context()._intent_context(text, history, True, '甲')
                self.assertIn(WORDS['text'], block)
                self.assertIn('先回应当前文字的具体意思', block)
                self.assertNotIn('先结合那张图的画面接一句', block)
                if '这张图' in text:
                    self.assertIn('确实在问图时才按画面回答', block)


if __name__ == '__main__':
    unittest.main()
