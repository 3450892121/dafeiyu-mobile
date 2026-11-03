"""Parse QQ face shorthand through actual send methods, with a fake transport only."""
import ast
import importlib.util
from pathlib import Path
import re
import socket
import sys
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
APPS = (Path(__file__).resolve().parents[2] / 'runtime' / 'core',)


def load(path):
    spec = importlib.util.spec_from_file_location('face_fixture_' + path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def methods(path):
    return {node.name: node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig')))
            if isinstance(node, ast.FunctionDef)}


class QQFaceShorthandTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No QQ/network in face tests'))
        network.start()
        self.addCleanup(network.stop)

    def each(self):
        for app in APPS:
            yield app, load(app / 'qq_faces.py')

    def test_shorthand_becomes_real_face_id_20_in_place(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                self.assertEqual([('text', '早上好'), ('face', 20), ('text', '接着聊')],
                                 faces.split('早上好[偷笑]接着聊'))
                self.assertEqual([('face', 20)], faces.split('[偷笑]'))
                self.assertEqual([('text', 'hi!'), ('face', 20)], faces.split('hi![偷笑]'))

    def test_current_face_table_includes_zero_and_new_duplicate_ids(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                self.assertEqual(329, len(faces.FACES))
                self.assertEqual([('face', 0)], faces.split('[惊讶]'))
                self.assertEqual([('face', 0)], faces.split('[脸:惊讶]'))
                self.assertEqual('惊讶', faces.name_of('0'))
                for empty in (None, '', False, 'not-an-id'):
                    self.assertEqual('', faces.name_of(empty))
                    self.assertEqual('', faces.label(empty))
                self.assertEqual('[脸:惊讶]', faces.normalize('【惊讶】'))
                for fid in range(450, 458):
                    self.assertTrue(faces.name_of(fid))
                self.assertEqual(14, faces.resolve('微笑'))  # Preserve the existing canonical ID.

    def test_bracket_width_spacing_slashes_and_ascii_case_are_supported(self):
        for app, faces in self.each():
            for text in ('［偷笑］', '【偷笑】', '[ 偷笑 ]', '[ /偷笑 ]', '【 脸 ： 偷笑 】',
                         '［QQ表情： 偷笑］', '[小黄脸 偷笑]', '[捂嘴笑]'):
                with self.subTest(app=str(app), text=text):
                    self.assertEqual([('face', 20)], faces.split(text))
            for text in ('[DOGE]', '【ｄｏｇｅ】', '[QQ表情: DoGe]'):
                self.assertEqual([('face', 179)], faces.split(text))

    def test_unknown_explicit_markers_are_removed_without_substring_guessing(self):
        for app, faces in self.each():
            for text in ('[脸:不要偷笑]', '【QQ表情：我不想哭】', '[超级表情:未知新名字]',
                         '[脸:这是很长的一个模型编造的不存在的表情名字]', '[脸:没有结束', '【表情包：没结束'):
                with self.subTest(app=str(app), text=text):
                    self.assertEqual([], faces.split(text))
                    self.assertEqual('', faces.strip(text))

    def test_cq_faces_use_known_ids_without_executing_other_cq_directives(self):
        for app, faces in self.each():
            self.assertEqual([('face', 20), ('face', 0)], faces.split('[CQ:face,id=20][CQ:face,id=0]'))
            for text in ('[CQ:face,id=999999]', '[CQ:face,id=made-up]',
                         '[CQ:image,file=https://example.invalid/private]', '[CQ:record,file=anything]'):
                self.assertEqual([], faces.split(text))

    def test_image_directives_and_placeholder_then_keyword_never_leak(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                text = '[表情]你好【表情包：开心】【表情：另外一张】[偷笑]'
                self.assertEqual(('你好[偷笑]', '开心'), faces.take_sticker_marker(text))
                self.assertEqual([('text', '你好'), ('face', 20)], faces.split(text))
                self.assertEqual('你好', faces.strip(text))

    def test_known_words_inside_code_or_markdown_links_are_literal(self):
        samples = ('`[偷笑]` 的写法', '```text\n[脸:偷笑]\n```',
                   '[微笑](https://example.invalid)', '![偷笑](fixture.png)', '【参考答案】[注意]')
        for app, faces in self.each():
            for text in samples:
                with self.subTest(app=str(app), text=text):
                    self.assertEqual([('text', text)], faces.split(text))
                    self.assertEqual(text, faces.strip(text))

    def test_all_known_names_and_non_numeric_aliases_use_exact_lookup(self):
        for app, faces in self.each():
            for name in set(faces.names()) | {n for n in faces._ALIAS if not n.isdigit()}:
                with self.subTest(app=str(app), name=name):
                    self.assertEqual([('face', faces.resolve(name))], faces.split('[' + name + ']'))

    def test_multiple_mixed_markers_preserve_order_and_existing_segments(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                self.assertEqual([('text', '甲'), ('face', 20), ('text', '乙'),
                                  ('face', faces.resolve('捂脸')), ('face', faces.resolve('狗头')),
                                  ('text', '丙')], faces.split('甲[偷笑]乙[脸:捂脸][狗头]丙'))

    def test_ordinary_brackets_unknown_names_and_partial_names_stay_text(self):
        samples = ('[注意]先备份', '[参考答案][图片][文件:例子.txt]',
                   '[这个很好笑][偷笑一下][想一下再说][没有这个表情]',
                   '[1][6][20][2026]', '数组[0]，列表[1, 2]，链接[说明](https://example.invalid)',
                   '[偷笑', '偷笑]', '[偷\n笑]')
        for app, faces in self.each():
            for text in samples:
                with self.subTest(app=str(app), text=text):
                    self.assertEqual([('text', text)], faces.split(text))
                    self.assertEqual(text, faces.strip(text))

    def test_existing_explicit_syntax_and_unknown_marker_policy_are_preserved(self):
        for app, faces in self.each():
            for marker in ('[脸:偷笑]', '[脸：偷笑]', '[小黄脸:偷笑]', '[QQ表情:偷笑]', '[超级表情:偷笑]'):
                with self.subTest(app=str(app), marker=marker):
                    self.assertEqual([('face', 20)], faces.split(marker))
                    self.assertEqual('', faces.strip(marker))
            self.assertEqual([('text', '甲'), ('text', '乙')], faces.split('甲[脸:不存在的标记]乙'))
            # The first capture remains compatible with emoticon conversion callers.
            self.assertEqual('偷笑', faces.MARKER_RE.search('[脸:偷笑]').group(1))

    def test_document_text_strips_both_formats_consistently(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                self.assertEqual('你好[注意]先备份', faces.strip('你好[偷笑][注意]先备份[脸:捂脸]'))
                self.assertEqual('', faces.strip(None))
                self.assertEqual([], faces.split(None))

    def test_actual_tts_sanitizer_does_not_read_shorthand_face_names(self):
        faces = load(APPS[0] / 'qq_faces.py')
        tree = ast.parse((APPS[0] / 'voice.py').read_text(encoding='utf-8-sig'))
        nodes = [node for node in tree.body
                 if (isinstance(node, ast.FunctionDef) and node.name == 'clean_for_tts') or
                 (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_DROP_RES'
                                                     for t in node.targets))]
        namespace = {'qq_faces': faces, 're': re, 'strip_choice': lambda t: str(t or '')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-face-tts-cleaner>', 'exec'), namespace)
        with patch.dict(sys.modules, {'guard': SimpleNamespace(strip_voice_notes=lambda t: t)}):
            self.assertEqual('你好[注意]先备份', namespace['clean_for_tts']('你好[偷笑][注意]先备份[脸:捂脸]'))
            self.assertEqual('', namespace['clean_for_tts']('[偷笑]'))

    def sender(self, app, faces):
        functions = methods(app / 'flow_reply.py')
        selected = [functions['_send']]
        if '_send_detail' in functions:
            selected.append(functions['_send_detail'])
        selected.append(methods(app / 'flow_stickers.py')['_text_segs'])
        transport = load(app / 'onebot.py')
        namespace = {'qq_faces': faces, 'text_seg': transport.text_seg, 'face_seg': transport.face_seg,
                     'at_seg': transport.at_seg, 'log': Mock(), 'note_delivery': Mock(),
                     'guard': SimpleNamespace(scrub=lambda t: t, strip_image_notes=lambda t: t,
                                              check=lambda t: {'action': 'ok'}, content_allowed=lambda *a: True),
                     'blocked': lambda *a: False, 'policy_changed': lambda *a: False,
                     'is_connection_refused': lambda e: False, 'OneBotError': transport.OneBotError,
                     'time': SimpleNamespace(sleep=Mock())}
        exec(compile(ast.Module(body=selected, type_ignores=[]), '<actual-face-send>', 'exec'), namespace)
        send = Mock(return_value={'message_id': 77})
        bot = SimpleNamespace(onebot=SimpleNamespace(send_private=send, send_group=send),
                              store=SimpleNamespace(add_log=Mock()), _take_group_at=lambda *a: None)
        for node in selected:
            setattr(bot, node.name, MethodType(namespace[node.name], bot))
        return bot, send, transport

    def test_final_sender_converts_direct_text_in_private_and_group(self):
        for app, faces in self.each():
            for is_group in (False, True):
                with self.subTest(app=str(app), is_group=is_group):
                    bot, send, transport = self.sender(app, faces)
                    self.assertTrue(bot._send(is_group, 123, 456, [transport.text_seg('你好[偷笑]')]))
                    send.assert_called_once_with(123 if is_group else 456,
                                                 [transport.text_seg('你好'), transport.face_seg(20)])

    def test_preparsed_reply_is_not_converted_or_duplicated_twice(self):
        for app, faces in self.each():
            with self.subTest(app=str(app)):
                bot, send, transport = self.sender(app, faces)
                segments = bot._text_segs('你好[偷笑]')
                self.assertTrue(bot._send(False, 0, 456, segments))
                send.assert_called_once_with(456, [transport.text_seg('你好'), transport.face_seg(20)])

    def test_final_sender_does_not_submit_messages_emptied_by_marker_cleanup(self):
        for app, faces in self.each():
            for text in ('[脸:未知名称]', '【表情包：开心】', '[表情]', '[QQ表情:截断'):
                with self.subTest(app=str(app), text=text):
                    bot, send, transport = self.sender(app, faces)
                    self.assertFalse(bot._send(False, 0, 456, [transport.text_seg(text)]))
                    send.assert_not_called()

    def test_long_messages_and_length_trimming_never_cut_a_marker_in_half(self):
        for app, faces in self.each():
            funcs = methods(app / 'flow_reply.py')
            namespace = {'qq_faces': faces, 're': re, 'MAX_TEXT_CHUNK': 900, 'MAX_TEXT_BYTES': 4000}
            exec(compile(ast.Module(body=[funcs['split_chunks'], funcs['_trim_reply']], type_ignores=[]),
                         '<actual-face-text-boundaries>', 'exec'), namespace)
            for padding in range(1, 25):
                for marker in ('[偷笑]', '[脸:偷笑]', '【QQ表情：偷笑】'):
                    with self.subTest(app=str(app), padding=padding, marker=marker):
                        text = '甲' * padding + marker + '乙' * 35
                        chunks = namespace['split_chunks'](text, limit=12, byte_limit=36)
                        self.assertEqual(faces.normalize(text), ''.join(chunks))
                        self.assertEqual([20], [value for chunk in chunks for kind, value in faces.split(chunk) if kind == 'face'])
                        trimmed = namespace['_trim_reply'](text, padding + 2)
                        self.assertNotIn('[', trimmed)
                        self.assertNotIn('【', trimmed)
            self.assertEqual(['你好'], namespace['split_chunks']('你好[脸:' + '不存在' * 1000 + ']', limit=12, byte_limit=36))


if __name__ == '__main__':
    unittest.main()
