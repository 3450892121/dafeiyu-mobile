"""Public QQ connection states; never return upstream login URLs or credentials."""
import re

MESSAGES = {
    'unknown': '尚未取得 QQ 连接状态，请在 QQ 登录页查看。',
    'stopped': 'QQ 组件未运行。',
    'waiting_login': 'QQ 尚未完成登录，请在 QQ 登录页查看。',
    'identity_mismatch': '已登录的 QQ 与设置中的机器人小号不一致，已暂停；请自行切换到指定小号后手动启动机器人。',
    'interface_pending': '指定小号已登录，但机器人消息接口尚未接通。',
    'connected': '指定小号已连接，消息接口可用。',
    'paused': '连接已暂停，请检查登录页和日志后手动恢复。',
}


def identity_state(response, expected):
    if not isinstance(response, dict) or response.get('code') != 0:
        return 'unknown'
    info = response.get('data')
    if not isinstance(info, dict):
        return 'unknown'
    if info.get('online') is not True:
        return 'waiting_login'
    uin = str(info.get('uin') or '')
    if not re.fullmatch(r'[1-9][0-9]{4,11}', uin):
        return 'unknown'
    return 'interface_pending' if uin == str(expected) else 'identity_mismatch'


def public_status(state):
    if not isinstance(state, str) or state not in MESSAGES:
        state = 'unknown'
    return {'state': state, 'message': MESSAGES[state]}
