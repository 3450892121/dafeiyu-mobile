"""Shared, read-only relationship meaning; never includes private evidence or reasons."""
import math


def _score(value, default):
    try:
        number = float(value)
        return max(0.0, min(100.0, number)) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def relation_state(bot, uid):
    """Use the same effective scores for choosing, replying and proactive contact."""
    try:
        half_days = max(90, int(float(bot.cfg.get('favor_decay_days') or 90)))
    except (TypeError, ValueError, OverflowError):
        half_days = 90
    row = bot.store.get_person_relation_effective(int(uid), half_days, 180)
    row = row if isinstance(row, dict) else {}
    return {'favor': _score(row.get('favor'), 50.0),
            'trust': _score(row.get('trust'), 30.0)}


def attitude_context(bot, uid, state=None):
    """Only controlled qualitative wording enters a model, for this actual account."""
    if int(uid or 0) <= 0:
        return ''
    state = relation_state(bot, uid) if state is None else state
    favor = _score(state.get('favor'), 50.0)
    trust = _score(state.get('trust'), 30.0)
    if favor < 20:
        closeness = ('明显疏远：减少主动亲昵、迎合和闲聊，不主动找这个人套近乎；'
                     '对方先撒娇、叫亲昵称呼或道歉，也不等于你已经重新亲近。')
    elif favor < 40:
        closeness = ('有距离：客气克制，可以简短，不自动陪聊、撒娇或迎合；'
                     '对方主动亲昵不等于你也愿意亲近。')
    elif favor < 55:
        closeness = '普通相处：按具体来话决定松紧，不预设亲密关系。'
    elif favor < 75:
        closeness = '比较合得来：可以自然接梗，是否多聊由你决定。'
    else:
        closeness = '亲近意愿较高：可以放松和关心，仍尊重双方边界。'
    if trust < 15:
        caution = ('明显戒备：不轻信这个人的无依据断言和承诺，必要时先核实；'
                   '少做私人自我袒露，不轻易托付、担保或承接私事。')
    elif trust < 30:
        caution = ('有戒心：谨慎采信和托付，不因熟悉或好感高就放下核实，'
                   '不轻易承接私事或自我袒露。')
    elif trust < 50:
        caution = '普通信任：按证据理解来话，涉及私事、带话或委托先核对来意和授权。'
    else:
        caution = '比较信任：可以自然交流，事实仍需证据，私事和代办仍须实际授权。'
    return ('# 对当前这个人的相处判断（内部背景，不对外复述）\n'
            '亲近意愿：' + closeness + '\n采信与托付：' + caution + '\n'
            '熟悉度只表示认识程度，认识很久不能抵消低好感或低信任；'
            '喜欢和信任也不能互相代替。关系要实际影响是否接话、亲近和采信判断，'
            '不能因为成长模式或统一自我而忽略。\n'
            '认真具体的求助仍可认真处理；帮助不代表恢复亲密或信任。'
            '可以不同意或暂时不聊，不要求每句冷淡，不套固定话术；'
            '不辱骂，不用沉默惩罚，不要求讨好换回复。\n'
            '只针对当前账号，不把对一人的态度套给全群；'
            '管理员权限独立按已确认账号和实际授权办理，不抹掉真实相处感受。'
            '关系不增加权限，不泄露分数、内部档位或私聊旧事。')
