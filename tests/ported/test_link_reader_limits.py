"""Small compressed fixtures cannot bypass the webpage byte limit."""
import gzip
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zlib

ROOT=Path(__file__).resolve().parents[1]
APP = Path(__file__).resolve().parents[2] / 'runtime' / 'core'
sys.path.insert(0,str(APP))
import link_reader as reader


class LinkReaderLimitTests(unittest.TestCase):
    def setUp(self):
        self.limit=patch.object(reader,'MAX_BYTES',64)
        self.limit.start()
        self.addCleanup(self.limit.stop)
        reader._CACHE.clear()

    @staticmethod
    def raw_deflate(data):
        encoder=zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return encoder.compress(data)+encoder.flush()

    def test_gzip_is_bounded_after_decompression(self):
        compressed=gzip.compress(b'x'*256)
        self.assertLess(len(compressed),64)
        self.assertIsNone(reader._decompress(compressed,'gzip'))

    def test_both_deflate_formats_are_bounded(self):
        for compressed in (zlib.compress(b'x'*256),self.raw_deflate(b'x'*256)):
            with self.subTest(compressed_bytes=len(compressed)):
                self.assertLess(len(compressed),64)
                self.assertIsNone(reader._decompress(compressed,'deflate'))

    def test_small_supported_compressed_pages_still_decode(self):
        data=b'hello '*6
        for encoding,compressed in (('gzip',gzip.compress(data)),('deflate',zlib.compress(data)),
                                   ('deflate',self.raw_deflate(data))):
            with self.subTest(encoding=encoding):
                self.assertEqual(data,reader._decompress(compressed,encoding))

    def test_invalid_compressed_body_is_not_treated_as_plain_text(self):
        for encoding in ('gzip','deflate'):
            with self.subTest(encoding=encoding):
                self.assertIsNone(reader._decompress(b'not compressed',encoding))

    def test_concatenated_gzip_members_share_the_same_total_limit(self):
        compressed=gzip.compress(b'a'*40)+gzip.compress(b'b'*40)
        self.assertLess(len(compressed),64)
        self.assertIsNone(reader._decompress(compressed,'gzip'))

    def test_fetch_rejects_oversized_expansion_before_html_extraction(self):
        response={'status':200,'headers':{'content-type':'text/plain','content-encoding':'gzip'},
                  'body':gzip.compress(b'x'*256)}
        with patch.object(reader,'_request',return_value=(response,'')), \
             patch.object(reader,'_extract',wraps=reader._extract) as extract:
            result=reader.fetch_page('https://fixture.example/oversized')
        self.assertFalse(result['ok'])
        extract.assert_not_called()

    def test_fetch_accepts_a_normal_small_compressed_page(self):
        response={'status':200,'headers':{'content-type':'text/plain','content-encoding':'gzip'},
                  'body':gzip.compress(b'hello world '*4)}
        with patch.object(reader,'_request',return_value=(response,'')):
            result=reader.fetch_page('https://fixture.example/small')
        self.assertTrue(result['ok'])
        self.assertIn('hello world',result['text'])


if __name__=='__main__':
    unittest.main()
