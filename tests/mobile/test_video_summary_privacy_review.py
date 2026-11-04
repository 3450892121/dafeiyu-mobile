"""Exercise actual video summary writers/readers with local fixtures and fake media tools."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import flow_media
from flow_media import MediaMixin
import video_reader


URL = 'https://www.bilibili.com/video/BV1234567890'
PRIVATE_SUMMARY = '视频感想带有私聊中的临时暗号：私聊夹竹桃。'


class VideoSummaryPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.generation = '0'
        self.bot = MediaMixin()
        self.bot.cfg = {}
        self.bot.self_id = 100
        self.bot.asr_sem = threading.Semaphore(1)
        self.bot._send = Mock(return_value=True)
        self.bot._tool_owner_remind = Mock()
        self.bot.store = SimpleNamespace(
            is_blocked=lambda _: False, blacklist_generation=lambda: self.generation,
            transcript=lambda *_: [{'who': '虚构私聊人', 'text': '私聊夹竹桃'}],
            add_message=Mock())
        self.bot.persona = SimpleNamespace(system_prompt=Mock(return_value='fixture persona'))
        self.bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text=PRIVATE_SUMMARY)))
        for target in (
            patch.object(flow_media, 'DATA_DIR', str(self.root)),
            patch.object(flow_media.tools_gate, 'check', return_value=(True, '')),
            patch.object(flow_media.tools_gate, 'note'),
            patch.object(flow_media.time, 'sleep'),
        ):
            target.start()
            self.addCleanup(target.stop)

    def write_private_reaction(self):
        workdir = self.root/'video-work'
        workdir.mkdir()
        with patch.object(flow_media.tempfile, 'mkdtemp', return_value=str(workdir)), \
                patch.object(video_reader, 'download', return_value={'path': 'unused-fixture.mp4', 'title': '公开片段'}), \
                patch.object(video_reader, 'probe', return_value={'has_video': True, 'has_audio': True, 'duration': 20}), \
                patch.object(video_reader, 'extract_audio', return_value=True), \
                patch.object(video_reader, 'transcribe_srt', return_value={'text': '公开视频原音', 'segments': []}), \
                patch.object(video_reader, 'extract_frames', return_value=[]):
            self.bot._video_job(URL, 'p:101', False, 0, 101)
        self.assertIn('私聊夹竹桃', self.bot.pool.chat.call_args.args[2])
        cache = json.loads((self.root/'video_cache.json').read_text(encoding='utf-8'))
        self.assertEqual(len(cache), 1)
        self.assertTrue(next(iter(cache)).startswith('scoped-v1:'))
        self.assertNotIn(video_reader.cache_key(URL), cache)
        self.bot._video_busy = True  # A miss must not create a real worker in this fixture.

    def read(self, conv, uid, group=False):
        return self.bot._exec_extra_tool('watch_video', {'url': URL}, conv, group,
                                        202 if group else 0, uid, False, '虚构来话人', videos=[URL])

    def test_private_summary_is_reusable_only_by_its_original_conversation_and_requester(self):
        self.write_private_reaction()
        self.assertIn('私聊夹竹桃', self.read('p:101', 101))
        self.assertNotIn('私聊夹竹桃', self.read('g:202', 101, True))
        self.assertNotIn('私聊夹竹桃', self.read('p:303', 303))
        self.assertNotIn('私聊夹竹桃', self.read('p:101', 303))

    def test_legacy_unscoped_summaries_are_ignored_in_private_and_group_chats(self):
        video_reader.cache_put(str(self.root/'video_cache.json'), video_reader.cache_key(URL),
                               {'summary': PRIVATE_SUMMARY})
        self.bot._video_busy = True
        for conv, uid, group in (('p:101', 101, False), ('g:202', 101, True)):
            with self.subTest(conv=conv):
                self.assertNotIn('私聊夹竹桃', self.read(conv, uid, group))

    def test_blacklist_generation_change_invalidates_prior_conversation_reactions(self):
        self.write_private_reaction()
        self.assertIn('私聊夹竹桃', self.read('p:101', 101))
        self.generation = '1'
        self.assertNotIn('私聊夹竹桃', self.read('p:101', 101))

    def test_policy_changes_while_generating_do_not_promote_old_material_to_the_new_cache_scope(self):
        def change_policy(*args, **kwargs):
            self.generation = '1'
            return SimpleNamespace(ok=True, text=PRIVATE_SUMMARY)

        self.bot.pool.chat.side_effect = change_policy
        self.write_private_reaction()
        self.assertNotIn('私聊夹竹桃', self.read('p:101', 101))

    def test_failures_describe_supported_links_without_claiming_uploaded_video_support(self):
        for error in (video_reader.VideoError('站点未授权'), RuntimeError('fixture')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(flow_media.tempfile, 'mkdtemp', return_value=str(self.root/'unused-work')), \
                    patch.object(video_reader, 'download', side_effect=error):
                self.bot._send.reset_mock()
                self.bot._video_job(URL, 'p:101', False, 0, 101)
                notice = self.bot._send.call_args.args[-1][0]['data']['text']
                self.assertIn('视频链接', notice)
                self.assertNotIn('视频文件', notice)
                self.assertNotIn('直接看', notice)


if __name__ == '__main__':
    unittest.main()
