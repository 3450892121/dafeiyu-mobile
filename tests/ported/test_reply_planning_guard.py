import os
import sys
import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.environ.get("BOT_TEST_PATH") or os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core'))
import guard


class ReplyPlanningGuardTests(unittest.TestCase):
    def test_reported_reply_is_blocked(self):
        reply = ('熬到三点还睡得挺美是吧。书还了没？等等，还书这事之前说过了。'
                 '今天是10月31日周六。之前有“还书的日子到了”提醒。'
                 '可以问一句还书弄了没，但别翻旧账追问。嗯，简单问问也行。')
        self.assertEqual(guard.check(reply)["reason"], "泄露回复判断")

    def test_planning_variants_and_tags(self):
        for reply in ("先问问考试的事，但不要反复追问。", "<think>先判断一下</think>你好",
                      '学校在山西那边？ （不用列点，短一点） 其实就一句话，不要追问太多。',
                      '学校在山西那边？其实就一句话，不要追问太多。',
                      '你好。回复策略：保持简短。', '<reasoning>先想想</reasoning>你好',
                      '你好（不要分点，简短回答）'):
            with self.subTest(reply=reply):
                self.assertEqual(guard.check(reply)["reason"], "泄露回复判断")

    def test_normal_conversation_is_not_blocked(self):
        for reply in ("书还了没？", "你不想说就算了，我不追问啦。", "可以问你个问题吗？",
                      "别翻旧账啦，咱们去吃饭。", "这个问题可以先问老师。",
                      "作文不用列点，写成一段就好。", "我就说一句话：晚安。", "不用追问他啦，他不想说。"):
            with self.subTest(reply=reply):
                self.assertEqual(guard.check(reply)["action"], "ok")

    def test_unfinished_tool_result_cannot_leak_payload(self):
        self.assertEqual(guard.scrub('你好。<tool_result>内部检索结果'), '你好。')
        self.assertEqual(guard.scrub('<tool_call>私有参数'), '')

    def test_actual_reply_path_rewrites_leak_once_and_rechecks_result(self):
        app = Path(guard.__file__).parent
        tree = ast.parse((app / 'flow_reply.py').read_text(encoding='utf-8'))
        reply = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_reply')
        block = next(n for n in reply.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                     and n.test.id == 'reply' and any(isinstance(c, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == '_chk' for t in c.targets) for c in n.body))
        args = ast.arguments(posonlyargs=[], args=[ast.arg(arg='self'), ast.arg(arg='reply')],
                             kwonlyargs=[], kw_defaults=[], defaults=[])
        fn = ast.FunctionDef(name='rewrite', args=args, body=[block, ast.Return(value=ast.Name(id='reply', ctx=ast.Load()))], decorator_list=[])
        ns = dict(guard=guard, log=lambda *a:None, is_owner=True, name='测试用户', is_group=False)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), '<actual reply rewrite>', 'exec'), ns)
        leak = '学校在山西那边？ （不用列点，短一点） 其实就一句话，不要追问太多。'
        for rewritten, expected in [('明天就回学校啦，路上慢点。', '明天就回学校啦，路上慢点。'), (leak, None)]:
            b = SimpleNamespace(_guard_rewrite=Mock(return_value=rewritten), store=SimpleNamespace(add_log=Mock()))
            self.assertEqual(ns['rewrite'](b, leak), expected)
            b._guard_rewrite.assert_called_once()

    def test_rewrite_hint_and_recheck(self):
        self.assertIn("直接自然地接话", guard.rewrite_hint("泄露回复判断"))
        self.assertEqual(guard.check("可以问一句，但别追问。")["action"], "drop")
        self.assertEqual(guard.check("昨晚睡得真晚，中午起来还困吗？")["action"], "ok")


if __name__ == "__main__":
    unittest.main()
