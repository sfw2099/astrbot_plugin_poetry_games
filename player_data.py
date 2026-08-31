# -*- coding: utf-8 -*-
"""玩家个人数据 + 成就系统。

- 每个参与诗词游戏的玩家在 plugin_data_dir/players/{uid}.json 维护个人数据
- 记录玩家使用过的所有诗句（拆成单句存储，去重计数）
- 记录玩家达成的所有成就及未完成进度
"""

import json
import os
import time

# ============ 成就定义 ============
# 结构: {id: (名称, 描述)}
ACHIEVEMENTS = {
    # 猜诗词 · 参与人数
    "solo_pass": ("单通！", "成功完成一次猜诗词，且本局只有一人参与"),
    "double_pass": ("双人成行", "成功完成一次猜诗词，且本局有且只有两人参与"),
    "triple_pass": ("三人行", "成功完成一次猜诗词，且本局有且只有三人参与"),
    "four_scholar": ("四大才子", "成功完成一次猜诗词，且本局有且只有四人参与"),
    "five_poem": ("我们的诗词!!!!!", "成功完成一次猜诗词，且本局有且只有五人参与"),
    "six_scholar": ("=主", "成功完成一次猜诗词，且本局有且只有六人参与"),
    "seven_sage": ("竹林七贤", "成功完成一次猜诗词，且本局有且只有七人参与"),
    "eight_scholar": ("八大山人?", "成功完成一次猜诗词，且本局有且只有八人参与"),
    # 猜诗词 · 效率
    "minimalist": ("极简主义", "十次以内成功完成一次猜诗词"),
    "first_hit": ("一发入魂", "开局首句（本局第1条猜测）即猜中"),
    "persistent": ("坚持不懈", "单局内个人累计发送 20 句仍未猜中"),
    "closer": ("收尾人", "作为本局最终猜中者（随次数升级：一阶→二阶→三阶→四阶→色彩）"),
    "all_gray": ("一事无成", "猜诗句/对垒中一次猜测的每个字全部为灰色（无一命中）"),
    "all_initials": ("于无声处听惊雷", "一局中累计使用了全部声母并成功结束"),
    "all_finals": ("绕梁余韵", "一局中累计使用了全部韵母并成功结束"),
    "night_group": ("怀民亦未寝", "多人/双人在夜间成功完成一次猜诗句或诗词对垒"),
    "first_hit_char": ("旗开得胜", "开局首句（本局第1条猜测）出现存在的字或完整拼音（声母韵母正确，可错位）"),
    "peach_picker": ("摘桃子", "多人在场时本局仅发送一句即猜中"),
    # 特殊
    "pig": ("🐖", "一局中重复发送同一诗句（每重复一次加一只🐖）"),
    # 诗词对垒
    "duel_speed": ("速通", "十次内猜出对方诗句并获胜"),
    "duel_open": ("开了！", "五次内猜出对方诗句并获胜"),
    "soulmate": ("心有灵犀", "双方所出诗句为同一首诗词内的诗句"),
    "first_mover": ("先手必胜", "作为挑战者在对垒中获胜"),
    "second_mover": ("后发制人", "作为被挑战者在对垒中获胜"),
    "avenger": ("复仇者", "对垒输给某人后，下一局赢回"),
    "too_dark": ("太阴了！", "对垒中双方各累计 20 次猜测仍未分出胜负"),
    # 对垒 · 里程碑
    "duel_win_5": ("常胜将军", "对垒累计获胜 5 场"),
    "duel_win_10": ("百战不殆", "对垒累计获胜 10 场"),
    "duel_streak": ("一破·卧龙出山", "对垒连胜（升级制：双连→三连→四连→五连及更高）"),
    # 猜诗句 · 里程碑
    "guess_win_10": ("神机妙算", "猜诗句累计获胜 10 场"),
    # 个人积累
    "poet_100": ("小诗仙", "个人诗词数据达到 100 句"),
    "poet_1000": ("诗仙", "个人诗词数据达到 1000 句"),
    # 特殊
    "night_owl": ("更深月色半人家", "在 23:00-5:00 期间完成一局诗词游戏"),
    "early_bird": ("雄鸡一唱天下白", "在 5:00-8:00 期间完成一局诗词游戏"),
    "five_word": ("五言专家", "个人诗句库中 5 字单句累计 100 条"),
    "seven_word": ("七言高手", "个人诗句库中 7 字单句累计 100 条"),
    "beloved_verse": ("挚爱诗句", "某诗句使用次数≥50 且为本人使用频率最高的诗句"),
}

