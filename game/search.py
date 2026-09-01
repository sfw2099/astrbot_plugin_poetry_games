# -*- coding: utf-8 -*-
"""诗句检索：先按字面/字数粗筛，再按拼音（声母/韵母）精筛。"""

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


def search_verses_candidates(db, *, include=None, exclude=None, length=None,
                             initials=None, finals=None, limit=20):
    """检索候选诗句。

    include:  必须包含的字列表（每个都在句中）
    exclude:  必须排除的字列表
    length:   单句字数
    initials: 声母约束列表（候选句须包含全部这些声母）
    finals:   韵母约束列表（候选句须包含全部这些韵母）
    返回 [{text, title, author, dynasty}]（按纯汉字去重）。
    """
    include = [extract_hanzi(c) for c in (include or [])]
    include = [c for c in include if c]
    exclude = [extract_hanzi(c) for c in (exclude or [])]
    exclude = [c for c in exclude if c]
    initials = [i for i in (initials or []) if i]
    finals = [f for f in (finals or []) if f]

    # ---- 粗筛（SQL 层）----
    raw = []
    if include:
        raw = db.search_by_chars_and_len(include, length, exclude_chars=exclude, limit=500)
    elif length:
        raw = db.get_random_verse(length, length, target_count=60, max_scan=300)
    else:
        raw = db.get_random_verse(4, 7, target_count=60, max_scan=300)

    # ---- 精筛（Python 层）----
    seen = set()
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
        if hanzi in seen:
            continue
        seen.add(hanzi)
        out.append({"text": hanzi, "title": title, "author": author, "dynasty": dynasty})
        if len(out) >= limit:
            break
    return out
