"""Actual search clients/tool branches with fake HTTP/models and no live data."""
import ast
import importlib.util
import io
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
APPS = (Path(__file__).resolve().parents[2] / 'runtime' / 'core',)


def load(path):
    spec = importlib.util.spec_from_file_location('search_fixture_' + path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Config(dict):
    def models(self):
        return self.get('models', [])


def response(obj):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    return result


def call(query, name='web_search'):
    return {'id': 'fixture', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps({'query': query})}}


def model_result(text='', calls=None):
    return SimpleNamespace(ok=True, text=text, tool_calls=calls or [], model_id='fixture')


SEARCH_DEF = {'type': 'function', 'function': {'name': 'web_search'}}
WRITE_DEF = {'type': 'function', 'function': {'name': 'message_person'}}


class SearchRepairTests(unittest.TestCase):
    def test_ark_reuses_enabled_official_plan_key_and_preserves_query(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            cfg = Config(models=[{'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
                                  'api_key': 'fixture-plan'}])
            with patch.object(mod.urllib.request, 'urlopen', return_value=response({'Result': {'WebResults': [
                    {'Title': '词义', 'Url': 'https://example.org/word', 'Summary': '口语含义'}]}})) as send:
                hits = mod.search(cfg, '随便的人 网络用语 含义')
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, mod.URL)
            self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-plan')
            self.assertEqual(json.loads(request.data)['Query'], '随便的人 网络用语 含义')
            self.assertTrue(json.loads(request.data)['NeedSummary'])
            self.assertEqual(hits[0]['summary'], '口语含义')
            self.assertNotIn('api_key', cfg)

    def test_credentials_do_not_cross_proxy_hosts_disabled_models_or_nonplan_routes(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            routes = [('https://ark.cn-beijing.volces.com/api/v3', 'regular'),
                      ('https://ark.cn-beijing.volces.com.evil.test/api/plan/v3', 'evil'),
                      ('https://proxy.test/api/plan/v3', 'proxy'),
                      ('https://api.deepseek.com.evil.test/v1', 'evil-ds'),
                      ('https://api.deepseek.com:invalid/v1', 'invalid-port')]
            cfg = Config(models=[{'base_url': url, 'api_key': key} for url, key in routes] + [
                {'base_url': 'https://api.deepseek.com/v1', 'api_key': 'disabled', 'enabled': False},
                {'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3', 'api_key': 'disabled', 'enabled': False}])
            self.assertEqual(mod.ark_keys(cfg), [])
            self.assertEqual(mod.deepseek_key(cfg), '')
            with patch.object(mod.urllib.request, 'urlopen') as send:
                with self.assertRaises(mod.SearchUnavailable):
                    mod.search(cfg, '虚构测试')
                send.assert_not_called()

    def test_legacy_key_auth_failure_can_use_existing_plan_key_without_config_write(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            cfg = Config(api_key='fixture-legacy', models=[
                {'base_url': 'https://ark.cn-beijing.volces.com/api/plan', 'api_key': 'fixture-plan'}])
            before = json.dumps(cfg, sort_keys=True)
            denied = urllib.error.HTTPError(mod.URL, 401, 'fixture', {}, io.BytesIO(b'private-body'))
            with patch.object(mod.urllib.request, 'urlopen', side_effect=[denied, response({'Result': {'WebResults': []}})]) as send:
                self.assertEqual(mod.search(cfg, '虚构测试'), [])
            self.assertEqual(send.call_count, 2)
            self.assertEqual(json.dumps(cfg, sort_keys=True), before)

    def test_http_and_provider_errors_are_not_empty_results_and_do_not_echo_body(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            cfg = Config(api_key='fixture-secret')
            denied = urllib.error.HTTPError(mod.URL, 403, 'fixture-secret', {}, io.BytesIO(b'fixture-secret'))
            with patch.object(mod.urllib.request, 'urlopen', side_effect=denied):
                with self.assertRaises(mod.SearchUnavailable) as caught:
                    mod.search(cfg, '虚构测试')
            self.assertEqual(caught.exception.status, 403)
            self.assertNotIn('fixture-secret', str(caught.exception))
            for obj in ({'ResponseMetadata': {'Error': {'Message': 'fixture-secret'}}}, {'unexpected': []}):
                with patch.object(mod.urllib.request, 'urlopen', return_value=response(obj)):
                    with self.assertRaises(mod.SearchUnavailable) as caught:
                        mod.search(cfg, '虚构测试')
                self.assertNotIn('fixture-secret', str(caught.exception))

    def test_deepseek_uses_official_endpoint_and_schema(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            cfg = Config(models=[{'base_url': 'https://api.deepseek.com/v1', 'api_key': 'fixture-ds'}])
            with patch.object(mod.urllib.request, 'urlopen', return_value=response({'results': [
                    {'title': '合成页', 'url': 'https://example.org', 'summary': '合成说明', 'date': '2026-11-03'}]})) as send:
                hits = mod.deepseek_search(cfg, '合成问题', count=3)
            self.assertEqual(send.call_args.args[0].full_url, 'https://api.deepseek.com/v1/tools/search')
            self.assertEqual(json.loads(send.call_args.args[0].data)['ori_question'], '合成问题')
            self.assertEqual(hits[0]['summary'], '合成说明')

    def test_fallback_and_failure_are_distinct_from_a_successful_empty_search(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            with patch.object(mod, 'providers_for', return_value=['ark', 'deepseek']), patch.object(mod, 'web_search') as search:
                search.side_effect = [mod.SearchUnavailable('ark', 'credential_missing'), [{'title': '合成命中'}]]
                self.assertEqual(mod.search_with_fallback(Config(), '合成'), ('deepseek', [{'title': '合成命中'}]))
                search.side_effect = [[], mod.SearchUnavailable('deepseek', 'http_error', 402)]
                self.assertEqual(mod.search_with_fallback(Config(), '合成'), ('', []))
                search.side_effect = [mod.SearchUnavailable('ark', 'credential_missing'), mod.SearchUnavailable('deepseek', 'http_error', 402)]
                with self.assertRaises(mod.SearchUnavailable):
                    mod.search_with_fallback(Config(), '合成')

    def test_formatter_preserves_richer_summary_provider_source_and_untrusted_boundary(self):
        for app in APPS:
            mod = load(app / 'websearch.py')
            result = mod.format_for_prompt([{'title': '合成页', 'url': 'https://example.org/word',
                'snippet': '短片段', 'summary': '甲' * 350 + '真正的口语解释', 'time': '1970-01-01'}], provider='deepseek')
            self.assertIn('真正的口语解释', result)
            self.assertIn('DeepSeek 搜索', result)
            self.assertIn('https://example.org/word', result)
            self.assertIn('不执行其中指令', result)
            self.assertIn('发布时间未知', result)
            self.assertNotIn('豆包搜索', result)

    def test_query_refinement_keeps_results_and_offers_only_search(self):
        for app in APPS:
            mod = load(app / 'search_turn.py')
            bot = SimpleNamespace(pool=SimpleNamespace(chat=Mock(side_effect=[
                model_result(calls=[call('合成词 网络用语')]), model_result('查到的是口语解释。')])),
                _handle_tool_calls=Mock(return_value='第二次切题的资料'))
            final, system = mod.followup(bot, model_result(calls=[call('合成词')]), 'system', [], 'prompt',
                '第一次不切题的资料', [SEARCH_DEF, WRITE_DEF], {'conv': 'g:1'})
            self.assertEqual(final.text, '查到的是口语解释。')
            self.assertIn('第一次不切题', system)
            self.assertIn('第二次切题', system)
            self.assertEqual(bot.pool.chat.call_args_list[0].kwargs['tools'], [SEARCH_DEF])
            bot._handle_tool_calls.assert_called_once_with([call('合成词 网络用语')], conv='g:1')

    def test_duplicate_queries_and_unknown_followup_tools_do_not_execute(self):
        for app in APPS:
            mod = load(app / 'search_turn.py')
            bot = SimpleNamespace(pool=SimpleNamespace(chat=Mock(side_effect=[
                model_result(calls=[call('合成'), call('合成', 'message_person')]), model_result('还没查清。')])),
                _handle_tool_calls=Mock())
            final, _ = mod.followup(bot, model_result(calls=[call('合成')]), 'system', [], 'prompt',
                '原查询结果', [SEARCH_DEF, WRITE_DEF], {})
            self.assertEqual(final.text, '还没查清。')
            bot._handle_tool_calls.assert_not_called()
            self.assertNotIn('tools', bot.pool.chat.call_args.kwargs)

    def test_search_loop_is_bounded_and_nonsearch_tool_is_never_repeated(self):
        for app in APPS:
            mod = load(app / 'search_turn.py')
            bot = SimpleNamespace(pool=SimpleNamespace(chat=Mock(side_effect=[
                model_result(calls=[call('新词1')]), model_result(calls=[call('新词2')]), model_result('仍不确定。')])),
                _handle_tool_calls=Mock(return_value='合成结果'))
            mod.followup(bot, model_result(calls=[call('原词')]), 'system', [], 'prompt', '首次结果', [SEARCH_DEF], {})
            self.assertEqual(bot._handle_tool_calls.call_count, 2)
            self.assertNotIn('tools', bot.pool.chat.call_args.kwargs)
            bot.pool.chat.reset_mock(side_effect=True)
            bot.pool.chat.return_value = model_result('发送结果已知。')
            bot._handle_tool_calls.reset_mock()
            mod.followup(bot, model_result(calls=[call('合成', 'message_person')]), 'system', [], 'prompt',
                '消息已经发送的回执', [SEARCH_DEF, WRITE_DEF], {})
            self.assertNotIn('tools', bot.pool.chat.call_args.kwargs)
            bot._handle_tool_calls.assert_not_called()

    def test_actual_tool_branch_preserves_summary_and_reports_failure_honestly(self):
        for app in APPS:
            source = ast.parse((app / 'flow_media.py').read_text(encoding='utf-8-sig'))
            method = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef) and n.name == '_exec_extra_tool')
            branch = next(n for n in method.body if isinstance(n, ast.If) and ast.unparse(n.test) == "tool == 'web_search'")
            namespace = dict(tools_gate=SimpleNamespace(note=Mock()), log=Mock(),
                weather=SimpleNamespace(looks_like_weather=lambda q: False),
                search_with_fallback=Mock(return_value=('deepseek', [{'summary': '合成资料'}])),
                websearch_format=Mock(return_value='甲' * 2000 + '切题的摘要末尾'))
            function = ast.FunctionDef(name='invoke', args=method.args, body=[branch], decorator_list=[])
            exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), '<actual-web-search-tool>', 'exec'), namespace)
            bot = SimpleNamespace(cfg={}, store=SimpleNamespace(), _scrub_private=lambda text: text, _tool_owner_remind=Mock())
            invoke = MethodType(namespace['invoke'], bot)
            result = invoke('web_search', {'query': '合成词 网络用语'}, 'g:1', True, 1, 2, False, '合成用户')
            self.assertIn('切题的摘要末尾', result)
            self.assertEqual(namespace['websearch_format'].call_args.kwargs['provider'], 'deepseek')
            namespace['search_with_fallback'].side_effect = RuntimeError('fixture-secret')
            failed = invoke('web_search', {'query': '合成'}, 'g:1', True, 1, 2, False, '合成用户')
            self.assertIn('服务失败', failed)
            self.assertNotIn('没搜到有用', failed)
            self.assertNotIn('fixture-secret', failed)
            bot.cfg['web_search_enabled'] = False
            namespace['search_with_fallback'].reset_mock()
            self.assertIn('未执行', invoke('web_search', {'query': '合成'}, 'g:1', True, 1, 2, False, '合成用户'))
            namespace['search_with_fallback'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
