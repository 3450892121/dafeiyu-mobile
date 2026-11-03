"""HTTP payload/compatibility tests use fake responses and keys; no network or production config."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load_model(relative, name):
    folder = ROOT / relative
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location(name, folder/'model.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


MODULES = (load_model('runtime/core', 'protocol_model_format_test'),
           load_model('runtime/core', 'hybrid_model_format_test'))
FORMAT = {'type':'json_object'}
MESSAGES = [{'role':'system','content':'Return json.'}, {'role':'user','content':'mock'}]
CONF = {'id':'mock','model':'mock','api_key':'unit-test-key','base_url':'https://unit.invalid/v1'}


class Response:
    status = 200
    def __init__(self, content='{"reply_mode":"voice","text":"mock"}', calls=None):
        self.body = json.dumps({'choices':[{'message':{'content':content,'tool_calls':calls or []},'finish_reason':'stop'}]}).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return self.body


def http_error(status, detail):
    return urllib.error.HTTPError('https://unit.invalid/v1/chat/completions',status,'mock',{},io.BytesIO(detail.encode()))


class ResponseFormatTests(unittest.TestCase):
    def request(self, module, responder, response_format=FORMAT, *, effort='', thinking=True, post_flags=None, tools=None):
        bodies = []
        def open_request(request, **kwargs):
            bodies.append(json.loads(request.data))
            return responder(bodies[-1],len(bodies))
        ark = module.Ark(CONF, thinking_enabled=thinking, reasoning_effort=effort)
        with patch.object(module.urllib.request,'urlopen',side_effect=open_request), patch.object(module,'log'):
            if post_flags is None:
                result = ark.chat(MESSAGES, max_tokens=64, response_format=response_format, tools=tools)
            else:
                result = ark._post(MESSAGES,64,.5,10,response_format=response_format,tools=tools,**post_flags)
        return result, bodies

    def test_optional_json_format_is_in_payload_and_old_calls_omit_it(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                result,bodies = self.request(module,lambda *_:Response())
                self.assertTrue(result.ok)
                self.assertEqual(bodies[0]['response_format'],FORMAT)
                self.assertEqual(bodies[0]['messages'],MESSAGES)
                _,plain = self.request(module,lambda *_:Response('plain'),response_format=None)
                self.assertNotIn('response_format',plain[0])

    def test_explicit_unsupported_format_retries_once_without_losing_tools(self):
        tools = [{'type':'function','function':{'name':'mock_tool','parameters':{'type':'object'}}}]
        def respond(body, count):
            if count == 1: raise http_error(400,'Unsupported parameter: response_format')
            return Response()
        for module in MODULES:
            with self.subTest(module=module.__name__):
                result,bodies = self.request(module,respond,tools=tools)
                self.assertTrue(result.ok)
                self.assertEqual(len(bodies),2)
                self.assertIn('response_format',bodies[0])
                self.assertNotIn('response_format',bodies[1])
                for body in bodies:
                    self.assertEqual(body['tools'],tools)
                    self.assertEqual(body['tool_choice'],'auto')

    def test_json_mode_unsupported_alias_is_recognized(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                def respond(body, count):
                    if count == 1: raise http_error(400,'JSON mode is not supported for this model')
                    return Response()
                result,bodies = self.request(module,respond)
                self.assertTrue(result.ok)
                self.assertEqual(len(bodies),2)
                self.assertNotIn('response_format',bodies[1])

    def test_unrelated_errors_do_not_remove_format_or_add_compatibility_retries(self):
        for module in MODULES:
            for status,detail in ((400,'invalid messages'),(400,'response_format schema is invalid'),
                                  (401,'response_format unsupported'),(500,'response_format unsupported')):
                with self.subTest(module=module.__name__,status=status,detail=detail):
                    def respond(*_): raise http_error(status,detail)
                    result,bodies = self.request(module,respond)
                    self.assertFalse(result.ok)
                    self.assertEqual(len(bodies),1)
                    self.assertIn('response_format',bodies[0])

    def test_repeated_format_rejection_has_a_finite_one_retry_limit(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                def respond(*_): raise http_error(400,'response_format unsupported')
                result,bodies = self.request(module,respond)
                self.assertFalse(result.ok)
                self.assertEqual(len(bodies),2)
                self.assertNotIn('response_format',bodies[1])

    def test_rejected_effort_is_actually_absent_on_compatibility_retry(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                def respond(body, count):
                    if 'reasoning_effort' in body: raise http_error(400,'reasoning_effort unsupported')
                    return Response()
                result,bodies = self.request(module,respond,effort='high')
                self.assertTrue(result.ok)
                self.assertEqual(len(bodies),2)
                self.assertEqual(bodies[0]['reasoning_effort'],'high')
                self.assertNotIn('reasoning_effort',bodies[1])
                self.assertIn('response_format',bodies[1])

    def test_thinking_fallback_never_restores_disabled_effort_or_new_thinking_fields(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                def respond(body, count):
                    if count == 1: raise http_error(400,'thinking unsupported')
                    return Response()
                result,bodies = self.request(module,respond,effort='high',thinking=False,
                    post_flags={'disable_thinking':True,'allow_effort':False})
                self.assertTrue(result.ok)
                self.assertEqual(len(bodies),2)
                self.assertIn('thinking',bodies[0])
                for body in bodies: self.assertNotIn('reasoning_effort',body)
                self.assertNotIn('thinking',bodies[1])
                self.assertNotIn('thinking_budget',bodies[1])

    def test_combined_format_and_thinking_downgrade_is_monotonic(self):
        for module in MODULES:
            with self.subTest(module=module.__name__):
                def respond(body, count):
                    if 'response_format' in body: raise http_error(400,'response_format unsupported')
                    if 'thinking' in body: raise http_error(400,'thinking unsupported')
                    self.assertNotIn('reasoning_effort',body)
                    return Response()
                result,bodies = self.request(module,respond,effort='high',thinking=False,
                    post_flags={'disable_thinking':True})
                self.assertTrue(result.ok)
                self.assertEqual(len(bodies),3)
                keys = {'response_format','reasoning_effort','thinking','thinking_budget','enable_thinking','chat_template_kwargs'}
                for earlier,later in zip(bodies,bodies[1:]):
                    self.assertLess(set(later)&keys,set(earlier)&keys)

    def test_tool_calls_remain_valid_with_json_output_enabled(self):
        calls = [{'id':'call_mock','type':'function','function':{'name':'mock_tool','arguments':'{}'}}]
        for module in MODULES:
            with self.subTest(module=module.__name__):
                result,_ = self.request(module,lambda *_:Response('',calls))
                self.assertTrue(result.ok)
                self.assertEqual(result.tool_calls,calls)

    def test_model_pool_propagates_format_through_normal_retry_and_empty_completion_paths(self):
        for module in MODULES:
            for case in ('normal','retry','empty'):
                with self.subTest(module=module.__name__,case=case):
                    pool = module.ModelPool({})
                    final = module.Result(True,'{"reply_mode":"voice","text":"mock"}')
                    first = (module.Result(False,status=429) if case == 'retry' else
                             module.Result(True,'',finish='length') if case == 'empty' else final)
                    chat = Mock(side_effect=[first,final] if case != 'normal' else [final])
                    with patch.object(pool,'_list',return_value=[CONF]), \
                         patch.object(pool,'_thinking_for',return_value=(True,0,'')), \
                         patch.object(pool,'_timeout_for',return_value=10), \
                         patch.object(pool,'_record'), patch.object(pool,'_record_fail'), \
                         patch.object(module,'Ark',return_value=Mock(chat=chat)), \
                         patch.object(module.time,'sleep'), patch.object(module,'log'):
                        self.assertTrue(pool.chat('Return json.',[],'mock',response_format=FORMAT).ok)
                    self.assertEqual(chat.call_count,1 if case == 'normal' else 2)
                    for call in chat.call_args_list: self.assertEqual(call.kwargs['response_format'],FORMAT)


if __name__ == '__main__':
    unittest.main()
