"""Actual image delivery and reply-tail methods with temporary files and fake transport."""
import ast
import os
from pathlib import Path
import re
import tempfile
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from test_qq_face_shorthand import APPS, load, methods


class StickerDeliveryEdgesTests(unittest.TestCase):
    def bot(self, app, path, enabled=True):
        funcs = methods(app / 'flow_stickers.py')
        faces = load(app / 'qq_faces.py')
        transport = load(app / 'onebot.py')
        namespace = {'os': os, 'time': time, 'qq_faces': faces, 're': re, 'log': Mock(),
                     'image_seg': transport.image_seg}
        names = ('send_sticker', 'send_sticker_item', 'take_sticker_marker', 'mood_sticker_keyword')
        tree = ast.parse((app / 'flow_stickers.py').read_text(encoding='utf-8-sig'))
        constants = [n for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id in {'_FACE_STICKER_KW', '_EMO_STICKER_RULES'} for t in n.targets)]
        exec(compile(ast.Module(body=constants + [funcs[n] for n in names], type_ignores=[]),
                     '<actual-sticker-delivery>', 'exec'), namespace)
        item = {'file': 'fixture.gif', 'desc': '假图'}
        bot = SimpleNamespace(cfg={'sticker_judge_enabled': enabled}, stickers=SimpleNamespace(path=Mock(return_value=str(path))),
                              _select_sticker=Mock(return_value=item), _send=Mock(return_value=True),
                              store=SimpleNamespace(add_log=Mock()), last_sticker={}, last_sticker_at={}, recent_stickers={})
        for name in ('send_sticker', 'send_sticker_item'):
            setattr(bot, name, MethodType(namespace[name], bot))
        return bot, item, namespace

    def invoke(self, bot, item, direct):
        return bot.send_sticker_item('p:1', False, 0, 1, item) if direct else bot.send_sticker('p:1', False, 0, 1, kw='开心')

    def test_missing_files_never_reach_transport_or_claim_delivery(self):
        for app in APPS:
            for direct in (False, True):
                with tempfile.TemporaryDirectory(dir=os.getcwd()) as temp:
                    bot, item, _ = self.bot(app, Path(temp) / 'missing.gif')
                    self.assertFalse(self.invoke(bot, item, direct))
                    bot._send.assert_not_called()
                    self.assertEqual({}, bot.last_sticker)
                    bot.store.add_log.assert_called_once()

    def test_failed_and_unknown_sends_do_not_update_usage_or_retry(self):
        for app in APPS:
            for direct in (False, True):
                for error in (False, True):
                    with tempfile.TemporaryDirectory(dir=os.getcwd()) as temp:
                        path = Path(temp) / 'fixture.gif'
                        path.write_bytes(b'fixture-only')
                        bot, item, _ = self.bot(app, path)
                        bot._send.return_value = False
                        if error:
                            bot._send.side_effect = TimeoutError('uncertain fixture delivery')
                        self.assertFalse(self.invoke(bot, item, direct))
                        bot._send.assert_called_once()
                        self.assertEqual({}, bot.last_sticker)
                        self.assertEqual({}, bot.last_sticker_at)

    def test_success_updates_usage_once_after_confirmed_transport(self):
        for app in APPS:
            for direct in (False, True):
                with tempfile.TemporaryDirectory(dir=os.getcwd()) as temp:
                    path = Path(temp) / 'fixture.gif'
                    path.write_bytes(b'fixture-only')
                    bot, item, _ = self.bot(app, path)
                    self.assertTrue(self.invoke(bot, item, direct))
                    bot._send.assert_called_once()
                    self.assertEqual({'p:1': 'fixture.gif'}, bot.last_sticker)
                    self.assertEqual({'p:1': ['fixture.gif']}, bot.recent_stickers)

    def test_disabled_stickers_block_both_direct_and_selected_image_paths(self):
        for app in APPS:
            for direct in (False, True):
                bot, item, _ = self.bot(app, 'never-read.gif', enabled=False)
                self.assertFalse(self.invoke(bot, item, direct))
                bot._send.assert_not_called()
                bot.stickers.path.assert_not_called()
                bot._select_sticker.assert_not_called()

    def test_marker_extraction_and_mood_use_all_supported_formats_including_id_zero(self):
        for app in APPS:
            _bot, _item, ns = self.bot(app, 'never-read.gif')
            self.assertEqual(('你好', '开心'), ns['take_sticker_marker']('你好【表情包：开心】'))
            self.assertEqual('开心', ns['mood_sticker_keyword']('', '[偷笑]'))
            self.assertEqual('震惊', ns['mood_sticker_keyword']('', '【惊讶】'))

    def reply_tail(self, app, reply, failed, wait, keyword):
        reply_node = methods(app / 'flow_reply.py')['_reply']
        flag = next(n for n in reply_node.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '_reply_text_failed' for t in n.targets))
        sender = next(n for n in reply_node.body if isinstance(n, ast.If) and ast.unparse(n.test) == 'reply and (not doc_name)')
        images = next(n for n in reply_node.body if isinstance(n, ast.Try) and
                      any(isinstance(x, ast.Name) and x.id == '_reply_text_failed' for x in ast.walk(n)))
        bot = SimpleNamespace(cfg={}, _text_segs=lambda text: [{'type': 'text', 'data': {'text': text}}],
                              _send=Mock(return_value=not failed), _send_detail=Mock(return_value={'ok': not failed}),
                              _judge_sticker=Mock(return_value={'file': 'fixture.gif'}),
                              send_sticker=Mock(return_value={'file': 'fixture.gif'}),
                              send_sticker_item=Mock(return_value=True))
        ns = dict(self=bot, reply=reply, doc_name='', _image_qa=False, _voice_turn=False, _voice_all=False,
                  _voice_atts=[], _bubble_gap=lambda: None, _wait_before_bubble=lambda: None,
                  sent_any=False, sent_parts=[], split_chunks=lambda text: [text], split_reply_units=lambda text: [text],
                  MAX_TEXT_PARTS=3, text_seg=lambda text: {'type': 'text', 'data': {'text': text}},
                  conv='p:1', is_group=False, gid=0, uid=1, name='fixture', is_owner=False,
                  cmd_text='fixture', wait_ready=wait, sticker_kw='开心' if keyword else '',
                  sticker_item=None if keyword else {'file': 'fixture.gif'}, sticker=None, sticker_sent=False,
                  time=SimpleNamespace(sleep=Mock()), log=Mock())
        exec(compile(ast.Module(body=[flag, sender, images], type_ignores=[]), '<actual-reply-and-image-tail>', 'exec'), ns)
        return bot

    def test_reply_text_failure_does_not_send_a_detached_image(self):
        for app in APPS:
            for wait in (False, True):
                for keyword in (False, True):
                    bot = self.reply_tail(app, '你好', True, wait, keyword)
                    bot.send_sticker.assert_not_called()
                    bot.send_sticker_item.assert_not_called()
                    bot._judge_sticker.assert_not_called()

    def test_image_only_reply_and_successful_text_keep_image_delivery(self):
        for app in APPS:
            for reply in ('', '你好'):
                bot = self.reply_tail(app, reply, False, True, False)
                bot.send_sticker_item.assert_called_once()


if __name__ == '__main__':
    unittest.main()