# 收尾人等级：按累计次数映射等级名（升序）
CLOSER_LEVELS = [
    (10, "色彩收尾人"),
    (7, "四阶收尾人"),
    (5, "三阶收尾人"),
    (3, "二阶收尾人"),
    (1, "一阶收尾人"),
]


def closer_level_name(progress):
    """根据收尾人累计次数返回当前等级名（含阶位）。"""
    if progress <= 0:
        return "收尾人"
    for thr, name in CLOSER_LEVELS:
        if progress >= thr:
            return name
    return "收尾人"


def duel_streak_name(n):
    """根据对垒连胜数返回成就名。

    1 连：一破·卧龙出山
    2 连：双连·一战成名
    3 连：三连·举世皆惊
    4 连：四连·天下无敌
    5 连及更高：(N)连·诛天灭地
    """
    _CN = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    if n <= 0:
        return "连胜未开始"
    if n == 1:
        return "一破·卧龙出山"
    if n == 2:
        return "双连·一战成名"
    if n == 3:
        return "三连·举世皆惊"
    if n == 4:
        return "四连·天下无敌"
    if n >= 5:
        prefix = _CN[n] if n <= 10 else str(n)
        return f"{prefix}连·诛天灭地"
    return "一破·卧龙出山"


# 旧版收尾人分阶成就 id（用于迁移清空）
_OLD_CLOSER_IDS = ["closer_1", "closer_3", "closer_5", "closer_7", "closer_10"]


