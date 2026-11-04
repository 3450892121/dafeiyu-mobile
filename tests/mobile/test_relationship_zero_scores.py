"""Actual zero scores survive setters, summaries and the one-time legacy import."""
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from store import Store


class RelationshipZeroScoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.path = Path(self.folder.name) / 'bot.db'
        self.now = int(time.time())
        self.clock = patch('store.time.time', return_value=self.now)
        self.clock.start()
        self.store = Store(str(self.path))

    def tearDown(self):
        self.store.conn.close()
        self.clock.stop()
        self.folder.cleanup()

    def test_manual_zero_is_persisted_and_shown_consistently_after_reopen(self):
        self.assertEqual(self.store.set_favor(20, 0), 0)
        self.assertEqual(self.store.set_trust(20, 0), 0)
        row = self.store.get_person_relation(20)
        self.assertEqual((row['favor'], row['trust']), (0, 0))
        favor = self.store.favor_history(20)[-1]
        self.assertEqual((favor['favor'], favor['delta']), (0, -50))
        trust = self.store.conn.execute('SELECT trust,delta FROM trust_history WHERE qq=20').fetchone()
        self.assertEqual(tuple(trust), (0, -30))
        self.store.add_message('g:7', 20, '合成零分用户', 'in', '合成消息', '', 101)
        self.store.conn.close()
        self.store = Store(str(self.path))
        effective = self.store.get_person_relation_effective(20)
        detail = self.store.person_detail(20, [7])['relation']
        overview = next(row for row in self.store.people_overview([20], [7]) if row['qq'] == 20)
        member = next(row for row in self.store.group_detail(7)['members'] if row['qq'] == 20)
        for values in (effective, detail, overview):
            self.assertEqual((values['favor'], values['trust']), (0, 0))
        self.assertEqual(member['favor'], 0)

    def test_none_keeps_defaults_and_string_zero_is_also_valid(self):
        self.assertEqual(self.store.set_favor(20, None), 50)
        self.assertEqual(self.store.set_trust(20, None), 30)
        self.assertEqual(self.store.set_favor(20, '0'), 0)
        self.assertEqual(self.store.set_trust(20, '0'), 0)
        self.assertEqual(self.store.get_person_relation_effective(20)['favor'], 0)
        self.assertEqual(self.store.get_person_relation_effective(20)['trust'], 0)

    def test_legacy_zero_import_preserves_zero_without_rerunning_completed_migration(self):
        self.store.conn.close()
        other = Path(self.folder.name) / 'legacy.db'
        conn = sqlite3.connect(str(other))
        conn.execute("CREATE TABLE relations(conv TEXT PRIMARY KEY,name TEXT,favor INTEGER,"
                     "grudge TEXT DEFAULT '',updated_at INTEGER)")
        conn.executemany('INSERT INTO relations VALUES(?,?,?,?,?)', [
            ('p:20', '合成旧用户', 0, '', self.now),
            ('p:21', '合成缺省用户', None, '', self.now),
            ('g:7', '合成旧群', 0, '', self.now)])
        conn.commit()
        conn.close()
        self.store = Store(str(other))
        self.assertEqual(self.store.get_person_relation(20)['favor'], 0)
        self.assertEqual(self.store.get_person_relation(21)['favor'], 50)
        self.assertEqual(self.store.get_group_mood(7)['mood'], 0)
        self.assertEqual(self.store.get_setting('profile_v1_done'), '1')
        self.store.set_favor(20, 5)
        self.store.conn.close()
        self.store = Store(str(other))
        self.assertEqual(self.store.get_person_relation(20)['favor'], 5)

    def test_comparison_does_not_make_zero_or_neutral_favor_jealous(self):
        for uid, favor, expected in ((20, 0, 0), (21, 50, 0), (22, 65, 4)):
            with self.subTest(favor=favor):
                self.store.set_favor(uid, favor)
                self.store.set_trust(uid, 90)
                event = self.store.apply_interaction(uid, 'p:%d' % uid, 'comparison-1',
                    '我觉得另一个机器人比你更亲近',
                    {'kind': 'comparison', 'target': 'bot', 'confidence': .99,
                     'evidence': '我觉得另一个机器人比你更亲近'}, now=self.now)
                self.assertIsNotNone(event)
                emotions = {row['kind']: row['intensity'] for row in self.store.get_emotions(uid)}
                self.assertEqual(emotions.get('jealousy', 0), expected)
                self.assertEqual(self.store.get_person_relation(uid)['favor'], favor)


if __name__ == '__main__':
    unittest.main()
