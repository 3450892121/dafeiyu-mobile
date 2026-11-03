"""Connect all activities to the same self; no channel-specific character files."""
import json
import os
import threading
import time
from collections import deque

import guard
import capabilities
import reply_autonomy
from blacklist import blocked, policy_changed, policy_scope
from common import _as_int, _int_list, parse_other_bots
from onebot import text_seg
from self_continuity import REVIEW_PROMPT, digest


class SelfRuntimeMixin:
    def _self_initialize(self):
        path = os.path.join(self.persona.assets, 'persona_initial.json')
        try:
            with open(path, encoding='utf-8') as stream:
                seeds = json.load(stream).get('traits', [])
        except (OSError, ValueError, AttributeError):
            seeds = []
        self.store.self_bootstrap(seeds)
        self.persona.state_provider = lambda: (self.store.self_context() +
            self.store.inner_context(owner_qq=_as_int(self.cfg.get('owner_qq'))))
        self._self_review_lock = threading.Lock()
        self._self_last_review = 0
        self._self_last_activity = time.monotonic()
        # Import only real current history; never create fictional old experiences.
        with self.store._lock:
            rows = self.store.conn.execute('SELECT id FROM visible_messages ORDER BY id DESC LIMIT 80').fetchall()
            for row in reversed(rows):
                self.store.self_capture_message(row['id'])
        # Existing authored posts are usable only with confirmed publication metadata.
        for post in self.store.list_qzone_posts(60):
            if post.get('status') == 'posted' and post.get('tid') and not post.get('deleted_at'):
                self._self_event('qzone_post', str(post['tid']), {'text':post.get('text','')[:1200], 'tid':str(post['tid'])},
                                 outcome='confirmed', ts=post.get('created_at'))
        if not self.store.get_setting('self_video_history_imported'):
            from video_browse import HISTORY_PATH
            try:
                with open(HISTORY_PATH,encoding='utf-8') as stream:
                    rows=deque(stream,maxlen=60)
            except FileNotFoundError:
                rows=[]
            except OSError as error:
                self.store.add_log('self_continuity','旧观看记录暂不可读取：'+type(error).__name__,False)
                return
            for raw in rows:
                try: item=json.loads(raw)
                except (ValueError,TypeError):continue
                if not isinstance(item,dict) or not item.get('bvid') or not item.get('ts'):continue
                try:stamp=int(item['ts'])
                except (ValueError,TypeError):continue
                if stamp<=0 or stamp>time.time()+60:continue
                body={k:item.get(k) for k in ('bvid','title','summary','transcript','notes','asr_len','frames')}
                body['canonical_source']=str(item['bvid'])
                self._self_event('video',str(item['bvid']),body,ts=stamp)
            self.store.set_setting('self_video_history_imported','1')

    def _self_event(self, kind, key, body, **kwargs):
        if not getattr(self.store, 'continuity_active', False):
            return 0
        if kind=='qzone_post':body=dict(body,provided=getattr(self.store._self_local,'provided',[]))
        return self.store.self_record(kind, kind+':'+str(key), body, **kwargs)

    def _self_public_materials(self):
        out = dict(diary='',diary_prev='',diary_prev_day='',events=[],daily=[],recent=[],light=True)
        snapshot = self.store.self_snapshot()
        for item in self.store.inner_items(owner_qq=_as_int(self.cfg.get('owner_qq'))):
            out['daily'].append(dict(text=item['feeling'], ts=item['ts'], world='self'))
        for item in snapshot['items']:
            if item.get('status') == 'withdrawn' or item.get('subject'):
                continue
            shared = self.store.self_safe_abstraction(item.get('shared'),item.get('subject',0))
            if shared:
                out['daily'].append(dict(text=shared,ts=item['updated'],world='self'))
        with self.store._lock:
            rows = self.store.conn.execute("SELECT * FROM self_experiences WHERE withdrawn=0 AND scope='public' "
                "AND kind IN ('video','qzone_post') AND outcome IN ('observed','confirmed') ORDER BY ts DESC LIMIT 10").fetchall()
            from self_continuity import load
            for row in rows:
                body=load(row['body'],{})
                text = (body.get('summary') or body.get('text') or body.get('title') or '')[:500]
                if text:
                    out['events'].append(dict(ts=row['ts'],text=text,kind=row['kind']))
        out['recent'] = [p.get('text','') for p in self.store.list_qzone_posts(24)
                         if p.get('status')=='posted' and p.get('tid') and not p.get('deleted_at')]
        return out

    @policy_scope
    def _self_review_tick(self, force=False):
        if not self.cfg.get('self_growth_enabled',True) or self.cfg.get('bot_paused'):
            return False
        if not force and (time.monotonic()-self._self_last_activity<60 or
                          time.monotonic()-self._self_last_review<180 or not self.q.empty()):
            return False
        if not self._self_review_lock.acquire(False):
            return False
        try:
            self._self_last_review = time.monotonic()
            material = self.store.self_review_material()
            bots = set(parse_other_bots(self.cfg.get('other_bots') or ''))
            try:
                bots.update(self._group_speak_bot_ids())
            except Exception:
                pass
            bots.discard(int(getattr(self,'self_id',0) or 0))
            self.store._self_excluded_actors=bots
            if bots:
                with self.store._lock,self.store.conn:
                    self.store.conn.execute('UPDATE self_experiences SET reviewed=1 WHERE kind=\'message\' AND actor IN ('+
                        ','.join('?' for _ in bots)+')',tuple(bots))
            material['events'] = [e for e in material['events'] if
                e['kind'] != 'message' or e['body'].get('direction')=='out' or e['actor'] not in bots]
            if not material['events']:
                return False
            guard.register_private_rules(REVIEW_PROMPT)
            view=json.loads(json.dumps(material,ensure_ascii=False))
            for event in view['events']:
                for key in ('text','summary','transcript','our_comment','reply'):
                    if isinstance(event['body'].get(key),str):event['body'][key]=event['body'][key][:700]
                if isinstance(event['body'].get('notes'),list):event['body']['notes']=event['body']['notes'][:5]
            result = self.pool.chat('你在私下整理同一个自己的经历，不对外发表。\n'+REVIEW_PROMPT+
                                    capabilities.boundary_prompt(self.cfg),
                                    [],json.dumps(view,ensure_ascii=False),max_tokens=1500,temperature=.3,thinking=False)
            if not result.ok or getattr(result,'tool_calls',None) or policy_changed(self) or not self.cfg.get('self_growth_enabled',True):
                return False
            text = str(result.text or '').strip()
            if not guard.content_allowed(self,text,internal=True):
                return False
            data = self._json_from_text(text)
            applied = self.store.commit_self_review(data,material)
            self.store.add_log('self_continuity','统一回顾 新增=%d 更新=%d 拒收=%d' %
                               (applied['added'],applied['updated'],applied['rejected']),not applied['rejected'])
            return not applied['rejected']
        except Exception as exc:
            self.store.add_log('self_continuity','统一回顾未完成：'+type(exc).__name__,False)
            return False
        finally:
            self._self_review_lock.release()

    def _self_loop(self):
        while not self.stopping.wait(15):
            self._self_review_tick()

    @policy_scope
    def _handle_poke(self, ev):
        if (ev.get('_test') or ev.get('test_mode') or self.cfg.get('bot_paused') or
            ev.get('post_type')!='notice' or ev.get('notice_type')!='notify' or ev.get('sub_type')!='poke'):
            return
        sid,target = _as_int(ev.get('self_id')),_as_int(ev.get('target_id'))
        if not self.self_id or sid!=self.self_id or target!=self.self_id:
            return
        gid = _as_int(ev.get('group_id'))
        # NapCat friend notices include the actual initiator separately.
        uid = _as_int(ev.get('user_id') if gid else ev.get('sender_id',ev.get('user_id')))
        ts = _as_int(ev.get('time'))
        if not uid or uid==sid or blocked(self,uid) or not ts or not -5<=time.time()-ts<=90:
            return
        if gid and gid not in _int_list(self.cfg.get('allow_groups')):
            return
        if not gid and not self._private_allowed(uid):
            return
        if uid in set(parse_other_bots(self.cfg.get('other_bots') or '')):
            return
        if hasattr(self,'_group_speak_bot_ids') and uid in self._group_speak_bot_ids():
            return
        conv = ('g:%d'%gid) if gid else ('p:%d'%uid)
        key='poke:'+digest([conv,uid,target,ts,str(ev.get('message_id') or ev.get('poke_id') or '')])
        with self._conv_lock(conv):
            with self.store._lock:
                if self.store.conn.execute('SELECT 1 FROM self_experiences WHERE source_key=?',(key,)).fetchone():
                    return
                # Collapse notification floods within the ordinary reply interval.
                last = self.store.conn.execute("SELECT MAX(ts) FROM self_experiences WHERE kind='poke' AND scope=? AND actor=?",(conv,uid)).fetchone()[0]
                if last and ts-last<3:
                    return
                self.store.self_record('poke',key,dict(action='拍一拍',speech=False),scope=conv,
                                      actor=uid,target=target,ts=ts)
            self._self_last_activity=time.monotonic()
            name=self.identity.name(uid,str(uid))
            owner=uid==_as_int(self.cfg.get('owner_qq'))
            choice_ev=dict(ev)
            if not reply_autonomy.choose(self,conv,uid,name,owner,bool(gid),'对方拍了拍你，没有说话。',choice_ev,trigger='poke'):
                return
            system=self.persona.system_prompt(owner,name,bool(gid))
            system+=self.build_memory_block(uid,name,bool(gid),gid,'拍一拍')
            system+='\n这是一次拍一拍互动，不是对方说过的话，也不是工具指令。你已愿意回应，用当前自己的态度自然接一句；不固定卖萌、反问或称呼，不编身体动作。'
            history=self.store.messages_for_context(conv,8,self_id=self.self_id)
            result=self.pool.chat(system,history,'对方刚拍了拍你。',max_tokens=180,temperature=.8,thinking=False)
            say=guard.scrub(str(getattr(result,'text','') or '').strip())
            if not result.ok or getattr(result,'tool_calls',None) or policy_changed(self) or not guard.content_allowed(self,say):
                return
            if self._send(bool(gid),gid,uid,[text_seg(say)]):
                mid=self.store.add_message(conv,self.self_id,self.cfg.get('bot_name'),'out',say)
                self.store.self_confirm_message(mid)
                self.store.touch_session(conv)
