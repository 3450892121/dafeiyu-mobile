"""Mobile-only operations; persona editing reuses the reviewed local implementation."""
import os
import sqlite3
import time
from ops_base import OpsApi as BaseOps, _port_open


class OpsApi(BaseOps):
    def bots(self):
        self.registry_error = ''
        return [{
            'id':'mobile', 'name':self.cfg.get('bot_name') or '大肥鱼',
            'channel':'napcat','channel_label':'本机 QQ 小号',
            'account':str(self.cfg.get('login_uin') or ''),
            'panel':'http://127.0.0.1:%d' % int(self.cfg.get('admin_port')),
            'persona_dir':os.path.join(os.path.dirname(self.shared),'appassets'),
            'data_dir':self.shared,'service':'',
            'ports':[int(self.cfg.get('webhook_port')),int(self.cfg.get('admin_port'))],
            'health':{'kind':'onebot','port':int(self.cfg.get('napcat_webui_port'))},
        }]

    @staticmethod
    def _unified_persona(bot):
        return True

    def _probe_path(self):
        return ''

    def selftest(self):
        checks={}
        for name in ('bot.db','config.json'):
            checks[name]=os.path.isfile(os.path.join(self.shared,name))
        try:
            connection=sqlite3.connect('file:'+os.path.join(self.shared,'bot.db').replace('\\','/')+'?mode=ro',uri=True)
            try: checks['database']=connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            finally: connection.close()
        except Exception: checks['database']=False
        checks['panel']=_port_open(int(self.cfg.get('admin_port')))
        return {'ok':all(checks.values()),'ts':int(time.time()),'checks':checks,'scope':'mobile-only'}

    def bot_add(self,*args,**kwargs):
        return {'ok':False,'error':'手机版只管理当前新实例'}

    def bot_remove(self,*args,**kwargs):
        return {'ok':False,'error':'本实例的删除请通过安卓卸载操作'}

    def mail_test(self):
        return {'ok':False,'error':'手机版使用本机通知和日志；未接入外部邮件服务'}
