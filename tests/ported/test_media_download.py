"""Media fetches must connect to the already validated IP, not re-resolve DNS."""
import io
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
APP = Path(__file__).resolve().parents[2] / 'runtime' / 'core'
sys.path.insert(0,str(APP))
import flow_media as media


class FakeSocket:
    def __init__(self,created,*args,**kwargs):
        self.peer=None
        self.sent=[]
        self.closed=False
        self.response=None
        created.append(self)
    def settimeout(self,timeout):
        pass
    def setsockopt(self,*args):
        pass
    def connect(self,address):
        self.peer=address
    def sendall(self,data):
        self.sent.append(data)
    def close(self):
        self.closed=True
    def makefile(self,*args,**kwargs):
        if self.response is not None:
            return io.BytesIO(self.response)
        body=b'INTERNAL' if self.peer[0]=='127.0.0.1' else b'PUBLIC'
        return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)


class MediaDownloadTests(unittest.TestCase):
    def setUp(self):
        self.bot=media.MediaMixin()
        self.bot.cfg={'vision_max_bytes':1024}

    def fetch_with_dns_rebinding(self,scheme='http'):
        sockets=[]
        resolutions=[]
        sni=[]
        def resolve(host,port,*args,**kwargs):
            resolutions.append(host)
            # Numeric, already pinned addresses cannot be changed by a DNS response.
            address='1.1.1.1' if host=='1.1.1.1' or resolutions.count(host)==1 else '127.0.0.1'
            return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(address,port))]
        context=SimpleNamespace(wrap_socket=lambda sock,server_hostname:(sni.append(server_hostname) or sock))
        with patch.object(media.socket,'getaddrinfo',side_effect=resolve), \
             patch.object(media.socket,'socket',side_effect=lambda *a,**k:FakeSocket(sockets,*a,**k)), \
             patch('urllib.request.getproxies',return_value={}), \
             patch('ssl.create_default_context',return_value=context):
            result=self.bot._download(scheme+'://rebind.example/image.png?fixture=1')
        return result,sockets,resolutions,sni

    def test_http_dns_rebinding_cannot_reach_a_private_address(self):
        result,sockets,resolutions,_=self.fetch_with_dns_rebinding()
        self.assertEqual(b'PUBLIC',result)
        self.assertEqual([('1.1.1.1',80)],[sock.peer for sock in sockets])
        self.assertEqual(1,resolutions.count('rebind.example'))
        self.assertIn(b'Host: rebind.example',b''.join(sockets[0].sent))

    def test_https_pins_public_ip_but_preserves_original_hostname_for_tls(self):
        result,sockets,resolutions,sni=self.fetch_with_dns_rebinding('https')
        self.assertEqual(b'PUBLIC',result)
        self.assertEqual([('1.1.1.1',443)],[sock.peer for sock in sockets])
        self.assertEqual(['rebind.example'],sni)
        self.assertEqual(1,resolutions.count('rebind.example'))

    def test_any_private_dns_answer_blocks_before_a_socket_is_created(self):
        infos=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',80)),
               (socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
        with patch.object(media.socket,'getaddrinfo',return_value=infos), patch.object(media.socket,'socket') as create:
            self.assertIsNone(self.bot._download('http://mixed.example/media'))
        create.assert_not_called()

    def test_credentialed_or_unsupported_urls_are_rejected_without_network(self):
        for url in ('http://user:password@public.example/image','file:///tmp/fixture',
                    'ftp://public.example/image','http://public.example:bad/image'):
            with self.subTest(url=url),patch.object(media.socket,'getaddrinfo') as resolve:
                self.assertIsNone(self.bot._download(url))
                resolve.assert_not_called()

    def simple_response(self,url,response=None,limit=None):
        sockets=[]
        def create(*args,**kwargs):
            sock=FakeSocket(sockets,*args,**kwargs)
            sock.response=response
            return sock
        def resolve(host,port,*args,**kwargs):
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',port))]
        with patch.object(media.socket,'getaddrinfo',side_effect=resolve), \
             patch.object(media.socket,'socket',side_effect=create):
            result=self.bot._download(url,limit=limit)
        return result,sockets

    def test_redirect_to_internal_service_is_never_followed(self):
        response=b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n'
        result,sockets=self.simple_response('http://public.example/image',response)
        self.assertIsNone(result)
        self.assertEqual([('1.1.1.1',80)],[sock.peer for sock in sockets])
        self.assertTrue(sockets[0].closed)

    def test_response_remains_bounded_and_connection_is_closed(self):
        result,sockets=self.simple_response('http://public.example/image',limit=4)
        self.assertEqual(b'PUBL',result)
        self.assertTrue(sockets[0].closed)

    def test_public_custom_port_keeps_host_and_query_but_not_fragment(self):
        result,sockets=self.simple_response('http://public.example:8080/image?fixture=1#ignored')
        self.assertEqual(b'PUBLIC',result)
        self.assertEqual(('1.1.1.1',8080),sockets[0].peer)
        sent=b''.join(sockets[0].sent)
        self.assertIn(b'GET /image?fixture=1 HTTP/1.1',sent)
        self.assertIn(b'Host: public.example:8080',sent)
        self.assertNotIn(b'ignored',sent)

    def test_empty_dns_answer_does_not_open_a_connection(self):
        with patch.object(media.socket,'getaddrinfo',return_value=[]),patch.object(media.socket,'socket') as create:
            self.assertIsNone(self.bot._download('http://unresolved.example/image'))
        create.assert_not_called()



class ImageReferenceTests(unittest.TestCase):
    """Only fake pixel metadata is used; no large image is ever allocated."""
    DATA=b'\x89PNG\r\n\x1a\nfixture'

    def fake_convert(self,size=(4000,3000),format='PNG',draft_size=None,draft_error=False,bomb=None):
        events=[]
        class BombError(Exception):
            pass
        class BombWarning(Warning):
            pass
        class FakeImage:
            def __init__(self):
                self.dimensions=size
                self.format=format
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            @property
            def size(self):
                events.append(('size',self.dimensions))
                return self.dimensions
            def draft(self,mode,target):
                events.append(('draft',mode,target))
                if draft_error:
                    raise ValueError('fixture draft failure')
                if draft_size:
                    self.dimensions=draft_size
            def convert(self,mode):
                events.append(('convert',mode))
                return self
            def resize(self,dimensions,filter):
                events.append(('resize',dimensions))
                return self
            def save(self,buf,*args,**kwargs):
                events.append(('save',))
                buf.write(b'jpeg-fixture')
        def open_image(stream):
            if bomb=='error':
                raise BombError('fixture')
            if bomb=='warning':
                import warnings
                warnings.warn('fixture',BombWarning)
            return FakeImage()
        image=SimpleNamespace(open=open_image,LANCZOS=1,DecompressionBombError=BombError,
                              DecompressionBombWarning=BombWarning)
        with patch.dict(sys.modules,{'PIL':SimpleNamespace(Image=image)}):
            result=media.image_to_data_url(self.DATA)
        return result,events

    def test_ordinary_12mp_png_checks_size_before_conversion_and_resizes(self):
        result,events=self.fake_convert()
        self.assertTrue(result.startswith('data:image/jpeg;base64,'))
        self.assertEqual(('size',(4000,3000)),events[0])
        self.assertIn(('resize',(1280,960)),events)
        self.assertFalse(any(event[0]=='draft' for event in events))

    def test_16mp_boundary_is_allowed(self):
        result,events=self.fake_convert(size=(4000,4000))
        self.assertTrue(result)
        self.assertIn(('convert','RGB'),events)

    def test_over_limit_png_is_rejected_before_conversion_without_raw_fallback(self):
        result,events=self.fake_convert(size=(8000,8000))
        self.assertEqual('',result)
        self.assertEqual([('size',(8000,8000))],events)

    def test_large_jpeg_drafts_and_rechecks_before_any_conversion(self):
        result,events=self.fake_convert(size=(8000,8000),format='JPEG',draft_size=(2000,2000))
        self.assertTrue(result)
        self.assertEqual([('size',(8000,8000)),('draft','RGB',(1280,1280)),
                          ('size',(2000,2000)),('convert','RGB')],events[:4])

    def test_jpeg_still_over_limit_after_draft_is_rejected(self):
        result,events=self.fake_convert(size=(8000,8000),format='JPEG')
        self.assertEqual('',result)
        self.assertFalse(any(event[0]=='convert' for event in events))

    def test_failed_jpeg_draft_cannot_fall_back_to_original_large_bytes(self):
        result,events=self.fake_convert(size=(8000,8000),format='JPEG',draft_error=True)
        self.assertEqual('',result)
        self.assertFalse(any(event[0]=='convert' for event in events))

    def test_pillow_bomb_error_and_warning_fail_closed(self):
        for bomb in ('error','warning'):
            with self.subTest(bomb=bomb):
                result,events=self.fake_convert(bomb=bomb)
                self.assertEqual('',result)
                self.assertEqual([],events)

    def test_nonpositive_dimensions_are_rejected_before_conversion(self):
        for size in ((0,100),(-1,100),(100,0)):
            with self.subTest(size=size):
                result,events=self.fake_convert(size=size)
                self.assertEqual('',result)
                self.assertFalse(any(event[0]=='convert' for event in events))

    def test_missing_pillow_preserves_recognized_magic_fallback_contract(self):
        import base64
        with patch.dict(sys.modules,{'PIL':None}):
            result=media.image_to_data_url(self.DATA)
            self.assertEqual('data:image/png;base64,'+base64.b64encode(self.DATA).decode(),result)
            self.assertEqual('',media.image_to_data_url(b'not an image'))


if __name__=='__main__':
    unittest.main()
