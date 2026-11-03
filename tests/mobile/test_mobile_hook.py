"""Real loopback HTTP fixtures, synthetic events and no QQ/model access."""
import hashlib
import hmac
import http.client
import json
import threading
import unittest
from unittest.mock import Mock,patch

import main
from mobile_hook import handler_for


class SignedEventTests(unittest.TestCase):
    def setUp(self):
        self.token='fictional-mobile-event-secret'
        self.bot=Mock();self.bot.cfg.get.return_value=self.token
        self.bot.enqueue.return_value=True
        self.patch=patch.object(main.Bot,'instance',self.bot);self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server=main.HookServer(('127.0.0.1',0),handler_for(main))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.event={'post_type':'message','message_type':'private','self_id':190001,'user_id':190002,'message':'手机测试'}
        self.body=json.dumps(self.event,ensure_ascii=False,separators=(',',':')).encode('utf-8')

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def signature(self,body,token=None):
        return 'sha1='+hmac.new((token or self.token).encode(),body,hashlib.sha1).hexdigest()

    def request(self,body,headers=None,chunked=False):
        con=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        h={'Content-Type':'application/json',**(headers or {})}
        data=[body[:11],body[11:]] if chunked else body
        con.request('POST','/',body=data,headers=h,encode_chunked=chunked)
        response=con.getresponse();result=response.status;response.read();con.close()
        return result

    def test_napcat_signed_unicode_event_reaches_queue_once(self):
        self.assertEqual(self.request(self.body,{'X-Signature':self.signature(self.body)}),204)
        self.bot.enqueue.assert_called_once_with(self.event)

    def test_chunked_event_uses_exact_body_bytes_for_signature(self):
        self.assertEqual(self.request(self.body,{'X-Signature':self.signature(self.body)},chunked=True),204)
        self.bot.enqueue.assert_called_once_with(self.event)

    def test_wrong_missing_and_malformed_signatures_never_enqueue(self):
        for value in ('',self.signature(self.body,'wrong-secret'),'sha1=not-a-hash'):
            with self.subTest(value=bool(value)):
                self.assertEqual(self.request(self.body,{'X-Signature':value}),401)
        self.bot.enqueue.assert_not_called()

    def test_tampered_body_is_rejected(self):
        self.assertEqual(self.request(self.body+b' ',{'X-Signature':self.signature(self.body)}),401)
        self.bot.enqueue.assert_not_called()

    def test_existing_bearer_auth_remains_compatible(self):
        self.assertEqual(self.request(self.body,{'Authorization':'Bearer '+self.token}),204)
        self.bot.enqueue.assert_called_once_with(self.event)

    def test_missing_server_token_fails_closed(self):
        self.bot.cfg.get.return_value=''
        self.assertEqual(self.request(self.body,{'X-Signature':self.signature(self.body)}),401)
        self.bot.enqueue.assert_not_called()

    def test_signed_invalid_json_and_nonobject_are_rejected(self):
        for body in (b'{',b'[]',b'null',b'\xff'):
            self.assertEqual(self.request(body,{'X-Signature':self.signature(body)}),400)
        self.bot.enqueue.assert_not_called()

    def test_signature_does_not_bypass_body_limit(self):
        self.assertEqual(self.request(b'',{'X-Signature':self.signature(b''),'Content-Length':str(4*1024*1024+1)}),413)
        self.bot.enqueue.assert_not_called()

    def test_busy_queue_returns_retryable_failure(self):
        self.bot.enqueue.return_value=False
        self.assertEqual(self.request(self.body,{'X-Signature':self.signature(self.body)}),503)


if __name__=='__main__':unittest.main()