class PlayerManager:
    """管理所有玩家的个人数据文件。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.players = {}  # uid -> dict

    def _path(self, uid):
        return os.path.join(self.data_dir, f"{uid}.json")

    def load(self, uid, name=""):
        """加载玩家数据，不存在则创建。返回玩家数据 dict。"""
        uid = str(uid)
        if uid in self.players:
            p = self.players[uid]
            if name and p.get("name") != name:
                p["name"] = name
            if self._migrate(p):
                self.save(uid)
            return p
        path = self._path(uid)
        p = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    p = json.load(f)
            except Exception:
                p = None
        if not p or not isinstance(p, dict):
            now = time.time()
            p = {
                "uid": uid,
                "name": name or f"用户{uid}",
                "first_seen": now,
                "last_active": now,
                "verses": {},       # 单句 -> {first, count}
                "achievements": {},  # 成就id -> {unlocked, time, progress}
                "stats": {
                    "guess_games": 0,
                    "guess_wins": 0,
                    "duel_games": 0,
                    "duel_wins": 0,
                    "total_guesses": 0,
                    "duel_streak": 0,      # 当前连胜
                    "max_duel_streak": 0,  # 历史最高连胜
                },
            }
        self.players[uid] = p
        if self._migrate(p):
            self.save(uid)
        return p

    def _migrate(self, p):
        """迁移旧数据：清空旧版分阶收尾人成就 id，从 0 重计。返回是否有改动。"""
        changed = False
        ach = p.get("achievements")
        if isinstance(ach, dict):
            for old in _OLD_CLOSER_IDS:
                if old in ach:
                    del ach[old]
                    changed = True
        return changed

    def save(self, uid):
        """立即写盘指定玩家。"""
        p = self.players.get(str(uid))
        if not p:
            return
        p["last_active"] = time.time()
        path = self._path(uid)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def record_verse(self, uid, text, name=""):
        """记录玩家使用过的一句诗（拆成单句）。返回新记录的单句数量。"""
        p = self.load(uid, name)
        added = 0
        for clause in _split_single_clauses(text):
            if not clause:
                continue
            if clause in p["verses"]:
                p["verses"][clause]["count"] += 1
            else:
                p["verses"][clause] = {"first": time.time(), "count": 1}
                added += 1
        self.save(uid)
        return added

    def inc_stat(self, uid, key, amount=1, name=""):
        """累加统计字段。"""
        p = self.load(uid, name)
        p["stats"][key] = p["stats"].get(key, 0) + amount
        self.save(uid)

    def verse_count(self, uid):
        """返回玩家诗句条数。"""
        p = self.load(uid)
        return len(p["verses"])

    def check_verse_achievements(self, uid, name=""):
        """检查个人积累/特殊类成就（诗句数、字数、时段）。返回新解锁成就 id 列表。"""
        p = self.load(uid, name)
        verses = p["verses"]
        total = len(verses)
        five = sum(1 for v in verses if len(v) == 5)
        seven = sum(1 for v in verses if len(v) == 7)
        now = time.localtime()
        hour = now.tm_hour
        is_night = hour >= 23 or hour < 5
        is_early = 5 <= hour < 8

        checks = {
            "poet_100": total >= 100,
            "poet_1000": total >= 1000,
            "five_word": five >= 100,
            "seven_word": seven >= 100,
        }
        # 时段成就：仅在完成一局时由外部触发时段标记，这里不做时段检查（避免误判）
        return self._unlock(uid, checks)

    def unlock_achievement(self, uid, ach_id, name=""):
        """直接解锁指定成就。返回是否新解锁。"""
        p = self.load(uid, name)
        if p["achievements"].get(ach_id, {}).get("unlocked"):
            return False
        p["achievements"][ach_id] = {"unlocked": True, "time": time.time()}
        self.save(uid)
        return True

    def set_progress(self, uid, ach_id, progress, name=""):
        """更新成就进度（不锁定）。"""
        p = self.load(uid, name)
        cur = p["achievements"].get(ach_id, {})
        if cur.get("unlocked"):
            return False
        if progress > cur.get("progress", 0):
            p["achievements"][ach_id] = {"unlocked": False, "progress": progress}
            self.save(uid)
        return False

    def _unlock(self, uid, checks, name=""):
        """批量检查成就条件，解锁满足的。返回新解锁列表。"""
        p = self.load(uid, name)
        new = []
        for ach_id, ok in checks.items():
            if not ok:
                continue
            cur = p["achievements"].get(ach_id, {})
            if not cur.get("unlocked"):
                p["achievements"][ach_id] = {"unlocked": True, "time": time.time()}
                new.append(ach_id)
        if new:
            self.save(uid)
        return new

    def check_closer(self, uid, closer_count, name=""):
        """更新收尾人累计次数并检查是否升级。closer_count 为累计收尾次数。
        返回升级后的等级名（若跨入新等级），否则返回 None。"""
        p = self.load(uid, name)
        cur = p["achievements"].get("closer", {})
        old_lv = closer_level_name(cur.get("progress", 0))
        p["achievements"]["closer"] = {
            "unlocked": True,
            "time": cur.get("time", time.time()),
            "progress": closer_count,
        }
        new_lv = closer_level_name(closer_count)
        self.save(uid)
        if new_lv != old_lv:
            return new_lv
        return None

    def check_duel_streak(self, uid, streak, name=""):
        """更新对垒最高连胜并检查是否升级。streak 为当前连胜数。
        只记录历史最高连胜。返回升级后的成就名（若跨入新等级），否则返回 None。"""
        p = self.load(uid, name)
        cur = p["achievements"].get("duel_streak", {})
        old_max = cur.get("progress", 0)
        new_max = max(old_max, streak)
        if new_max <= 0:
            return None
        p["achievements"]["duel_streak"] = {
            "unlocked": True,
            "time": cur.get("time", time.time()),
            "progress": new_max,
        }
        new_lv = duel_streak_name(new_max)
        self.save(uid)
        if new_lv != duel_streak_name(old_max):
            return new_lv
        return None

    def check_beloved_verse(self, uid, name=""):
        """挚爱诗句成就：使用次数≥50 且为本人使用频率最高的诗句。

        动态更新：若最高频诗句变化则更新成就名。返回 (verse, count) 当新解锁或
        名称/次数变化时，否则返回 None。"""
        p = self.load(uid, name)
        verses = p["verses"]
        if not verses:
            return None
        top_verse = max(verses, key=lambda v: verses[v].get("count", 0))
        top_count = verses[top_verse].get("count", 0)
        if top_count < 50:
            return None
        cur = p["achievements"].get("beloved_verse", {})
        if cur.get("unlocked") and cur.get("verse") == top_verse and cur.get("progress") == top_count:
            return None
        p["achievements"]["beloved_verse"] = {
            "unlocked": True,
            "time": cur.get("time", time.time()),
            "progress": top_count,
            "verse": top_verse,
        }
        self.save(uid)
        return (top_verse, top_count)

    def add_pig(self, uid, name=""):
        """🐖 成就：一局中重复诗句计数 +1。返回最新累计数。"""
        p = self.load(uid, name)
        cur = p["achievements"].get("pig", {})
        count = cur.get("progress", 0) + 1
        p["achievements"]["pig"] = {
            "unlocked": True,
            "time": cur.get("time", time.time()),
            "progress": count,
        }
        self.save(uid)
        return count

    def get_achievements(self, uid):
        """返回玩家成就字典（含进度）。"""
        return self.load(uid).get("achievements", {})

    def get_verses(self, uid):
        """返回玩家诗句字典。"""
        return self.load(uid).get("verses", {})


def _split_single_clauses(text):
    """将含标点的诗句拆成单个分句（去标点），两句则拆两个单句。"""
    import re
    clauses = re.split(r'[，。！？、；：\s]+', text or "")
    return [re.sub(r'[^\u4e00-\u9fff]', '', c) for c in clauses if re.sub(r'[^\u4e00-\u9fff]', '', c)]
