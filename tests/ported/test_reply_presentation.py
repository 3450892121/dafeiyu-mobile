"""QQ presentation: concise ordinary chat, explicit detail and readable line breaks."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import reply_expression as expression


class ReplyPresentationTests(unittest.TestCase):
    def test_qq_markdown_becomes_plain_text_without_flattening_lines_or_losing_sources(self):
        draft = '### 实验板\n- **指示灯**：显示状态。\n- **输入模块**：采集信号。\n[来源](https://example.org/device) [脸:偷笑]'
        actual = expression.qq_text(draft)
        self.assertEqual(actual, '实验板\n• 指示灯：显示状态。\n• 输入模块：采集信号。\n来源（https://example.org/device） [脸:偷笑]')
        self.assertNotIn('**',actual)

    def test_requested_code_is_not_damaged(self):
        code = '```python\nprint("**literal**")\n```'
        self.assertEqual(expression.qq_text(code),code)

    def test_unsolicited_long_image_or_search_explanation_gets_one_bounded_rewrite(self):
        draft = '看起来是教学实验设备。' + '模块的说明很长。' * 45
        short = '看着像教学实验板，主要演示传感器采集和控制。具体型号得看清标签才能确认。'
        bot = SimpleNamespace(pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True,text=short))))
        actual = expression.compact(bot,draft,'这图是什么？',True,'人设与本轮搜索证据',[],'fixture-model')
        self.assertEqual(actual,short)
        bot.pool.chat.assert_called_once()
        self.assertIn('不把推测变成确认',bot.pool.chat.call_args.args[0])
        self.assertIn('本轮搜索证据',bot.pool.chat.call_args.args[0])
        self.assertEqual(bot.pool.chat.call_args.kwargs['timeout_budget'],10)
        self.assertEqual(bot.pool.chat.call_args.kwargs['model_ids'],['fixture-model'])
        self.assertNotIn('tools',bot.pool.chat.call_args.kwargs)

    def test_explicit_detail_math_and_short_or_sticker_reply_do_not_add_a_model_call(self):
        bot=SimpleNamespace(pool=SimpleNamespace(chat=Mock()))
        for question,text in [('详细介绍每个模块','合成说明'*100),('给出完整计算过程','计算步骤'*100),
                              ('现在感觉怎么样','[表情:开心]'),('这是什么','像教学实验板。')]:
            self.assertEqual(expression.compact(bot,text,question,True,'人设',[]),text)
        bot.pool.chat.assert_not_called()

    def test_failed_or_invalid_rewrite_keeps_the_full_answer_instead_of_cutting_a_sentence(self):
        original='看起来是教学实验板。'+'具体型号尚未确定。'*40
        for result in [SimpleNamespace(ok=False,text=''),SimpleNamespace(ok=True,text='{"text":"内部封套"}'),
                       SimpleNamespace(ok=True,text='[表情:开心]'),
                       SimpleNamespace(ok=True,text='误称已查询',tool_calls=[{'function':{'name':'web_search'}}])]:
            bot=SimpleNamespace(pool=SimpleNamespace(chat=Mock(return_value=result)))
            self.assertEqual(expression.compact(bot,original,'这是什么',True,'人设',[]),original)
            bot.pool.chat.assert_called_once()

    def test_actual_group_send_keeps_multiple_lines_in_a_single_bubble(self):
        source=Path(expression.__file__).with_name('flow_reply.py')
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_reply')
        chunk=next(n for n in ast.walk(method) if isinstance(n,ast.Assign) and
                   any(isinstance(t,ast.Name) and t.id=='chunks' for t in n.targets))
        text='结论。\n• 重点一。\n• 重点二。'
        env=dict(reply=text,_image_qa=False,is_group=True,split_chunks=lambda value:[value],
                 split_reply_units=lambda value:value.splitlines())
        exec(compile(ast.Module(body=[chunk],type_ignores=[]),'<actual group chunking>','exec'),env)
        self.assertEqual(env['chunks'],[text])


if __name__=='__main__':unittest.main()
