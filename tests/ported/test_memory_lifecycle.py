import os, sys, tempfile, time, unittest
sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core')))
from store import Store


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.s = Store(os.path.join(self.tmp.name, 'bot.db'))

    def tearDown(self):
        self.s.conn.close(); self.tmp.cleanup()

    def old_decayed(self):
        item = self.s.add_person_memory(1, '小甲考试取得的重要结果', 'fact', 3)
        self.s.conn.execute("UPDATE person_memories SET status='decayed',decayed_at=? WHERE id=?", (int(time.time())-200*86400, item['id']))
        self.s.conn.commit()
        return item['id']

    def test_daily_prune_archives_before_delete(self):
        mid = self.old_decayed(); self.s.prune()
        self.assertIsNone(self.s.conn.execute('SELECT id FROM person_memories WHERE id=?', (mid,)).fetchone())
        self.assertIn('考试', self.s.dig_memory('cold://p%d' % mid)['text'])

    def test_failed_archive_keeps_original(self):
        mid = self.old_decayed()
        self.s._archive_rows = lambda *a, **k: 0
        self.s.prune()
        self.assertIsNotNone(self.s.conn.execute('SELECT id FROM person_memories WHERE id=?', (mid,)).fetchone())

    def test_stale_archive_does_not_delete_newer_source(self):
        mid = self.old_decayed()
        self.s._archive_rows('person_memories', 'id=?', (mid,), 'test')
        self.s.update_memory_row('person_memories', mid, text='新的纠正结果必须完整保留')
        self.s.prune()
        self.assertIsNotNone(self.s.conn.execute('SELECT id FROM person_memories WHERE id=?', (mid,)).fetchone())

    def test_protection_survives_decay_capacity_and_tier_cap(self):
        item = self.s.add_person_memory(1, '主人长期保留的重要约定', 'fact', 4)
        self.s.set_memory_protected('person_memories', item['id'], True)
        self.s.conn.execute('UPDATE person_memories SET created=?', (int(time.time())-1000*86400,)); self.s.conn.commit()
        for i in range(35): self.s.add_person_memory(1, '普通资料内容编号%d' % i, 'fact', 5)
        self.s.cap_high_tier(5); self.s._prune_scope('person_memories', 'qq', 1, 10); self.s.decay_memories(); self.s.prune()
        row = self.s.conn.execute('SELECT status,importance FROM person_memories WHERE id=?', (item['id'],)).fetchone()
        self.assertEqual(('active', 4), tuple(row))

    def test_retrieval_does_not_extend_lifetime_and_mentions_deduplicate(self):
        item = self.s.add_person_memory(1, '小甲喜欢有星空的游戏', 'preference', 3)
        self.s.search_person_memories(1, ['星空'], include_private=True)
        row = self.s.conn.execute('SELECT last_used,use_count,retrieved_at FROM person_memories WHERE id=?', (item['id'],)).fetchone()
        self.assertEqual((0,0), tuple(row)[:2]); self.assertGreater(row[2], 0)
        self.assertTrue(self.s.record_memory_mention('person_memories', item['id'], 'p:1', 'm1'))
        self.assertFalse(self.s.record_memory_mention('person_memories', item['id'], 'p:1', 'm1'))
        self.assertEqual(1, self.s.conn.execute('SELECT use_count FROM person_memories WHERE id=?', (item['id'],)).fetchone()[0])


if __name__ == '__main__': unittest.main()
