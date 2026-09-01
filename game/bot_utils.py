# -*- coding: utf-8 -*-
"""AI bot 辅助纯函数：反馈文本化 + 局势文本化（不依赖 astrbot，可独立测试）。"""

from .guess_verse import extract_hanzi

STATUS_CN = {
    "correct": "绿(对)",
    "present": "橙(存在但错位)",
    "absent": "灰(无)",
    "empty": "空",
}


def comp_to_text(guess_text, comp):
    """把一次猜测的 Wordle 反馈结构转成 LLM 可读文本。

    comp: compare_guess 返回的 list，每格 {char, initial, final, tone}。
    """
    hanzi = extract_hanzi(guess_text)
    lines = []
    for i, c in enumerate(comp):
        if c is None:
            lines.append(f"位{i+1}：空")
            continue
        ch = hanzi[i] if i < len(hanzi) else "?"
        lines.append(
            f"位{i+1}「{ch}」字={STATUS_CN.get(c.get('char'), c.get('char'))} "
            f"声母={STATUS_CN.get(c.get('initial'), c.get('initial'))} "
            f"韵母={STATUS_CN.get(c.get('final'), c.get('final'))} "
            f"声调={STATUS_CN.get(c.get('tone'), c.get('tone'))}"
        )
    return "\n".join(lines)


def build_verse_state_text(engine, session_id=""):
    """构建猜诗句局势文本（给 LLM 看）。"""
    lines = []
    target_len = len(getattr(engine, "target_hanzi", ""))
    fmt = engine.format_desc() if hasattr(engine, "format_desc") else f"{target_len} 字"
    lines.append(f"【猜诗句局势】")
    lines.append(f"答案格式：{fmt}")
    lines.append(f"提示方式：{'拼音' if getattr(engine, 'hint_mode', 'pinyin') == 'pinyin' else '部首'}")
    history = getattr(engine, "history", [])
    lines.append(f"已猜 {len(history)} 次")
    for idx, (text, parts, comp) in enumerate(history, 1):
        lines.append(f"--- 第 {idx} 次「{text}」---")
        lines.append(comp_to_text(text, comp))
    return "\n".join(lines)


def build_duel_state_text(engine, side):
    """构建对垒局势文本（给 LLM 看）。side ∈ 'a'|'b'（bot 所在方）。"""
    lines = []
    my_target = engine.a_target_hanzi if side == "a" else engine.b_target_hanzi
    my_punct = engine.a_target_punct if side == "a" else engine.b_target_punct
    lines.append("【诗词对垒局势】")
    lines.append(f"我要猜的目标：{len(my_target)} 字" + ("（两句，需带标点）" if my_punct else "（单句）"))
    if my_punct:
        lines.append(f"目标格式：{engine.format_desc(side)}")
    my_history = engine.a_history if side == "a" else engine.b_history
    lines.append(f"我方已猜 {len(my_history)} 次")
    for idx, (text, parts, comp) in enumerate(my_history, 1):
        lines.append(f"--- 我方第 {idx} 次「{text}」---")
        lines.append(comp_to_text(text, comp))
    opp_side = "b" if side == "a" else "a"
    opp_history = engine.b_history if side == "a" else engine.a_history
    if opp_history:
        lines.append(f"对方已猜 {len(opp_history)} 次（可参考对方的反馈）")
        for idx, (text, parts, comp) in enumerate(opp_history[-3:], 1):
            lines.append(f"--- 对方「{text}」---")
            lines.append(comp_to_text(text, comp))
    lines.append(f"当前轮到：{'我' if engine.is_turn((engine.a_id if side == 'a' else engine.b_id)) else '对方'}")
    return "\n".join(lines)


def parse_guess_from_speech(speech):
    """从 LLM 最终发言中提取猜测诗句。返回 (发言, 猜测) 或 (speech, None)。

    约定格式：最后一行的「猜：诗句」或「猜:诗句」。保留中文标点（两句格式需带标点）。
    """
    if not speech:
        return speech, None
    text = speech.strip()
    m = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        for prefix in ("猜：", "猜:", "答案：", "答案:"):
            if line.startswith(prefix):
                candidate = line[len(prefix):].strip()
                candidate = candidate.strip('「」『』""\'\'`\u3000 ')
                if candidate:
                    m = candidate
                    break
        if m:
            break
    if m:
        # 去掉发言中的猜测行，保留自然发言
        speech_lines = [ln for ln in text.splitlines()
                        if not (ln.strip().startswith(("猜：", "猜:", "答案：", "答案:")))]
        return "\n".join(speech_lines).strip(), m
    return speech, None
