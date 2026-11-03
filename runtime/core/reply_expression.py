"""Dafeiyu chooses text, a sticker, or both using the existing sticker marker."""
from qq_faces import take_sticker_marker
import re


CHAT_PROMPT = (
    '\n\n# QQ聊天的说法\n'
    '先直接回应当前问题，再按需要补一两处关键点，普通聊天通常一到三句就够。'
    '看图和搜索后也先理解、取舍，再用自己的口气说；不要把网页摘要、器件清单或资料逐项搬进聊天。'
    '对方没有要全套说明时，不默认写报告、列一长串或解释每个细节；可以表达自己的感受和不确定。'
    '对方明确要详细、步骤、计算或完整资料时再按要求展开。'
    'QQ不渲染Markdown：不要用双星号加粗、井号标题、Markdown表格；确实需要列点时分成两三条短行，保留换行。'
    '搜索后只交代当前结论和必要依据；引用时保留最相关的一两个来源链接，不复述整段结果。'
)


def qq_text(text):
    """Plain QQ text; preserve meaningful line breaks, URLs, code and face markers."""
    value = str(text or '').strip()
    if '```' in value:
        return value  # A requested code example must remain copyable.
    value = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', r'\1（\2）', value)
    value = re.sub(r'(\*\*|__)(\S(?:.*?\S)?)\1', r'\2', value)
    value = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', value)
    value = re.sub(r'(?m)^\s*[-*+]\s+', '• ', value)
    value = re.sub(r'`([^`\n]+)`', r'\1', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def wants_detail(question):
    return bool(re.search(r'详细|展开|逐[项个条]|每[一]?个|完整|全面|步骤|解题|计算|公式|推导|证明|'
                          r'过程|公差|尺寸|代码|脚本|程序|论文|报告|作文|表格|[0-9]+\s*字', str(question or '')))


def compact(bot, text, question, is_group, system, history, model_id=''):
    """One bounded rewrite of an unsolicited long explanation, never a blind cut."""
    if wants_detail(question) or len(str(text or '')) <= (240 if is_group else 420):
        return text
    target = 160 if is_group else 260
    instruction = (
        '\n\n# 本轮只精简已写好的草稿\n'
        '下面草稿太像资料报告。只保留回答当前问题所需的结论和一两处关键点，'
        '用你自己的口语说，约%d字以内；不逐项介绍器件，不抄网页摘要，不写Markdown。'
        '保留事实里的可能、看起来等不确定性，不新加事实，不把推测变成确认；'
        '有来源时只留最相关的一个链接。只输出最终聊天正文，不输出JSON、工具调用、控制标记或修改说明。'
        % target)
    # The tool evidence remains available, but the rewrite cannot execute tools.
    try:
        import voice
        clean_system = str(system or '').replace(voice.AUTO_CHOICE_PROMPT, '')
        result = bot.pool.chat(clean_system + instruction, history,
            '当前问题：' + str(question or '')[:1200] + '\n待精简草稿：\n' + str(text)[:8000],
            model_ids=[model_id] if model_id else None, thinking=False,
            max_tokens=500, timeout_budget=10, max_attempts=1)
        short = str(getattr(result, 'text', '') or '').strip()
        if (getattr(result, 'ok', False) and not getattr(result, 'tool_calls', None)
                and short and len(short) <= target + 80 and len(short) < len(text)
                and not short.startswith(('{', '```', '[[reply_mode'))
                and not take_sticker_marker(short)[1]):
            return short
    except Exception:
        pass
    return text


def prompt(bot):
    if not bot.cfg.get('sticker_judge_enabled', True) or not getattr(bot, 'stickers', None):
        return ''
    return (
        '\n\n# 本轮表达方式由你自己选\n'
        '可以只说话、只发一张本地表情包，或文字配一张表情包；没有必须附带文字的要求，'
        '也不用为了变化硬凑表情。按自己此刻的意思和情绪选择，不套固定频率。'
        '想只用表情包表达时，整个回复正文只写 [表情:开心] 这样的一个标记，冒号后换成想表达的情绪或意思；'
        '想图文一起就把标记放在正文末尾。不需要口头宣布发图，也不写图片说明或虚构动作。'
        '这是系统支持的发图控制格式，覆盖旧资料里不许写表情标记的说法；程序会取掉标记并实际选图发送。'
        '若本轮使用 reply_mode/text JSON 封套，仍保留封套，把标记写进 text 字段，reply_mode 选 text；'
        '例如 {"reply_mode":"text","text":"[表情:无语]"}。'
        '若在返回 act/text 的接话 JSON，把标记写进 text，act 选 reply。'
        '语音、文字和表情都只是表达选项；按你想说的内容决定，未选择表情时照常说话。'
    )


def split(text):
    return take_sticker_marker(str(text or ''))
