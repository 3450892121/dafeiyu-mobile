import ast
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

APP = Path(os.environ.get('BOT_TEST_PATH') or Path(__file__).resolve().parents[2] / 'runtime' / 'core')
sys.path.insert(0, str(APP))
import guard

BODY = '憨批，拿别人群里的截图来教我骂人？那是“小鱼儿”在骂，轮得到你替它给我立榜样？'
NOTE = '（自己发的图：画面：蓝发女仆面无表情，头顶气泡写着已思考13秒和哦牛逼）'


class ImageNoteGuardTests(unittest.TestCase):
    def test_reported_reply_keeps_original_tone_and_body(self):
        self.assertEqual(guard.scrub(BODY + NOTE), BODY)
        self.assertEqual(guard.check(BODY + NOTE)['action'], 'ok')

    def test_all_labels_emitted_by_history_renderer(self):
        spec = importlib.util.spec_from_file_location('test_note_store', APP / 'store.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for origin in ('in', 'out', ''):
            with self.subTest(origin=origin):
                history = module._render_attachment_notes('原句', [{'type': 'sticker', 'note': '画面：蓝发角色'}], origin)
                self.assertEqual(guard.scrub(history), '原句')

    def test_parenthesis_variants_nested_and_middle(self):
        for left, right in (('（', '）'), ('(', ')'), ('【', '】'), ('[', ']')):
            for label in ('自己发的图', '这位说话人发的图'):
                with self.subTest(left=left, label=label):
                    self.assertEqual(guard.scrub('前句。' + left + label + ':画面：角色（帽子）' + right + '后句。'), '前句。后句。')

    def test_truncation_does_not_leave_partial_annotation(self):
        for left in ('（', '(', '【', '['):
            text = BODY + left + '自己发的图：画面：蓝发角色'
            self.assertEqual(guard.scrub(text), BODY)

    def test_bare_or_malformed_label_is_blocked(self):
        for text in ('正文 自己发的图：画面：蓝发', '这位说话人发的图:蓝发', '正文 自己发的图：截断）'):
            self.assertEqual(guard.check(text)['reason'], '内部图片注记')
        self.assertIn('画面备注', guard.rewrite_hint('内部图片注记'))

    def test_normal_image_answers_and_parentheses_are_preserved(self):
        for text in ('画面：蓝发女孩，头顶有文字气泡。', '你自己发的图，我当然认得。',
                     '这张图（是昨天拍的）挺清楚。', '我自己发的图是昨天拍的。', '这张图不错  '):
            self.assertEqual(guard.strip_image_notes(text), text)
            self.assertEqual(guard.check(text)['action'], 'ok')

    def test_annotation_only_is_empty_and_not_sendable(self):
        self.assertEqual(guard.scrub(NOTE), '')
        self.assertEqual(guard.check(NOTE)['action'], 'drop')

    def test_actual_final_sender_cleans_before_onebot(self):
        tree = ast.parse((APP / 'flow_reply.py').read_text(encoding='utf-8'))
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        name = '_send_detail' if '_send_detail' in functions else '_send'
        node = functions[name]
        from blacklist import blocked, policy_changed
        namespace = {'guard': guard, 'log': lambda *a, **kw: None, 'OneBotError': RuntimeError,
                     'blocked': blocked, 'policy_changed': policy_changed}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<real final sender>', 'exec'), namespace)
        calls = []
        fake = SimpleNamespace(
            _text_segs=lambda text: [{'type': 'text', 'data': {'text': text}}],
            _take_group_at=lambda *a: False,
            onebot=SimpleNamespace(send_group=lambda gid, segs, **kw: calls.append(segs),
                                  send_private=lambda uid, segs: calls.append(segs)),
            store=SimpleNamespace(add_log=lambda *a, **kw: None),
        )
        namespace[name](fake, True, 123, 456, [{'type':'text', 'data':{'text':BODY+NOTE}}])
        self.assertTrue(calls)
        delivered = ''.join(seg.get('data', {}).get('text', '') for seg in calls[0])
        self.assertEqual(delivered, BODY)
        calls.clear()
        namespace[name](fake, False, 0, 456, [{'type':'text','data':{'text':'自己发的图：裸备注'}}])
        self.assertFalse(calls)


if __name__ == '__main__':
    unittest.main()
