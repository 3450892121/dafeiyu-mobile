"""Regression for the shared SQLite connection/GIL deadlock at brain startup."""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BOT = Path(__file__).resolve().parents[2] / 'runtime' / 'core'
sys.path.insert(0, str(BOT))


def startup_stress():
    from blacklist import attachments_allowed
    from flow_memory import MemoryMixin
    from store import Store
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as directory:
        store = Store(str(Path(directory) / 'bot.db'))
        for number in range(16):
            store.add_message('g:42', 20, 'sample', 'in', 'hello', '', number + 1, [])
            store.add_message('g:42', 20, 'sample', 'out', '', '', number + 100,
                              [{'type': 'sticker', 'file': 'fixture.gif'}])

        def delayed_filter(raw, ids):
            # SQLite calls Python while holding its connection mutex. Releasing
            # the GIL here widens the real startup race without real QQ traffic.
            time.sleep(.0005)
            return attachments_allowed(raw, ids)

        store.conn.create_function('blacklist_attachments_allowed', 2, delayed_filter)
        bot = SimpleNamespace(store=store, mem_meta={}, mem_pending={}, mem_last_activity={})
        ready = threading.Barrier(7)
        errors = []

        def run(work):
            try:
                ready.wait(timeout=3)
                for number in range(40):
                    work(number)
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=run, args=(lambda n: MemoryMixin._bootstrap_memory(bot),)),
            threading.Thread(target=run, args=(lambda n: store.memories_missing_vec(20),)),
            threading.Thread(target=run, args=(lambda n: store.add_log('startup-test', str(n)),)),
            threading.Thread(target=run, args=(lambda n: store.sticker_usage_between(0),)),
            threading.Thread(target=run, args=(lambda n: store.sticker_usage_since(0),)),
            threading.Thread(target=run, args=(lambda n: store.prune_memory_scopes(),)),
            threading.Thread(target=run, args=(lambda n: store.sleep_tainted(conv='g:42', source_mid='1', text='hello'),)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]
        assert store.sticker_usage_between(0)[0]['fixture.gif'] == 16
        assert store.sticker_usage_since(0)['fixture.gif'] == 16
        assert 'g:42' in bot.mem_meta
        assert store.conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        store.conn.close()


class SQLiteThreadingTests(unittest.TestCase):
    def test_startup_callbacks_and_maintenance_finish_in_separate_process(self):
        # A deadlocked C extension can hold the GIL, so the timeout must be
        # enforced by another process rather than a Python timer thread.
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2]/'tools/run-runtime-tests.py'), '--sqlite-stress'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_external_shared_connection_operations_hold_store_lock(self):
        violations = []
        for path in BOT.glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                expression = ast.unparse(node.func)
                if not expression.startswith(('self.store.conn.', 'bot.store.conn.')):
                    continue
                owner = expression.split('.conn.', 1)[0]
                current, guarded = node, False
                while current in parents:
                    current = parents[current]
                    if isinstance(current, ast.With):
                        guarded |= any(ast.unparse(item.context_expr) == owner + '._lock'
                                       for item in current.items)
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        break
                if not guarded:
                    violations.append('%s:%d' % (path.name, node.lineno))
        self.assertEqual(violations, [], 'unguarded shared SQLite access')


if __name__ == '__main__':
    if '--stress' in sys.argv:
        startup_stress()
    else:
        unittest.main()
