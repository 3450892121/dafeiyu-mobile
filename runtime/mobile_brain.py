import sys
from mobile_policy import initialize, config_for_mobile

initialize()
cfg=config_for_mobile()
import weather
weather.HOME_CITY=str(cfg.get('weather_home_city')or'').strip()
from mobile_asr import install_overrides
install_overrides()
import cover
cover.allowed=lambda *args,**kwargs:False
cover.capability_prompt=lambda *args,**kwargs:'手机版当前不提供原电脑的翻唱服务；普通点歌和歌曲分享仍可用，不要声称正在翻唱。'
import main

if __name__ == '__main__':
    main.Bot().start()
