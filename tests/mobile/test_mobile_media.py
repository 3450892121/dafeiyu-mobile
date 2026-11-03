import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import wave

from flow_media import MediaMixin
import flow_media
import mobile_asr
from persona import Persona
from store import Store


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.bot=MediaMixin();self.bot.cfg={}
        self.patched=patch.object(flow_media,'FILES_DIR',str(self.root));self.patched.start();self.addCleanup(self.patched.stop)
    def test_real_word_writer_and_reader_preserve_paragraph_and_table(self):
        path,error=self.bot._tool_make_docx({'title':'本机文档','content':'# 正文\n新的相遇\n|列一|列二|\n|---|---|\n|甲|乙|'})
        self.assertFalse(error);text=self.bot._parse_document('test.docx',Path(path).read_bytes())
        self.assertIn('新的相遇',text);self.assertIn('甲 | 乙',text)
    def test_real_excel_writer_and_reader_preserve_values(self):
        path,error=self.bot._tool_make_xlsx({'title':'记录','headers':['项目','数量'],'rows':[['苹果',3],['鱼',2]]})
        self.assertFalse(error);text=self.bot._parse_document('test.xlsx',Path(path).read_bytes())
        self.assertIn('苹果 | 3',text);self.assertIn('鱼 | 2',text)
    def test_real_powerpoint_writer_and_reader_preserve_slide_content(self):
        path,error=self.bot._tool_make_pptx({'title':'新计划','slides':[{'title':'日程','bullets':['准备','出发']}]})
        self.assertFalse(error);text=self.bot._parse_document('test.pptx',Path(path).read_bytes())
        self.assertIn('新计划',text);self.assertIn('日程',text);self.assertIn('出发',text)
    def test_txt_limits_and_legacy_format_fail_honestly(self):
        self.bot.cfg={'file_parse_chars':200}
        self.assertEqual(len(self.bot._parse_document('x.txt',b'a'*500)),200)
        self.assertIn('读不了',self.bot._parse_document('x.doc',b'not-a-real-old-office-file'))
        self.assertFalse(self.bot._parse_document('corrupt.pdf',b'bad pdf'))
    def test_document_creation_rejects_internal_response_annotations(self):
        path,error=self.bot._tool_make_docx({'title':'文档','content':'（此回复的接话对象：虚构甲(QQ190002)，对应来话#42）'})
        self.assertIsNone(path);self.assertTrue(error)


class AsrTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        lock=json.loads((mobile_asr.RUNTIME/'asr-model.lock.json').read_text())
        self.model=self.root/'asr'/lock['directory'];self.model.mkdir(parents=True)
        self.wav=self.root/'sound.wav'
        with wave.open(str(self.wav),'wb')as audio:audio.setparams((1,2,16000,0,'NONE',''));audio.writeframes(b'\0\0'*16000*51)
    def test_missing_cpu_model_is_not_fabricated_transcription(self):
        self.assertIn('下载',mobile_asr.recognize_local(self.wav,self.root)['error'])
    def test_cpu_recognition_splits_long_audio_and_keeps_timestamps(self):
        (self.model/'model.int8.onnx').write_bytes(b'fixture');(self.model/'tokens.txt').write_text('fixture')
        import sherpa_onnx
        recognizer=Mock();recognizer.create_stream.side_effect=lambda:SimpleNamespace(accept_waveform=Mock(),result=SimpleNamespace(text='<|zh|>识别文字'))
        with patch.object(sherpa_onnx.OfflineRecognizer,'from_sense_voice',return_value=recognizer)as factory:
            result=mobile_asr.recognize_local(self.wav,self.root)
        self.assertEqual([s['start']for s in result['segments']],[0,25,50])
        self.assertNotIn('<|zh|>',result['text']);self.assertEqual(factory.call_args.kwargs['provider'],'cpu')
        self.assertEqual(recognizer.decode_stream.call_count,3)
    def test_cloud_key_never_goes_to_a_different_host(self):
        cfg=Mock();cfg.models.return_value=[{'id':'speech','base_url':'https://speech.example/v1','api_key':'fictional-only'}]
        values={'mobile_asr_model_id':'speech','mobile_asr_base_url':'https://other.example/v1'};cfg.get.side_effect=lambda key,*a:values.get(key)
        with patch.object(mobile_asr.urllib.request,'build_opener')as opener:
            with self.assertRaises(ValueError):mobile_asr.recognize_api(self.wav,cfg)
        opener.assert_not_called()
    def test_transcription_redirect_is_rejected(self):
        with self.assertRaises(ValueError):mobile_asr.NoRedirect().redirect_request(None,None,302,'moved',{},'https://other.example')


class FreshRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(str(self.root/'bot.db'));self.addCleanup(self.store.conn.close)
    def test_private_owner_feelings_are_not_shared_without_new_instance_consent(self):
        text='我有点嫌爸爸今天太唠叨，想自己安静会儿。'
        self.assertEqual(self.store._inner_share(text,'father',101,101),'')
        self.store.set_setting('mobile_owner_public_sharing','1')
        self.assertEqual(self.store._inner_share(text,'father',101,101),text)
        self.store.set_setting('mobile_owner_public_sharing','0')
        self.assertEqual(self.store._inner_share(text,'father',101,101),'')
    def test_new_persona_does_not_claim_the_old_family_relationship(self):
        (self.root/'appassets').mkdir();(self.root/'appassets/persona_self.txt').write_text('新鲸鱼',encoding='utf-8')
        persona=Persona(str(self.root),cfg={'bot_name':'大肥鱼'})
        note=persona.relation_context(True)
        self.assertNotIn('你是他的女儿',note);self.assertIn('本实例管理员',note)

if __name__=='__main__':unittest.main()
