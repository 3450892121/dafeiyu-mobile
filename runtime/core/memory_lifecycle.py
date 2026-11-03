# -*- coding: utf-8 -*-
"""Shared lifecycle rules. Source rows are removed only after verified archival."""
import time


class MemoryLifecycle:
    def mark_memory_background(self, table, mid, now=None):
        if table not in self.MEMORY_TABLES:
            return
        with self._lock:
            self.conn.execute('UPDATE '+table+' SET last_background_at=? WHERE id=?',(int(now or time.time()),int(mid)))
            self.conn.commit()

    def memory_usage_rows(self, limit=50):
        with self._lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM memory_usage ORDER BY id DESC LIMIT ?', (min(100,max(1,int(limit))),)).fetchall()]

    def _archive_and_delete(self, table, where, params, reason):
        if table not in self.MEMORY_TABLES:
            raise ValueError('invalid memory table')
        from store import _dec
        safe_where = '(' + where + ") AND protected=0"
        with self._lock:
            rows = self.conn.execute('SELECT id,text,key_facts FROM ' + table + ' WHERE ' + safe_where, params).fetchall()
            self._archive_rows(table, safe_where, params, reason=reason)
            verified = []
            for row in rows:
                address = 'cold://%s%d' % (self._ARCHIVE_PREFIX[table], int(row['id']))
                saved = self.conn.execute('SELECT text,key_facts FROM memory_archive WHERE address=?', (address,)).fetchone()
                # A stale archive or failed write must never authorize deletion of newer text.
                if saved and _dec(saved['text']) == _dec(row['text']) and (saved['key_facts'] or '') == (row['key_facts'] or ''):
                    verified.append(int(row['id']))
            deleted = 0
            for mid in verified:
                cur = self.conn.execute('DELETE FROM ' + table + ' WHERE id=? AND protected=0 AND (' + where + ')', (mid, *params))
                deleted += max(0, cur.rowcount)
            return deleted

    def set_memory_protected(self, table, mid, protected):
        if table not in self.MEMORY_TABLES:
            return False
        with self._lock:
            cur = self.conn.execute("UPDATE " + table + " SET protected=?,status=CASE WHEN ?=1 THEN 'active' ELSE status END WHERE id=? AND status<>'deleted'",
                                    (int(bool(protected)), int(bool(protected)), int(mid)))
            self.conn.commit()
            return cur.rowcount == 1

    def record_memory_retrieval(self, table, ids, now=None):
        if table not in self.MEMORY_TABLES or not ids:
            return
        with self._lock:
            self.conn.executemany('UPDATE ' + table + ' SET retrieved_at=? WHERE id=?',
                                  [(int(now or time.time()), int(mid)) for mid in ids])

    def record_memory_mention(self, table, mid, conv, source_mid, now=None):
        if table not in self.MEMORY_TABLES:
            return False
        stamp = int(now or time.time())
        with self._lock:
            cur = self.conn.execute('INSERT OR IGNORE INTO memory_usage(table_name,memory_id,conv,source_mid,kind,ts) VALUES(?,?,?,?,?,?)',
                                    (table, int(mid), conv, str(source_mid), 'confirmed_mention', stamp))
            if cur.rowcount:
                self.conn.execute('UPDATE ' + table + ' SET actual_used_at=?,last_mentioned_at=?,last_used=?,use_count=use_count+1 WHERE id=?',
                                  (stamp, stamp, stamp, int(mid)))
            self.conn.commit()
            return cur.rowcount == 1
