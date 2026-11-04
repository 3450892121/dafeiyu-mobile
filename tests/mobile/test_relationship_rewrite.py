"""Production guard rewrites retain current-account attitude without private evidence."""
import ast
import os
from pathlib import Path
import tempfile
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import flow_reply
from flow_reply import ReplyMixin
import guard
from store import Store


class RelationshipRewriteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.folder.cleanup)
        self.store = Store(str(Path(self.folder.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.bot = SimpleNamespace(store=self.store, cfg={'owner_qq': 20},
            persona=SimpleNamespace(system_prompt=Mock(return_value='合成当前角色。')),
            pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(
                ok=True, text='先核对一下这件事。'))))
        self.bot._guard_rewrite = MethodType(ReplyMixin._guard_rewrite, self.bot)

    def relation(self, uid, favor, trust):
        self.store.conn.execute('INSERT INTO person_relations(qq,favor,trust,reason,updated_at) '
            'VALUES(?,?,?,?,?)', (uid, favor, trust, '合成关系秘密原因', int(time.time())))
        self.store.conn.commit()

    def test_rewrite_retains_low_relation_and_never_reads_private_evidence(self):
        self.relation(20, 4.1, 10)
        self.store.add_person_memory(20, '合成私聊秘密青色信封', 'fact', 4,
                                     source_conv='p:20', visibility='private')
        before = self.store.get_person_relation(20)
        result = self.bot._guard_rewrite('这件事先核实。（不要分点）',
                                         '泄露回复判断', True, '合成昵称', True, uid=20)
        self.assertEqual(result, '先核对一下这件事。')
        self.bot.pool.chat.assert_called_once()
        system, history, question = self.bot.pool.chat.call_args.args
        self.assertIn('明显疏远', system)
        self.assertIn('明显戒备', system)
        self.assertIn('认真具体的求助仍可认真处理', system)
        self.assertIn('管理员权限独立', system)
        self.assertIn('不把原来的疏远、克制或戒备自动改成热情', system)
        self.assertEqual(history, [])
        self.assertNotIn('合成关系秘密原因', system + question)
        self.assertNotIn('合成私聊秘密青色信封', system + question)
        self.assertEqual(before, self.store.get_person_relation(20))

    def test_rewrite_selects_real_uid_despite_same_nickname_and_keeps_zero(self):
        self.relation(20, 0, 0)
        self.relation(21, 90, 90)
        for uid, closeness, caution in ((20, '明显疏远', '明显戒备'),
                                        (21, '亲近意愿较高', '比较信任'),
                                        (22, '普通相处', '普通信任')):
            with self.subTest(uid=uid):
                self.bot.pool.chat.reset_mock()
                with patch.object(self.store, 'get_person_relation_effective',
                                  wraps=self.store.get_person_relation_effective) as read:
                    self.assertEqual(self.bot._guard_rewrite('先确认事实。', '时间对不上',
                        False, '相同昵称', True, hint='只修正这处时间。', uid=uid), '先核对一下这件事。')
                    read.assert_called_once_with(uid, 90, 180)
                system = self.bot.pool.chat.call_args.args[0]
                self.assertIn(closeness, system)
                self.assertIn(caution, system)
        self.assertEqual((self.store.get_person_relation(20)['favor'],
                          self.store.get_person_relation(20)['trust']), (0, 0))
        self.assertIsNone(self.store.get_person_relation(22))

    def test_legacy_rewrite_call_without_uid_stays_compatible_and_does_not_guess(self):
        with patch.object(self.store, 'get_person_relation_effective') as read:
            self.assertEqual(self.bot._guard_rewrite('先核实。', '时间对不上', False,
                                                    '相同昵称', True), '先核对一下这件事。')
            read.assert_not_called()
        self.assertNotIn('对当前这个人的相处判断', self.bot.pool.chat.call_args.args[0])

    def actual_guard_block(self, uid):
        """Execute the actual bounded guard section, retaining both production call sites."""
        tree = ast.parse(Path(flow_reply.__file__).read_text(encoding='utf-8'))
        reply = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_reply')
        block = next(n for n in reply.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
            and n.test.id == 'reply' and any(isinstance(child, ast.Assign) and
                any(isinstance(target, ast.Name) and target.id == '_chk' for target in child.targets)
                for child in n.body))
        args = ast.arguments(posonlyargs=[], args=[ast.arg(arg='self'), ast.arg(arg='reply')],
                             kwonlyargs=[], kw_defaults=[], defaults=[])
        function = ast.FunctionDef(name='rewrite', args=args, body=[block,
            ast.Return(value=ast.Name(id='reply', ctx=ast.Load()))], decorator_list=[])
        namespace = dict(guard=guard, log=lambda *a: None, is_owner=False,
                         name='合成引用者的昵称', is_group=True, uid=uid)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     '<actual relationship guard rewrite>', 'exec'), namespace)
        return namespace['rewrite']

    def test_both_actual_guard_call_sites_pass_current_author_to_the_rewrite_model(self):
        self.relation(20, 0, 0)
        rewrite = self.actual_guard_block(20)
        for original in ('你好。回复策略：保持简短。', '今天上午已经做过了。'):
            with self.subTest(original=original):
                self.bot.pool.chat.reset_mock()
                issue = lambda text: '合成时间矛盾' if text == '今天上午已经做过了。' else ''
                with patch.object(guard, 'time_issue', side_effect=issue), \
                     patch.object(self.store, 'get_person_relation_effective',
                                  wraps=self.store.get_person_relation_effective) as read:
                    self.assertEqual(rewrite(self.bot, original), '先核对一下这件事。')
                    read.assert_called_once_with(20, 90, 180)
                self.bot.pool.chat.assert_called_once()
                system = self.bot.pool.chat.call_args.args[0]
                self.assertIn('明显疏远', system)
                self.assertIn('明显戒备', system)


if __name__ == '__main__':
    unittest.main()
