"""Independent instance policy. Set the environment before importing reusable core."""
import json
import os
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent
LAYOUT = json.loads((RUNTIME / 'layout.json').read_text(encoding='utf-8'))


def initialize(home=None):
    if os.name == 'nt' and not (home or os.environ.get('DAFEIYU_HOME')):
        raise ValueError('电脑隔离测试必须显式指定本项目临时目录')
    home = Path(home or os.environ.get('DAFEIYU_HOME') or LAYOUT['home']).resolve()
    os.environ['DAFEIYU_HOME'] = str(home)
    os.environ['DAFEIYU_SHARED_DATA'] = str(home / 'data')
    os.environ['DAFEIYU_SHARED_MODELS'] = str(home / 'data' / 'models.json')
    os.environ['DAFEIYU_BILI_HOME'] = str(home / 'bili')
    os.environ['DFY_MEDIA_CACHE_DB'] = str(home / 'data' / 'media_cache.db')
    os.environ['DAFEIYU_BILI_BIN'] = str(home / 'tools' / 'bin' / ('bili.exe' if os.name == 'nt' else 'bili'))
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.umask(0o077)
    for directory in ('data', 'appassets', 'bili', 'napcat', 'qq-profile', 'data/stickers/images', 'backups'):
        (home / directory).mkdir(parents=True, exist_ok=True)
    import sys
    sys.path.insert(0, str(RUNTIME / 'core'))
    import config
    ports = LAYOUT['ports']
    config.DEFAULT_MODELS = []
    config.DEFAULTS.update({
        'bot_name':'大肥鱼', 'trigger_name':'大肥鱼', 'owner_qq':0, 'owner_name':'',
        'allow_users':[], 'allow_groups':[], 'allow_all_users':False,
        'login_uin':'', 'models':[], 'api_key':'',
        'onebot_api':'http://127.0.0.1:%d' % ports['onebot'],
        'webhook_host':'127.0.0.1', 'webhook_port':ports['webhook'],
        'admin_port':ports['panel'], 'napcat_webui_port':ports['qqWebui'],
        'voice_enabled':False, 'voice_auto_choose':False, 'voice_tts_url':'',
        'voice_ref_path':'', 'voice_ref_text':'', 'cover_groups':[], 'cover_privates':[],
        'voice_instruct':'', 'mobile_autostart':False,
        'mobile_asr_enabled':True, 'mobile_asr_provider':'local',
        'mobile_asr_base_url':'', 'mobile_asr_model':'', 'mobile_asr_model_id':'',
        'local_llm_enabled':False, 'local_llm_autostart':False,
        'local_llm_path':'', 'other_bots':'', 'bot_peers_qq':'',
        'video_browse_bili_owner_uid':0, 'video_browse_bili_self_mid':0,
        'video_browse_enabled':False,'video_browse_like':False,'video_browse_fav':False,
        'video_browse_reply_owner':False,'video_browse_reply_strangers':False,'video_browse_comment_max_per_day':0,
        'qzone_enabled':False,'qzone_mode':'dry','weather_home_city':'',
        'reply_workers':2, 'model_max_concurrent':2, 'memory_workers':1,
    })
    if not getattr(config.Config, '_mobile_policy', False):
        original = config.Config
        fixed = {key: config.DEFAULTS[key] for key in ('onebot_api','webhook_host','webhook_port','admin_port','napcat_webui_port','voice_enabled','voice_tts_url','voice_ref_path','cover_groups','cover_privates')}
        class MobileConfig(original):
            _mobile_policy = True
            def get(self, key, default=None):
                return fixed[key] if key in fixed else super().get(key, default)
            def set(self, key, value):
                if key in fixed and value != fixed[key]:
                    if key.startswith(('voice_','cover_')):raise ValueError('本版未内嵌原电脑的发声与翻唱后端；文字、点歌和声音转写仍可使用')
                    raise ValueError('手机版本机运行接口不能改为外部服务')
                return super().set(key, value)
            def save(self):
                self.data.update(fixed)
                return super().save()
        config.Config = MobileConfig
    persona = home / 'appassets' / 'persona_self.txt'
    if not persona.exists():
        persona.write_bytes((RUNTIME / 'default-persona.txt').read_bytes())
    # No contacts, model keys, cookies, identity relationships or historical state are imported.
    return home


def config_for_mobile():
    from config import Config
    cfg = Config()
    from urllib.parse import urlsplit
    required = 'http://127.0.0.1:%d' % LAYOUT['ports']['onebot']
    if cfg.get('onebot_api') != required:
        cfg.set('onebot_api', required)
    if cfg.get('webhook_port') != LAYOUT['ports']['webhook']:
        cfg.set('webhook_port', LAYOUT['ports']['webhook'])
    if urlsplit(str(cfg.get('onebot_api'))).hostname != '127.0.0.1':
        raise ValueError('QQ 接口必须在本机回环地址')
    return cfg
