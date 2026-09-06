# -*- coding: utf-8 -*-
"""诗词道具系统：道具定义与掉落池。

掉落概率：胜者自身猜测次数 <=5 次 100%，每 +1 次概率 ×0.8（100→80→64→…，取整）。
败者固定 5%。
"""

# 道具定义: {key: (名称, 描述, 使用场景类型)}
# 使用场景: verse=猜诗句, duel=对垒, both=通用, any=均可（按效果实际校验）
ITEMS = {
    "火眼金睛": {"desc": "随机获取当前答案中 1 个字及其位置", "scene": "any"},
    "三仙归洞": {"desc": "随机获取当前答案中 3 个字各自的正确声母/韵母及位置", "scene": "any"},
    "仙人指路": {"desc": "获取当前答案诗句的作者", "scene": "any"},
    "定仙游": {"desc": "猜诗句时将当前题目更换为一句含指定汉字的诗句（例：/诗词道具 定仙游 春）", "scene": "verse"},
    "乐不思蜀": {"desc": "对垒自己回合使用，使对手下一回合跳过", "scene": "duel"},
    "金蝉脱壳": {"desc": "对垒自己回合使用，更换自己出的题", "scene": "duel"},
    "百战不殆": {"desc": "对垒自己回合使用，五个回合内对手猜中不结束比赛", "scene": "duel"},
    "孤注一掷": {"desc": "对垒自己回合使用，追加三次自己回合，三次内未猜出则判负", "scene": "duel"},
    "请君入梦": {"desc": "对垒自己回合使用，对方下两个回合由系统随机代猜", "scene": "duel"},
    "探囊取物": {"desc": "@指定玩家使用，随机获取对方一个道具", "scene": "both"},
}

# 各类型掉落池（D1 默认）
VERSE_DROP_POOL = ["火眼金睛", "三仙归洞", "定仙游", "仙人指路", "探囊取物"]
DUEL_DROP_POOL = ["乐不思蜀", "金蝉脱壳", "孤注一掷", "请君入梦", "仙人指路", "探囊取物"]
# 败者额外可能掉落的道具
LOSER_EXTRA_POOL = ["百战不殆"]


def drop_rate_by_guess(guesses: int) -> int:
    """胜者自身猜测次数 -> 掉落概率(%)。"""
    if guesses <= 5:
        return 100
    p = 100.0
    for _ in range(guesses - 5):
        p *= 0.8
    return max(1, int(p))


def roll_win_item(guesses: int, scene: str):
    """胜者结算：按概率返回掉落道具 key 或 None。scene: verse/duel。"""
    import random
    rate = drop_rate_by_guess(guesses)
    if random.randint(1, 100) > rate:
        return None
    pool = list(DUEL_DROP_POOL if scene == "duel" else VERSE_DROP_POOL)
    return random.choice(pool) if pool else None


def roll_loser_item(scene: str):
    """败者结算：5% 概率，命中时可能掉百战不殆或本池道具。"""
    import random
    if random.randint(1, 100) > 5:
        return None
    pool = list(LOSER_EXTRA_POOL)
    pool += list(DUEL_DROP_POOL if scene == "duel" else VERSE_DROP_POOL)
    return random.choice(pool) if pool else None
