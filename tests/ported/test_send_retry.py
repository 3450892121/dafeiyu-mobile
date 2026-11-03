"""No retry after uncertain delivery; retry only an actual refused connection."""
import ast
import errno
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
APP = Path(__file__).resolve().parents[2] / 'runtime' / 'core'
SPEC=importlib.util.spec_from_file_location('retry_transport_'+ROOT.name,APP/'onebot.py')
transport=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class SendRetryTests(unittest.TestCase):
    def sender(self,outcomes,client=None,receipt_error=None):
        tree=ast.parse((APP/'flow_reply.py').read_text(encoding='utf-8-sig'))
        functions={n.name:n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)}
        name='_send_detail' if '_send_detail' in functions else '_send'
        self.deliveries=Mock(side_effect=receipt_error)
        guard=SimpleNamespace(scrub=lambda text:text,strip_image_notes=lambda text:text,
                              check=lambda text:{'action':'ok'},content_allowed=lambda *args:True)
        namespace={'guard':guard,'log':lambda *args:None,'OneBotError':transport.OneBotError,
                   'blocked':lambda *args:False,'policy_changed':lambda *args:False,
                   'time':SimpleNamespace(sleep=Mock()),'note_delivery':self.deliveries,
                   'is_connection_refused':getattr(transport,'is_connection_refused',lambda error:False)}
        exec(compile(ast.Module(body=[functions[name]],type_ignores=[]),'<actual-final-sender>','exec'),namespace)
        self.send=Mock(side_effect=outcomes)
        bot=SimpleNamespace(_text_segs=lambda text:[{'type':'text','data':{'text':text}}],
                            _take_group_at=lambda *args:False,
                            onebot=client or SimpleNamespace(send_private=self.send,send_group=self.send),
                            store=SimpleNamespace(add_log=Mock()))
        result=namespace[name](bot,False,0,123,[{'type':'text','data':{'text':'离线发送测试'}}])
        return result.get('ok') if isinstance(result,dict) else result,result

    def test_direct_connection_refusal_retries_once_then_records_only_success(self):
        ok,_=self.sender([ConnectionRefusedError(errno.ECONNREFUSED,'fixture'),{'message_id':7}])
        self.assertTrue(ok)
        self.assertEqual(2,self.send.call_count)
        self.deliveries.assert_called_once()

    def test_wrapped_urlerror_refusal_from_real_onebot_client_is_retryable(self):
        response=SimpleNamespace(read=lambda *args:json.dumps({'status':'ok','retcode':0,'data':{'message_id':7}}).encode())
        # Magic methods are looked up on the class, so use a context manager mock.
        from unittest.mock import MagicMock
        context=MagicMock()
        context.__enter__.return_value=response
        error=urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED,'fixture'))
        client=transport.OneBot('http://127.0.0.1:1')
        with patch.object(transport.urllib.request,'urlopen',side_effect=[error,context]) as request:
            ok,_=self.sender([],client)
        self.assertTrue(ok)
        self.assertEqual(2,request.call_count)
        self.deliveries.assert_called_once()

    def test_repeated_refusal_stops_after_two_attempts_without_receipt(self):
        ok,_=self.sender([ConnectionRefusedError(errno.ECONNREFUSED,'fixture')]*2)
        self.assertFalse(ok)
        self.assertEqual(2,self.send.call_count)
        self.deliveries.assert_not_called()

    def test_receipt_bookkeeping_failure_never_replays_a_confirmed_send(self):
        ok,_=self.sender([{'message_id':7},{'message_id':8}],
                         receipt_error=ConnectionRefusedError(errno.ECONNREFUSED,'bookkeeping fixture'))
        self.assertTrue(ok)
        self.assertEqual(1,self.send.call_count)
        self.deliveries.assert_called_once()

    def test_unknown_errors_are_not_retried_even_if_second_attempt_would_work(self):
        for error in (OSError(errno.EIO,'uncertain delivery'),ValueError('malformed response'),
                      transport.OneBotError('bad json'),transport.OneBotError('HTTP 500 upstream failed'),
                      TimeoutError('deadline'),ConnectionResetError('reset')):
            with self.subTest(error=type(error).__name__):
                ok,_=self.sender([error,{'message_id':8}])
                self.assertFalse(ok)
                self.assertEqual(1,self.send.call_count)
                self.deliveries.assert_not_called()

    def test_words_in_protocol_rejection_do_not_make_it_safe_to_retry(self):
        for error in (transport.OneBotError('retcode=1200 ConnectionRefused 10061'),
                      transport.OneBotError('HTTP 500 ConnectionRefused'),
                      RuntimeError('10061 is part of a server response')):
            with self.subTest(error=repr(error)):
                ok,_=self.sender([error,{'message_id':8}])
                self.assertFalse(ok)
                self.assertEqual(1,self.send.call_count)

    def test_classifier_follows_typed_causes_and_handles_cycles(self):
        classify=getattr(transport,'is_connection_refused',None)
        self.assertIsNotNone(classify)
        wrapper=transport.OneBotError('transport failed')
        wrapper.__cause__=urllib.error.URLError(OSError(errno.ECONNREFUSED,'fixture'))
        self.assertTrue(classify(wrapper))
        self.assertTrue(classify(OSError(10061,'fixture')))
        self.assertFalse(classify(RuntimeError('ConnectionRefused 10061')))
        cycle=RuntimeError('cycle')
        cycle.__cause__=cycle
        self.assertFalse(classify(cycle))
        response=urllib.error.HTTPError('http://127.0.0.1:1',500,'fixture',{},None)
        ambiguous=transport.OneBotError('ambiguous chain')
        ambiguous.__cause__=response
        ambiguous.__context__=ConnectionRefusedError(errno.ECONNREFUSED,'earlier refusal')
        self.assertFalse(classify(ambiguous))
        reset=ConnectionResetError(errno.ECONNRESET,'later reset')
        reset.__context__=ConnectionRefusedError(errno.ECONNREFUSED,'earlier refusal')
        self.assertFalse(classify(reset))

    @unittest.skipUnless('_send_detail' in (APP/'flow_reply.py').read_text(encoding='utf-8-sig'),
                         'Partial-delivery metadata belongs to the official bridge')
    def test_partial_delivery_never_retries_even_with_connection_refusal_context(self):
        error=transport.OneBotError('partial',{'delivered_text':'delivered prefix'})
        error.__cause__=ConnectionRefusedError(errno.ECONNREFUSED,'fixture')
        ok,result=self.sender([error,{'message_id':8}])
        self.assertFalse(ok)
        self.assertEqual('delivered prefix',result['delivered_text'])
        self.assertEqual(1,self.send.call_count)


if __name__=='__main__':
    unittest.main()
