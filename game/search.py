# -*- coding: utf-8 -*-
"""诗句检索：先按字面/字数粗筛，再按拼音（声母/韵母）与位置精筛。

支持自然语言检索描述（parse_search_query），让 LLM 灵活表达：
- "第3个字是烟"  -> 位置字约束
- "第5个字声母j" -> 位置声母约束
- "包含明月"       -> 包含字
- "不含风雨"       -> 排除字
- "7字" / "7个字"  -> 字数
- "声母ch 韵母en"  -> 全局声韵约束
"""

import re

from .guess_verse import decompose_text, extract_hanzi


def _pinyin_profile(text):
    """返回诗句拼音特征：{声母集合, 韵母集合}。"""
    hanzi = extract_hanzi(text)
    initials = set()
    finals = set()
    for part in decompose_text(hanzi):
        if part.get("initial"):
            initials.add(part["initial"])
        if part.get("final"):
            finals.add(part["final"])
    return initials, finals


def _match_positions(hanzi, char_positions, initial_positions, final_positions):
    """判断诗句是否满足位置约束。position 为第几个字（1-based）。"""
    parts = None
    if char_positions:
        for p in char_positions:
            pos = int(p.get("position", 0))
            ch = p.get("char", "")
            if not ch:
                continue
            idx = pos - 1
            if 0 <= idx < len(hanzi):
                if hanzi[idx] != ch:
                    return False
            elif ch not in hanzi:
                return False
    if initial_positions or final_positions:
        parts = decompose_text(hanzi)
    if initial_positions:
        for p in initial_positions:
            pos = int(p.get("position", 0))
            init = p.get("initial", "").lower()
            if not init:
                continue
            idx = pos - 1
            if 0 <= idx < len(parts) and parts[idx].get("initial", "") != init:
                return False
    if final_positions:
        for p in final_positions:
            pos = int(p.get("position", 0))
            final = p.get("final", "").lower()
            if not final:
                continue
            idx = pos - 1
            if 0 <= idx < len(parts) and parts[idx].get("final", "") != final:
                return False
    return True


def parse_search_query(query):
    """解析自然语言检索描述，返回结构化条件 dict。

    返回键：include, exclude, length, initials, finals,
           char_positions, initial_positions, final_positions
    """
    q = (query or "").strip()
    result = {
        "include": [], "exclude": [], "length": None,
        "initials": [], "finals": [],
        "char_positions": [], "initial_positions": [], "final_positions": [],
    }
    if not q:
        return result

    # 字数：N字 / N个字（排除「第N个字」的干扰，用负向后顾）
    m = re.search(r'(?<![第\s])(\d+)\s*个字', q)
    if m:
        result["length"] = int(m.group(1))
    else:
        m = re.search(r'(?<![第\s])(\d+)\s*字(?![个字])', q)
        if m:
            result["length"] = int(m.group(1))

    # 位置声母：第N个字声母X / 第N字声母是X
    for m in re.finditer(r'第\s*(\d+)\s*个?\s*字\s*(?:的)?声母\s*(?:是|为|＝|:)?\s*([a-zA-ZüÜ]+)', q):
        result["initial_positions"].append({"position": int(m.group(1)), "initial": m.group(2).lower()})

    # 位置韵母：第N个字韵母X
    for m in re.finditer(r'第\s*(\d+)\s*个?\s*字\s*(?:的)?韵母\s*(?:是|为|＝|:)?\s*([a-zA-ZüÜ]+)', q):
        result["final_positions"].append({"position": int(m.group(1)), "final": m.group(2).lower()})

    # 位置字：第N个字是X / 第N字X / 第N个字为X（排除「声母/韵母」紧跟的情况）
    for m in re.finditer(r'第\s*(\d+)\s*个?\s*字\s*(?!声母|韵母)(?:是|为|＝|:)?\s*([\u4e00-\u9fff])', q):
        result["char_positions"].append({"position": int(m.group(1)), "char": m.group(2)})

    # 全局声母（前面不紧跟「字」的位置描述）：声母X
    for m in re.finditer(r'(?<!字)声母\s*(?:是|为|＝|:)?\s*([a-zA-ZüÜ]+)', q):
        init = m.group(1).lower()
        if init not in result["initials"]:
            result["initials"].append(init)

    # 全局韵母
    for m in re.finditer(r'(?<!字)韵母\s*(?:是|为|＝|:)?\s*([a-zA-ZüÜ]+)', q):
        fin = m.group(1).lower()
        if fin not in result["finals"]:
            result["finals"].append(fin)

    # 包含：包含X / 含有X / 含X（排除「不含」）
    for m in re.finditer(r'(?:包含|含有|(?<!不)含)\s*([\u4e00-\u9fff]+)', q):
        for ch in m.group(1):
            if ch not in result["include"]:
                result["include"].append(ch)

    # 排除：不含X / 排除X / 没有X / 去掉X / 无X
    for m in re.finditer(r'(?:不含|排除|没有|去掉|无)\s*([\u4e00-\u9fff]+)', q):
        for ch in m.group(1):
            if ch not in result["exclude"]:
                result["exclude"].append(ch)

    return result


