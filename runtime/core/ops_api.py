"""Mobile-only operations; persona editing reuses the reviewed local implementation."""
import os
import sqlite3
import time
from ops_base import OpsApi as BaseOps, _port_open, _write_json


class OpsApi(BaseOps):
    def bots(self):
        self.registry_error = ''
        return [{
            'id':'mobile', 'name':self.cfg.get('bot_name') or '机器人',
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
        bot = self.bots()[0]
        checks={}
        for name in ('bot.db','config.json'):
            checks[name]=os.path.isfile(os.path.join(self.shared,name))
        try:
            connection=sqlite3.connect('file:'+os.path.join(self.shared,'bot.db').replace('\\','/')+'?mode=ro',uri=True)
            try: checks['database']=connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            finally: connection.close()
        except Exception: checks['database']=False
        checks['panel']=_port_open(int(self.cfg.get('admin_port')))
        checks.update(self._live_checks(bot))
        passed=all(checks.values())
        result={'ts':int(time.time()),'overall':'ok' if passed else 'warn',
                'bots':[{'id':bot['id'],'name':bot['name'],'ok':passed,'checks':checks,
                         'problems':[name for name,value in checks.items() if not value]}]}
        _write_json(self.health_path,result,mode=0o600)
        return {'ok':True,'passed':passed,'ts':result['ts'],'checks':checks,
                'scope':'mobile-only','result':result,
                'note':'仅检查所列本机文件、数据库与端口；不证明 QQ 登录、收发或后台运行通过。'}

    def mail(self):
        return {'supported':False,'enabled':False,'ready':False,'to':'',
                'hint':'本页体检不发送或测试邮件。空间后台异常邮件由设置中的空间告警单独配置。'}

    def bot_add(self,*args,**kwargs):
        return {'ok':False,'error':'手机版只管理当前新实例'}

    def bot_remove(self,*args,**kwargs):
        return {'ok':False,'error':'本实例的删除请通过安卓卸载操作'}

    def mail_test(self):
        return {'ok':False,'error':'本页不提供运维测试邮件；空间后台异常告警请在空间设置中单独配置。'}
