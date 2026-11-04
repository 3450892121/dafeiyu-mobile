"""Exercise the actual console POST handler in memory, with a temporary relationship DB."""
import ast
from http.server import BaseHTTPRequestHandler
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import console_base as panel
from store import Store


class PersonRelationshipSaveTests(unittest.TestCase):
    uid = 190009

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.cfg = {'admin_token': 'fictional-person-save-token'}
        sup = SimpleNamespace(cfg=self.cfg, store=self.store, port=33114)
        module = ast.parse(Path(panel.__file__).read_text(encoding='utf-8'))
        main = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'main')
        handler = next(node for node in main.body
                       if isinstance(node, ast.ClassDef) and node.name == 'H')
        namespace = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, sup=sup, json=json,
                         hmac=hmac, parse_qs=parse_qs, urlsplit=urlsplit)
        exec(compile(ast.Module(body=[handler], type_ignores=[]),
                     '<person-save-console-http>', 'exec'), namespace)
        self.Handler = namespace['H']
        self.reset_relation()

    def reset_relation(self):
        self.store.conn.execute(
            'INSERT INTO person_relations(qq,favor,trust,updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(qq) DO UPDATE SET favor=excluded.favor,trust=excluded.trust,'
            'updated_at=excluded.updated_at', (self.uid, 52, 31, int(time.time())))
        self.store.conn.commit()

    def scores(self):
        relation = self.store.get_person_relation(self.uid)
        return relation['favor'], relation['trust']

    def request(self, fields, *, form=False, authenticated=True):
        data = dict(qq=self.uid, **fields)
        body = (urlencode(data) if form else json.dumps(data)).encode('utf-8')
        handler = self.Handler.__new__(self.Handler)
        handler.path = '/api/person_save'
        handler.headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
                           'Content-Length': str(len(body)), 'Host': '127.0.0.1:33114'}
        if authenticated:
            handler.headers['X-Admin-Token'] = self.cfg['admin_token']
        handler.connection = Mock()
        handler.rfile = io.BytesIO(body)
        handler._json = Mock()
        handler.do_POST()
        handler._json.assert_called_once()
        return handler._json.call_args.args

    def test_json_numeric_zero_is_saved_to_both_relationship_axes(self):
        self.assertEqual((200, {'ok': True}), self.request({'favor': 0, 'trust': 0}))
        self.assertEqual((0, 0), self.scores())

    def test_panel_form_string_zero_is_saved_to_both_relationship_axes(self):
        self.assertEqual((200, {'ok': True}), self.request({'favor': '0', 'trust': '0'}, form=True))
        self.assertEqual((0, 0), self.scores())

    def test_null_missing_and_blank_fields_leave_existing_scores_unchanged(self):
        for fields, form in (({}, False), ({'favor': None, 'trust': None}, False),
                             ({'favor': None}, False), ({'trust': None}, False),
                             ({'favor': '', 'trust': '   '}, False),
                             ({'favor': ' ', 'trust': ''}, True)):
            with self.subTest(fields=fields, form=form):
                self.reset_relation()
                self.assertEqual((200, {'ok': True}), self.request(fields, form=form))
                self.assertEqual((52, 31), self.scores())

    def test_updating_one_axis_to_zero_preserves_the_omitted_axis(self):
        self.assertEqual((200, {'ok': True}), self.request({'favor': 0}))
        self.assertEqual((0, 31), self.scores())
        self.reset_relation()
        self.assertEqual((200, {'ok': True}), self.request({'trust': 0}))
        self.assertEqual((52, 0), self.scores())

    def test_zero_update_still_requires_authenticated_local_management(self):
        self.assertEqual((403, {'ok': False, 'error': 'token'}),
                         self.request({'favor': 0, 'trust': 0}, authenticated=False))
        self.assertEqual((52, 31), self.scores())


if __name__ == '__main__':
    unittest.main()
