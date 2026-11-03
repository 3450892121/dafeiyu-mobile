"""Actual companion delivery with a temporary Store, fake models and fake QQ."""
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import flow_companion
import reply_expression
from onebot import text_seg
from store import Store
from test_search_repair import APPS, SEARCH_DEF, call, load, model_result
import tempfile


class AutonomousExpressionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, output, *, text_ok=True, image_ok=True, research=False):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as folder:
            store = Store(str(Path(folder) / 'bot.db'))
            try:
                store.add_message('g:7', 20, '合成用户', 'in', '合成话题', '', 101)
                payload = json.dumps({'act':'reply','text':output}, ensure_ascii=False)
                answers = ([model_result(calls=[call('合成梗 含义')]), model_result(payload)]
                           if research else [model_result(payload)])
                bot = SimpleNamespace(cfg={'self_growth_enabled':False,'sticker_judge_enabled':True},
                    self_id=99, store=store, stickers=['fixture.gif'],
                    pool=SimpleNamespace(chat=Mock(side_effect=answers)),
                    persona=SimpleNamespace(system_prompt=lambda *a,**k:'合成人设'),
                    _companion_enabled=lambda *a:True, _is_group_speak_quiet=lambda:False,
                    _group_speak_last_from_bot=lambda *a:False, _proactive_now_line=lambda:'今天',
                    _companion_state=SimpleNamespace(snapshot=lambda *a:(3,True,True),
                        eligible=lambda *a:True, sent=Mock()),
                    _tool_defs=lambda *a,**k:[SEARCH_DEF] if research else [],
                    _handle_tool_calls=Mock(return_value='搜索到了合成词的解释'),
                    _json_from_text=json.loads, _conv_lock=lambda *a:contextlib.nullcontext(),
                    _text_segs=lambda text:[text_seg(text)], _send=Mock(return_value=text_ok),
                    send_sticker=Mock(return_value={'file':'fixture.gif','desc':'合成笑脸'} if image_ok else None),
                    _sticker_label=lambda sticker:'[表情] 合成笑脸')
                with patch.object(flow_companion.reply_autonomy,'resting',return_value=False), \
                     patch.dict('sys.modules', {'recent_topic':SimpleNamespace(read=lambda *a:'',schedule=lambda *a:None)}):
                    yield bot
            finally:
                store.conn.close()

    def run_companion(self, bot):
        flow_companion.CompanionMixin._companion_run.__wrapped__(bot,7,3)
        return [r for r in bot.store.thread_rows('g:7', 10) if r['direction']=='out']

    def test_sticker_only_sends_no_text_and_records_only_confirmed_image(self):
        with self.fixture('[表情:开心]') as bot:
            outgoing = self.run_companion(bot)
            bot._send.assert_not_called()
            bot.send_sticker.assert_called_once()
            self.assertEqual(bot.send_sticker.call_args.kwargs['kw'],'开心')
            self.assertEqual(len(outgoing),1)
            self.assertEqual(outgoing[0]['text'],'[表情] 合成笑脸')
            self.assertEqual(outgoing[0]['attachments'][0]['type'],'sticker')
            self.assertNotIn('[表情:开心]',outgoing[0]['text'])

    def test_combined_reply_removes_control_marker_and_sends_text_then_image(self):
        with self.fixture('挺好玩的。[表情:开心]') as bot:
            order=[]
            bot._send.side_effect=lambda *a:order.append('text') or True
            bot.send_sticker.side_effect=lambda *a,**k:order.append('image') or {'file':'fixture.gif'}
            outgoing=self.run_companion(bot)
            self.assertEqual(order,['text','image'])
            self.assertEqual(bot._send.call_args.args[-1],[text_seg('挺好玩的。')])
            self.assertEqual(outgoing[0]['text'],'挺好玩的。')

    def test_failed_image_is_not_recorded_or_retried_and_failed_text_blocks_image(self):
        with self.fixture('[表情:开心]',image_ok=False) as bot:
            self.assertEqual(self.run_companion(bot),[])
            bot.send_sticker.assert_called_once()
            bot._send.assert_not_called()
        with self.fixture('挺好玩的。[表情:开心]',text_ok=False) as bot:
            self.assertEqual(self.run_companion(bot),[])
            bot.send_sticker.assert_not_called()
        with self.fixture('挺好玩的。[表情:开心]',image_ok=False) as bot:
            self.assertEqual(self.run_companion(bot)[0]['text'],'挺好玩的。')
            bot._send.assert_called_once()

    def test_companion_can_search_then_choose_a_sticker_without_text(self):
        with self.fixture('[表情:震惊]',research=True) as bot:
            self.assertEqual(len(self.run_companion(bot)),1)
            bot._handle_tool_calls.assert_called_once()
            bot._send.assert_not_called()
            self.assertIn('搜索到了合成词的解释',bot.pool.chat.call_args.args[0])

    def test_expression_prompt_respects_disabled_and_empty_library_and_is_protocol_only(self):
        bot=SimpleNamespace(cfg={'sticker_judge_enabled':True},stickers=['fixture.gif'])
        self.assertIn('没有必须附带文字',reply_expression.prompt(bot))
        bot.cfg['sticker_judge_enabled']=False
        self.assertEqual(reply_expression.prompt(bot),'')
        bot.cfg['sticker_judge_enabled']=True
        bot.stickers=[]
        self.assertEqual(reply_expression.prompt(bot),'')
        self.assertNotIn('window',load(APPS[0]/'tools_gate.py').LIMITS['web_search'])


if __name__=='__main__':
    unittest.main()