def search_verses_candidates(db, *, include=None, exclude=None, length=None,
                             initials=None, finals=None,
                             char_positions=None, initial_positions=None,
                             final_positions=None, common_chars=None, limit=20):
    """检索候选诗句。

    include/exclude/initials/finals: 全局约束（AND）。
    char_positions/initial_positions/final_positions: 位置约束。
    common_chars: 常见字集合；无位置约束时，全常见字候选优先返回。
    返回 [{text, title, author, dynasty}]（按纯汉字去重）。
    """
    include = [extract_hanzi(c) for c in (include or [])]
    include = [c for c in include if c]
    exclude = [extract_hanzi(c) for c in (exclude or [])]
    exclude = [c for c in exclude if c]
    initials = [i for i in (initials or []) if i]
    finals = [f for f in (finals or []) if f]
    char_positions = char_positions or []
    initial_positions = initial_positions or []
    final_positions = final_positions or []
    common = set(common_chars) if common_chars else None
    has_position = bool(char_positions or initial_positions or final_positions)

    # ---- 粗筛（SQL 层）----
    # 位置字约束的 char 也加入 include，缩小 SQL 范围
    rough_include = list(include)
    for p in char_positions:
        ch = p.get("char", "")
        if ch and ch not in rough_include:
            rough_include.append(ch)
    raw = []
    if rough_include:
        raw = db.search_by_chars_and_len(rough_include, length, exclude_chars=exclude, limit=800)
    elif length:
        raw = db.get_random_verse(length, length, target_count=120, max_scan=400)
    else:
        raw = db.get_random_verse(4, 7, target_count=120, max_scan=400)

    # ---- 精筛（Python 层）----
    seen = set()
    common_out = []
    out = []
    for verse, title, author, dynasty in raw:
        hanzi = extract_hanzi(verse)
        if not hanzi:
            continue
        if length and len(hanzi) != length:
            continue
        if include and not all(ch in hanzi for ch in include):
            continue
        if exclude and any(ch in hanzi for ch in exclude):
            continue
        if initials or finals:
            inits, fins = _pinyin_profile(hanzi)
            if initials and not all(i in inits for i in initials):
                continue
            if finals and not all(f in fins for f in finals):
                continue
        if has_position and not _match_positions(hanzi, char_positions, initial_positions, final_positions):
            continue
        if hanzi in seen:
            continue
        seen.add(hanzi)
        item = {"text": hanzi, "title": title, "author": author, "dynasty": dynasty}
        # 无位置约束时，全常见字候选优先
        if common and not has_position and set(hanzi) <= common:
            common_out.append(item)
        else:
            out.append(item)
        if len(common_out) + len(out) >= limit * 3:
            break
    result = common_out + out
    return result[:limit]
