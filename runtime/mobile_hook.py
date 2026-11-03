"""Accept NapCat HTTP event signatures without putting credentials in URLs."""
import hashlib
import hmac
import json
import re


def handler_for(core):
    class MobileHookHandler(core.HookHandler):
        def do_POST(self):
            self.connection.settimeout(10)
            instance=core.Bot.instance
            token=instance.cfg.get('onebot_token') if instance else ''
            bearer_ok=bool(token) and super()._authorized()
            signature=self.headers.get('X-Signature','')
            signed=bool(token) and bool(re.fullmatch(r'sha1=[0-9a-fA-F]{40}',signature))
            if not bearer_ok and not signed:
                return self._finish(401)
            try:
                body=self._read_body()
            except core.BodyTooLarge:
                return self._finish(413)
            except (ValueError, OSError):
                return self._finish(400)
            if not bearer_ok:
                expected='sha1='+hmac.new(str(token).encode('utf-8'),body,hashlib.sha1).hexdigest()
                if not hmac.compare_digest(expected,signature.lower()):
                    return self._finish(401)
            if not body:
                return self._finish(204)
            try:
                event=json.loads(body.decode('utf-8'))
                if not isinstance(event,dict):raise ValueError('event must be an object')
            except (UnicodeError,ValueError):
                return self._finish(400)
            accepted=instance.enqueue(event) if instance else False
            self._finish(204 if accepted else 503)

        def _finish(self,status):
            self.send_response(status)
            self.send_header('Content-Length','0')
            self.end_headers()

        def log_message(self,*args):
            # Never echo a URL, event payload, account identifier or credential.
            pass

    return MobileHookHandler
