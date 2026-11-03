"""Production consumer methods with fake writers/transports; no files, models or network."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
APPS = (Path(__file__).resolve().parents[2] / 'runtime' / 'core',)
sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(APPS[0]))
NOTE = '（此回复的接话对象：测试(QQ123456)，对应来话#42）'
SHORT = '（接话对象：测试(QQ123456)，对应来话#42）'
UNSAFE = '正文。（此回复的接话对象'


def load_guard(app):
    spec = importlib.util.spec_from_file_location('annotation_media_guard', app / 'guard.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def media_namespace(app):
    guard = load_guard(app)
    tree = ast.parse((app / 'flow_media.py').read_text(encoding='utf-8'))
    names = {'_clean_response_output', 'draw_request_body', '_make_doc', '_make_long_doc',
             '_make_calc_doc', '_send_long_doc', '_send_calc_doc', '_make_tool_file', '_draw'}
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(guard=guard, re=re, json=json, time=time, uuid=uuid, os=os, log=Mock(),
              FILES_DIR='synthetic-no-files', DRAW_MODEL='synthetic-model',
              DRAW_SIZE_DEFAULT='1.5K', DRAW_SIZE_HD='2K', DRAW_SIZES={'1.5K', '2K'},
              DRAW_MAX_REF=2, DRAW_API='http://synthetic.invalid/images', DRAW_TIMEOUT=1,
              text_seg=lambda text: dict(type='text', data=dict(text=text)),
              tools_gate=SimpleNamespace(note=Mock()))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-media-consumers>', 'exec'), ns)
    return ns


class FakeDocument:
    def __init__(self):
        self.lines = []
        self.saved = []
        font = SimpleNamespace(size=None)
        self.styles = {'Normal': SimpleNamespace(font=font, element=SimpleNamespace(
            rPr=SimpleNamespace(rFonts=SimpleNamespace(set=lambda *a: None))))}

    def add_heading(self, text, *a, **kw):
        self.lines.append(text)

    def add_paragraph(self, text=None, *a, **kw):
        if text is not None:
            self.lines.append(text)
        def add_run(value):
            self.lines.append(value)
            return SimpleNamespace(italic=False)
        return SimpleNamespace(add_run=add_run)

    def save(self, path):
        self.saved.append(path)  # Deliberately never create a file.


class Tests(unittest.TestCase):
    def test_raw_validator_rejects_annotations_and_ambiguous_truncation(self):
        for app in APPS:
            guard = load_guard(app)
            for text in ('正文。' + NOTE, '正文。' + SHORT,
                         '正文。（回复的接话对象：测试，对应来话#42）',
                         '正文。接话对象：测试(QQ123456)，对应来话#42', UNSAFE):
                with self.subTest(app=str(app), text=text):
                    self.assertEqual('drop', guard.check(text)['action'])
                    if hasattr(guard, 'content_allowed'):
                        self.assertFalse(guard.content_allowed(SimpleNamespace(cfg={}), text))
            self.assertEqual('正文。', guard.scrub('正文。' + SHORT))
            self.assertEqual('ok', guard.check(guard.scrub('正文。' + SHORT))['action'])

    def test_ordinary_addressee_and_qq_discussion_stays(self):
        for app in APPS:
            guard = load_guard(app)
            for text in ('这次接话对象：甲，先听他说。', '接话对象：甲。QQ123456是你提到的号。',
                         '“此回复的接话对象”这几个字是说明。', '对应来话#42是截图里的字样。',
                         '联系甲(QQ123456)，记得（先问候）。'):
                with self.subTest(app=str(app), text=text):
                    self.assertEqual(text, guard.scrub(text))
                    self.assertEqual('ok', guard.check(text)['action'])

    def test_generated_document_passes_cleaned_text_to_actual_writer_loop(self):
        for app in APPS:
            ns = media_namespace(app)
            for text, expected in (('正文。' + NOTE, ['正文。']), ('正文。' + SHORT, ['正文。']),
                                   (UNSAFE, [])):
                doc = FakeDocument()
                bot = SimpleNamespace(cfg={}, pool=SimpleNamespace(chat=Mock(
                    return_value=SimpleNamespace(ok=True, text=text))))
                with patch.dict(sys.modules, {'docx': SimpleNamespace(Document=lambda: doc)}):
                    result = ns['_make_doc'](bot, 'doc', '虚构主题')
                self.assertEqual(expected, doc.lines)
                self.assertEqual(bool(expected), bool(result[0]))
                self.assertEqual(bool(expected), bool(doc.saved))

    def test_tool_documents_clean_nested_strings_before_dispatch(self):
        for app in APPS:
            ns = media_namespace(app)
            for tool in ('make_document', 'make_spreadsheet', 'make_slides'):
                writer = Mock(return_value=('synthetic.docx', ''))
                bot = SimpleNamespace(cfg={}, _tool_make_docx=writer,
                    _tool_make_xlsx=writer, _tool_make_pptx=writer)
                args = {'title': '题目' + NOTE, 'content': '正文' + SHORT,
                        'headers': ['表头' + NOTE], 'rows': [['单元格' + SHORT]],
                        'slides': [{'title': '一页' + NOTE, 'bullets': ['要点' + SHORT]}]}
                self.assertTrue(ns['_make_tool_file'](bot, tool, args)[0])
                sent = writer.call_args.args[0]
                self.assertNotIn('接话对象', json.dumps(sent, ensure_ascii=False))
                self.assertEqual('正文', sent['content'])
                self.assertIn('接话对象', args['content'])  # Caller data was not mutated.
                writer.reset_mock()
                self.assertIsNone(ns['_make_tool_file'](bot, tool, {'content': UNSAFE})[0])
                writer.assert_not_called()
                # Headers/bullets can contain objects that downstream writers stringify.
                # Reject an annotation key; rewriting it could silently collide with a key.
                for malicious in ({'title': '表格', 'headers': [{NOTE: '值'}], 'rows': [['值']]},
                                  {'title': '演示', 'slides': [{'bullets': [{NOTE: '值'}]}]}):
                    with self.subTest(app=str(app), tool=tool, malicious=malicious):
                        self.assertIsNone(ns['_make_tool_file'](bot, tool, malicious)[0])
                        writer.assert_not_called()

    def test_long_and_calc_doc_writers_receive_cleaned_body(self):
        for app in APPS:
            ns = media_namespace(app)
            bot = SimpleNamespace(_SEC_RE=re.compile(r'^$'),
                _add_tol_runs=lambda paragraph, text: paragraph.add_run(text))
            for method in ('_make_long_doc', '_make_calc_doc'):
                doc = FakeDocument()
                modules = {'docx': SimpleNamespace(Document=lambda: doc),
                           'docx.shared': SimpleNamespace(Pt=lambda value: value),
                           'docx.oxml.ns': SimpleNamespace(qn=lambda value: value)}
                with patch.dict(sys.modules, modules):
                    path = ns[method](bot, '正文。' + NOTE)
                self.assertTrue(path)
                self.assertIn('正文。', doc.lines)
                self.assertNotIn('接话对象', ''.join(doc.lines))
                self.assertTrue(doc.saved)
                with patch.dict(sys.modules, modules):
                    self.assertIsNone(ns[method](bot, UNSAFE))

    def test_long_and_calc_upload_entry_uses_same_cleaned_value(self):
        for app in APPS:
            ns = media_namespace(app)
            for method in ('_send_long_doc', '_send_calc_doc'):
                for group in (False, True):
                    writer = Mock(return_value='synthetic.docx')
                    upload = Mock()
                    bot = SimpleNamespace(cfg={}, _make_long_doc=writer, _make_calc_doc=writer,
                        onebot=SimpleNamespace(upload_group_file=upload, upload_private_file=upload))
                    self.assertTrue(ns[method](bot, 'g:7', group, 7, 10, '正文。' + SHORT))
                    self.assertEqual('正文。', writer.call_args.args[0])
                    upload.assert_called_once()
                    writer.reset_mock(); upload.reset_mock()
                    self.assertFalse(ns[method](bot, 'g:7', group, 7, 10, UNSAFE))
                    writer.assert_not_called(); upload.assert_not_called()

    def test_actual_draw_request_uses_cleaned_prompt_and_blocks_truncation(self):
        for app in APPS:
            ns = media_namespace(app)
            for prompt, expected_calls in (('画一只鲸。' + NOTE, 1), ('画一只鲸。' + SHORT, 1),
                                            (UNSAFE, 0)):
                requests = []
                def fake_open(request, **kw):
                    requests.append(json.loads(request.data))
                    raise RuntimeError('stop before network/download')
                ns['urllib'] = SimpleNamespace(request=SimpleNamespace(
                    Request=urllib.request.Request, urlopen=fake_open), error=urllib.error)
                bot = SimpleNamespace(cfg={}, store=object(), _send=Mock(),
                    _tool_owner_remind=Mock(), _draw_key=lambda: 'fictional-key',
                    _draw_prompt_text=lambda *a: prompt, _draw_slot_end=Mock())
                ns['_draw'](bot, '鲸', 'g:7', True, 7, 10)
                self.assertEqual(expected_calls, len(requests))
                if requests:
                    self.assertEqual('画一只鲸。', requests[0]['prompt'])

    def test_actual_tts_synthesis_gate_blocks_raw_note_before_http(self):
        guard = load_guard(APPS[0])
        tree = ast.parse((APPS[0] / 'voice.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'synth')
        base = Mock(side_effect=AssertionError('unsafe text must stop before TTS routing'))
        ns = dict(guard=guard, SimpleNamespace=SimpleNamespace, _base=base)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-tts-gate>', 'exec'), ns)
        for text in ('正文。' + NOTE, '正文。' + SHORT, UNSAFE,
                     '正文。接话对象：测试(QQ123456)，对应来话#42'):
            result, error = ns['synth']({}, text)
            self.assertIsNone(result)
            self.assertTrue(error)
        base.assert_not_called()


if __name__ == '__main__':
    unittest.main()
