"""猜诗句 - 参考猜成语的 Wordle 风格诗句猜测游戏引擎。"""

import os
import re
import random
import gzip
import json
from PIL import Image, ImageDraw, ImageFont

# pypinyin 惰性加载
_py_available = False

# 214 个 Kangxi 部首：编号 -> 部首字符
_KANGXI = {
    1: "一", 2: "丨", 3: "丶", 4: "丿", 5: "乙", 6: "亅", 7: "二", 8: "亠",
    9: "人", 10: "儿", 11: "入", 12: "八", 13: "冂", 14: "冖", 15: "冫", 16: "几",
    17: "凵", 18: "刀", 19: "力", 20: "勹", 21: "匕", 22: "匚", 23: "匸", 24: "十",
    25: "卜", 26: "卩", 27: "厂", 28: "厶", 29: "又", 30: "口", 31: "囗", 32: "土",
    33: "士", 34: "夂", 35: "夊", 36: "夕", 37: "大", 38: "女", 39: "子", 40: "宀",
    41: "寸", 42: "小", 43: "尢", 44: "尸", 45: "屮", 46: "山", 47: "巛", 48: "工",
    49: "己", 50: "巾", 51: "干", 52: "幺", 53: "广", 54: "廴", 55: "廾", 56: "弋",
    57: "弓", 58: "彐", 59: "彡", 60: "彳", 61: "心", 62: "戈", 63: "戶", 64: "手",
    65: "支", 66: "攴", 67: "文", 68: "斗", 69: "斤", 70: "方", 71: "无", 72: "日",
    73: "曰", 74: "月", 75: "木", 76: "欠", 77: "止", 78: "歹", 79: "殳", 80: "毋",
    81: "比", 82: "毛", 83: "氏", 84: "气", 85: "水", 86: "火", 87: "爪", 88: "父",
    89: "爻", 90: "爿", 91: "片", 92: "牙", 93: "牛", 94: "犬", 95: "玄", 96: "玉",
    97: "瓜", 98: "瓦", 99: "甘", 100: "生", 101: "用", 102: "田", 103: "疋", 104: "疒",
    105: "癶", 106: "白", 107: "皮", 108: "皿", 109: "目", 110: "矛", 111: "矢", 112: "石",
    113: "示", 114: "禸", 115: "禾", 116: "穴", 117: "立", 118: "竹", 119: "米", 120: "糸",
    121: "缶", 122: "网", 123: "羊", 124: "羽", 125: "老", 126: "而", 127: "耒", 128: "耳",
    129: "聿", 130: "肉", 131: "臣", 132: "自", 133: "至", 134: "臼", 135: "舌", 136: "舛",
    137: "舟", 138: "艮", 139: "色", 140: "艸", 141: "虍", 142: "虫", 143: "血", 144: "行",
    145: "衣", 146: "襾", 147: "見", 148: "角", 149: "言", 150: "谷", 151: "豆", 152: "豕",
    153: "豸", 154: "貝", 155: "赤", 156: "走", 157: "足", 158: "身", 159: "車", 160: "辛",
    161: "辰", 162: "辵", 163: "邑", 164: "酉", 165: "釆", 166: "里", 167: "長", 168: "門",
    169: "阜", 170: "隶", 171: "隹", 172: "雨", 173: "青", 174: "非", 175: "面", 176: "革",
    177: "韋", 178: "韭", 179: "音", 180: "頁", 181: "風", 182: "飛", 183: "食", 184: "首",
    185: "香", 186: "馬", 187: "骨", 188: "高", 189: "髟", 190: "鬥", 191: "鬯", 192: "鬲",
    193: "鬼", 194: "魚", 195: "鳥", 196: "鹵", 197: "鹿", 198: "麥", 199: "麻", 200: "黃",
    201: "黍", 202: "黑", 203: "黹", 204: "黽", 205: "鼎", 206: "鼓", 207: "鼠", 208: "鼻",
    209: "齊", 210: "齒", 211: "龍", 212: "龜", 213: "龠", 214: "龠",
}

# 部首映射（懒加载）：{字符: 部首字符}
_RADICAL_MAP = None
_RADICAL_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radical_map.json.gz")


def _load_radical_map():
    """惰性加载部首映射（gzip 压缩 JSON）。返回 {char: 部首字符}。"""
    global _RADICAL_MAP
    if _RADICAL_MAP is not None:
        return _RADICAL_MAP
    m = {}
    try:
        if os.path.exists(_RADICAL_MAP_PATH):
            with gzip.open(_RADICAL_MAP_PATH, "rt", encoding="utf-8") as f:
                m = json.load(f)
    except Exception:
        m = {}
    _RADICAL_MAP = m
    return m


def radical_of(char):
    """返回单字的主部首字符（Unihan Kangxi 部首）。未命中返回空串。"""
    if not char:
        return ""
    m = _load_radical_map()
    return m.get(char, "")


def radical_status(guess_chars, answer_chars):
    """按宽松存在性规则计算每格的 (char_status, radical_status)。

    char_status: correct(对位) / present(错位存在) / absent(不存在)
    radical_status: correct(部首对位) / present(部首错位存在) / absent(部首不存在)
    """
    n = len(answer_chars)
    ans_rad = [radical_of(c) for c in answer_chars]
    rad_count = {}
    for r in ans_rad:
        if r:
            rad_count[r] = rad_count.get(r, 0) + 1

    char_status = ["absent"] * n
    used_ch = {}
    ch_count = {}
    for c in answer_chars:
        ch_count[c] = ch_count.get(c, 0) + 1
    # 字 pass1 对位
    for i in range(n):
        if i >= len(guess_chars):
            break
        g = guess_chars[i]
        if g == answer_chars[i] and used_ch.get(g, 0) < ch_count.get(g, 0):
            char_status[i] = "correct"
            used_ch[g] = used_ch.get(g, 0) + 1
    # 字 pass2 错位存在
    for i in range(n):
        if char_status[i] != "absent":
            continue
        g = guess_chars[i] if i < len(guess_chars) else None
        if g and ch_count.get(g, 0) > used_ch.get(g, 0):
            char_status[i] = "present"
            used_ch[g] = used_ch.get(g, 0) + 1

    # 部首 pass1 对位
    rad_status = ["absent"] * n
    for i in range(n):
        g = radical_of(guess_chars[i]) if i < len(guess_chars) else ""
        if g and g == ans_rad[i]:
            rad_status[i] = "correct"
    # 部首 pass2 存在性（宽松：答案中出现过即 present）
    for i in range(n):
        if rad_status[i] != "absent":
            continue
        g = radical_of(guess_chars[i]) if i < len(guess_chars) else ""
        if g and rad_count.get(g, 0) > 0:
            rad_status[i] = "present"
    return char_status, rad_status


def _init_pypinyin():
    global _py_available
    if _py_available:
        return True
    try:
        from pypinyin import pinyin, Style
        global _pinyin, _Style
        _pinyin, _Style = pinyin, Style
        _py_available = True
        return True
    except ImportError:
        return False


def _ensure_pypinyin():
    if _init_pypinyin():
        return True
    try:
        import subprocess
        import sys
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pypinyin", "-q",
             "-i", "https://mirrors.aliyun.com/pypi/simple/"]
        )
        return _init_pypinyin()
    except Exception:
        return False


# pypinyin 韵母变体 -> 教学标准形式
_FINAL_ALIAS = {
    "v": "ü", "ve": "üe", "vn": "ün", "van": "üan",   # ü 系列（pypinyin 用 v 表示 ü）
    "iou": "iu", "uei": "ui", "uen": "un",  # 省写还原
}


def _decompose_char(char):
    """分解单字为 (initial 声母, final 韵母, tone 声调)。"""
    if not _ensure_pypinyin():
        return {"char": char, "initial": "", "final": "", "tone": 0, "base": char}
    try:
        py_list = _pinyin(char, style=_Style.TONE2)
        if not py_list or not py_list[0]:
            return {"char": char, "initial": "", "final": "", "tone": 0, "base": char}
        full = py_list[0][0]
        tone = 0
        base = ""
        for ch in full:
            if ch.isdigit():
                tone = int(ch)
            else:
                base += ch

        init_list = _pinyin(char, style=_Style.INITIALS)
        initial = init_list[0][0] if init_list and init_list[0] else ""

        final_list = _pinyin(char, style=_Style.FINALS)
        final = final_list[0][0] if final_list and final_list[0] else ""

        # 韵母归一化：pypinyin 变体 -> 教学标准形式
        final = _FINAL_ALIAS.get(final, final)

        # y/w 零声母处理：按小学拼音教学法，把 y/w 当作声母展示
        # 例：往 wǎng -> 声母 w 韵母 ang（而非空声母 + uang）
        if not initial and base:
            if base.startswith("y"):
                initial = "y"
                rest = base[1:]
                # y + u 实际是 ü（鱼 yu->ü, 月 yue->üe, 云 yun->ün, 元 yuan->üan）
                if rest.startswith("u"):
                    final = "ü" + rest[1:]
                else:
                    final = rest
            elif base.startswith("w"):
                initial = "w"
                final = base[1:]

        return {"char": char, "initial": initial, "final": final, "tone": tone, "base": base}
    except Exception:
        return {"char": char, "initial": "", "final": "", "tone": 0, "base": char}


def decompose_text(text):
    """分解整句为字符部件列表。"""
    return [_decompose_char(ch) for ch in text]


# 中文标点
CN_PUNCT = set("，。！？；：、——…《》「」『』（）“”‘’")


def extract_punct(text):
    """提取句中的标点序列：[(字符, 标点)]，字符下标按汉字序列计。"""
    punct = []
    hanzi_idx = 0
    for ch in text:
        if ch in CN_PUNCT:
            punct.append((hanzi_idx, ch))
        elif '\u4e00' <= ch <= '\u9fff':
            hanzi_idx += 1
    return punct


def extract_hanzi(text):
    """提取句中纯汉字。"""
    return re.sub(r'[^\u4e00-\u9fff]', '', text)


def compare_guess(guess_parts, answer_parts):
    """
    按答案长度比较。返回 (list, bool)。
    - 超出答案长度的猜测字符被忽略
    - 不足答案长度的位置显示为空 (None)
    - 每位置含 char/initial/final/tone 四属性状态
    """
    n = len(answer_parts)
    result = []

    answer_counts = {"char": {}, "initial": {}, "final": {}, "tone": {}}
    for part in answer_parts:
        for key in ("char", "initial", "final", "tone"):
            val = part[key]
            answer_counts[key][val] = answer_counts[key].get(val, 0) + 1

    used = {"char": {}, "initial": {}, "final": {}, "tone": {}}

    # Pass 1: 正确位置 (绿)
    for i in range(n):
        if i >= len(guess_parts):
            result.append(None)
            continue
        status = {"char": "absent", "initial": "absent", "final": "absent", "tone": "absent"}
        for key in ("char", "initial", "final", "tone"):
            g = guess_parts[i][key]
            a = answer_parts[i][key]
            if g == a and used[key].get(g, 0) < answer_counts[key].get(a, 0):
                status[key] = "correct"
                used[key][g] = used[key].get(g, 0) + 1
        result.append(status)

    # Pass 2: 错位存在 (橙)
    for i in range(n):
        if result[i] is None:
            continue
        for key in ("char", "initial", "final", "tone"):
            if result[i][key] != "absent":
                continue
            g = guess_parts[i][key]
            if answer_counts[key].get(g, 0) > used[key].get(g, 0):
                result[i][key] = "present"
                used[key][g] = used[key].get(g, 0) + 1

    return result


def _build_syllable_set(parts):
    """构建目标诗句的音节集合（声母+韵母 组合，忽略声调）。用于下划线提示。"""
    return {(p.get("initial", ""), p.get("final", "")) for p in parts if p.get("initial") and p.get("final")}


def _syllable_underlined(gp, cell, syllable_set):
    """判断某格是否需加下划线：音节(声母+韵母)在原句中出现过，且整字未完全正确（非绿色）。"""
    if gp is None or cell is None:
        return False
    if cell.get("char") == "correct":
        return False
    initial = gp.get("initial", "")
    final = gp.get("final", "")
    if not initial or not final:
        return False
    return (initial, final) in syllable_set


class GuessVerseEngine:
    """猜诗句游戏引擎（单机，无存档）—— 4-7 字单句，可从总库随机出题"""

    def __init__(self, db_source, max_attempts=10, min_len=7, max_len=18, classic_poems=None, hint_mode="pinyin"):
        self.db_source = db_source
        self.max_attempts = max_attempts
        self.min_len = min_len
        self.max_len = max_len
        self.hint_mode = hint_mode
        self.target_text = None       # 完整句（含标点）
        self.target_hanzi = None      # 仅汉字
        self.target_parts = None      # 汉字分解列表
        self.target_punct = []        # 标点序列 [(位置, 标点)]
        self.title = None
        self.author = None
        self.dynasty = None
        self.history = []  # [(guess_text, guess_parts, compare_result)]
        # 经典曲库：list[dict(sentence/hanzi/chars/title/author/dynasty)]
        self.classic_poems = classic_poems or []
        self._classic_sentences = self._build_classic_index()
        # 曲库完整句的「纯汉字」集合（合规校验用，忽略标点）
        self._classic_hanzi_set = {p.get("hanzi", "") for p in self.classic_poems if p.get("hanzi")}
        # 曲库中出现的所有单字集合（作为「常见字」参考，用于总库抽题的候选过滤）
        self._common_chars = set()
        for p in self.classic_poems:
            self._common_chars.update(extract_hanzi(p.get("sentence", "") or ""))
        # 声母/韵母状态记录（用于提示功能，静默累积）
        self.initial_status = {}  # 声母 -> correct/present/absent
        self.final_status = {}    # 韵母 -> correct/present/absent

    def _record_pinyin_status(self, guess_parts, comp):
        """累积记录每次猜测的声母/韵母状态（不主动显示）。"""
        priority = {"correct": 3, "present": 2, "absent": 1}
        for i, cell in enumerate(comp):
            if cell is None or i >= len(guess_parts):
                continue
            gp = guess_parts[i]
            init = gp.get("initial", "")
            if init:
                st = cell.get("initial", "")
                if priority.get(st, 0) > priority.get(self.initial_status.get(init), 0):
                    self.initial_status[init] = st
            final = gp.get("final", "")
            if final:
                st = cell.get("final", "")
                if priority.get(st, 0) > priority.get(self.final_status.get(final), 0):
                    self.final_status[final] = st

    def _build_classic_index(self):
        """构建经典曲库的「完整句 -> 篇目」索引。键为含标点的完整句。"""
        index = {}
        for p in self.classic_poems:
            sent = p.get("sentence", "")
            hanzi = p.get("hanzi", "") or re.sub(r'[^\u4e00-\u9fff]', '', sent)
            if not sent or not (self.min_len <= len(hanzi) <= self.max_len):
                continue
            index.setdefault(sent, p)
        return index

    def _category_weight(self, category):
        """分类抽取权重：热门分类权重大，楚辞/诗经降权。"""
        w = {
            "唐诗三百首": 5,
            "宋词三百首": 5,
            "教材诗词": 4,
            "名句": 3,
            "毛泽东诗词": 2,
            "元曲四大家": 2,
            "诗经": 1,
            "楚辞": 1,
            "近代名篇": 1,
        }
        return w.get(category, 1)

    def _random_classic_verse(self):
        """从经典曲库按分类权重随机抽取一句完整句。返回 (sentence, poem) 或 None。"""
        if not self._classic_sentences:
            return None
        # 按分类分组
        groups = {}
        for sent, poem in self._classic_sentences.items():
            cat = poem.get("category", "")
            groups.setdefault(cat, []).append(sent)
        # 按权重选分类
        cats = list(groups.keys())
        weights = [self._category_weight(c) for c in cats]
        chosen_cat = random.choices(cats, weights=weights, k=1)[0]
        sent = random.choice(groups[chosen_cat])
        return sent, self._classic_sentences[sent]

    def new_game(self, word_len=None, combo=None):
        """随机抽取一句作为答案。返回 (ok, msg)。

        word_len: 指定单句字数（4-7）
        combo: (a, b) 指定「相邻 a字+b字 两句」格式（如 (5,5)/(3,4)/(7,7)）
        无参数时随机单句；combo 优先从总库抽。
        """
        # 优先：总库抽「两句」格式
        if combo and self.db_source:
            db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, "db_path", None)
            if db_path and os.path.exists(db_path):
                try:
                    cands = self.db_source.get_random_verse_by_combo(combo[0], combo[1], target_count=5)
                    if cands:
                        verse, title, author, dynasty = random.choice(cands)
                        self._set_target(verse, {"title": title, "author": author, "dynasty": dynasty})
                        return True, verse
                except Exception:
                    pass

        # 优先：经典曲库按字数过滤（曲库句子为常见句）
        if word_len and self._classic_sentences:
            cands = []
            for sent, poem in self._classic_sentences.items():
                if len(extract_hanzi(sent)) == word_len and not extract_punct(sent):
                    cands.append((sent, poem))
            if cands:
                sent, poem = random.choice(cands)
                self._set_target(sent, poem)
                return True, sent

        # 其次：总库随机抽指定字数的单句，优先「全常见字」候选
        if word_len and self.db_source:
            db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, "db_path", None)
            if db_path and os.path.exists(db_path):
                try:
                    candidates = self.db_source.get_random_verse(word_len, word_len, target_count=40)
                    candidates = [c for c in candidates if len(extract_hanzi(c[0])) == word_len]
                    if candidates:
                        common = [c for c in candidates if set(extract_hanzi(c[0])) <= (self._common_chars or set())]
                        pick = random.choice(common) if common else random.choice(candidates)
                        verse, title, author, dynasty = pick
                        self._set_target(verse, {"title": title, "author": author, "dynasty": dynasty})
                        return True, verse
                except Exception:
                    pass

        # 回退：经典曲库（任意字数）
        classic = self._random_classic_verse()
        if classic:
            sent, poem = classic
            self._set_target(sent, poem)
            return True, sent

        # 最后回退：全库随机抽取
        if not self.db_source:
            return False, "数据库不可用"
        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, "db_path", None)
        if not db_path or not os.path.exists(db_path):
            return False, "数据库未安装"
        candidates = self.db_source.get_random_verse(self.min_len, self.max_len, target_count=20)
        if not candidates:
            return False, "未找到合适的诗句"
        verse, title, author, dynasty = random.choice(candidates)
        self._set_target(verse, {"title": title, "author": author, "dynasty": dynasty})
        return True, verse

    def _set_target(self, sent, poem):
        """设置答案：完整句 + 汉字分解 + 标点序列。"""
        self.target_text = sent
        self.target_hanzi = extract_hanzi(sent)
        self.target_parts = decompose_text(self.target_hanzi)
        self.target_punct = extract_punct(sent)
        self.title = poem.get("title", "")
        self.author = poem.get("author", "")
        self.dynasty = poem.get("dynasty", "")
        self.history = []
        self.initial_status = {}
        self.final_status = {}

    def format_desc(self):
        """返回答案格式描述，如「5字单句」「5字+5字（两句）」。末尾标点不计。"""
        punct = self.target_punct
        if not punct:
            return f"{len(self.target_hanzi)} 字单句"
        # 按标点切分得到各分句字数
        lens = []
        cur = 0
        hanzi = self.target_hanzi
        for pos, p in punct:
            if pos >= len(hanzi):
                continue  # 末尾标点不计
            lens.append(pos - cur)
            cur = pos
        lens.append(len(hanzi) - cur)
        return " + ".join(f"{x} 字" for x in lens) + "（两句）"

    def check_format(self, text):
        """校验猜测文本的格式是否与答案一致。返回 (ok, msg)。
        text 为含标点的完整句。单句答案须无标点且字数一致；两句答案须有标点且各分句字数匹配。
        末尾标点（句号/感叹号等，位置==汉字总数）不作为分句分隔计入。
        """
        hanzi = extract_hanzi(text)
        punct = extract_punct(text)
        if not punct and not self.target_punct:
            # 单句
            return len(hanzi) == len(self.target_hanzi), None
        if punct and self.target_punct:
            # 两句：比较各分句字数（忽略末尾标点产生的空分句）
            def _lens(h, ps):
                ps = [pp for pp in ps if pp[0] < len(h)]  # 只把后面还有汉字的标点当分隔
                lens = []
                cur = 0
                for pos, p in ps:
                    lens.append(pos - cur)
                    cur = pos
                lens.append(len(h) - cur)
                return lens
            g_lens = _lens(hanzi, punct)
            t_lens = _lens(self.target_hanzi, self.target_punct)
            if g_lens == t_lens and len(hanzi) == len(self.target_hanzi):
                return True, None
            return False, f"格式需为 {'、'.join(str(x) for x in t_lens)} 字分句（如：带标点的一句）"
        # 格式类别不匹配
        return False, None

    def guess(self, text):
        """处理一次猜测。返回 (ok, msg, compare_result, all_correct)。
        输入为纯汉字（cc 前缀与库校验由上层负责）。只按汉字数与答案匹配，汉字做 Wordle 比较。"""
        text = text.strip()
        clean_hanzi = extract_hanzi(text)
        if not clean_hanzi:
            return False, "请输入汉字诗句", None, False
        if len(clean_hanzi) > self.max_len:
            return False, f"诗句最长 {self.max_len} 字，当前 {len(clean_hanzi)} 字", None, False
        if len(clean_hanzi) < self.min_len:
            return False, f"诗句最短 {self.min_len} 字，当前 {len(clean_hanzi)} 字", None, False
        if len(clean_hanzi) != len(self.target_hanzi):
            return False, f"答案 {len(self.target_hanzi)} 个字，当前 {len(clean_hanzi)} 字", None, False

        guess_parts = decompose_text(clean_hanzi)
        comp = compare_guess(guess_parts, self.target_parts)
        self.history.append((text, guess_parts, comp))
        self._record_pinyin_status(guess_parts, comp)

        all_correct = all(
            c is not None and all(v == "correct" for v in c.values())
            for c in comp
        )
        return True, "", comp, all_correct

    def is_finished(self):
        # max_attempts 为 None 表示不限次数，永不因次数耗尽结束
        if self.max_attempts is None:
            return False
        return len(self.history) >= self.max_attempts


# ============ 渲染 ============

CELL_W, CELL_H = 160, 170
GAP = 8
PAD = 40
HEADER_H = 85
FOOTER_H = 70

# 纯白底 + 黑线框，用文字颜色区分状态
CELL_BG = (255, 255, 255)
BORDER_COLOR = (0, 0, 0)
STATUS_COLOR = {
    "correct": (0, 150, 40),      # 绿
    "present": (230, 110, 0),     # 橙
    "absent": (160, 160, 160),    # 灰
    "empty": (200, 200, 205),     # 空格
    "default": (30, 30, 30),      # 黑
}

# 声母表（23 个，含 y/w）
INITIALS_LIST = ['b', 'p', 'm', 'f', 'd', 't', 'n', 'l', 'g', 'k', 'h',
                 'j', 'q', 'x', 'zh', 'ch', 'sh', 'r', 'z', 'c', 's', 'y', 'w']

# 韵母表（含介音韵母）
FINALS_LIST = ['a', 'o', 'e', 'i', 'u', 'ü', 'ai', 'ei', 'ui', 'ao', 'ou', 'iu',
               'ie', 'üe', 'er', 'an', 'en', 'in', 'un', 'ün', 'ang', 'eng', 'ing',
               'ong', 'ia', 'iao', 'ian', 'iang', 'iong', 'ua', 'uo', 'uai', 'uan',
               'uang', 'ueng', 'üan']

# 提示图颜色：correct绿 / present黄 / absent黑 / 默认浅灰
HINT_COLOR = {
    "correct": (0, 150, 40),
    "present": (230, 140, 0),
    "absent": (50, 50, 55),
    "default": (235, 235, 240),
}

_FONT_PATH = None
_PLUGIN_DIR = None


def _init_plugin_dir(plugin_dir):
    global _PLUGIN_DIR
    _PLUGIN_DIR = plugin_dir


def _get_font(size):
    global _FONT_PATH
    if _FONT_PATH and os.path.exists(_FONT_PATH):
        try:
            return ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            pass
    candidates = []
    if _PLUGIN_DIR:
        candidates.append(os.path.join(_PLUGIN_DIR, "game", "STZHONGS.TTF"))
        candidates.append(os.path.join(_PLUGIN_DIR, "STZHONGS.TTF"))
    candidates += [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                _FONT_PATH = p
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _text_width(draw, text, font):
    # 用 textlength 精确测量渲染宽度（含 kerning），避免拼接错位
    try:
        return draw.textlength(text, font=font)
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]


def _draw_pinyin_joined(draw, x_center, y_mid, segments):
    """把拼音各部件拼成整体显示，精确贴合、垂直中线对齐、分段独立着色。

    segments: [(text, color, font, gap_before), ...]
    y_mid: 垂直中线坐标，每段用 anchor='lm' 对齐到同一中线。
    返回渲染总宽度（像素），供下划线定位使用。
    """
    if not segments:
        return 0
    gaps = [seg[3] if len(seg) > 3 else 0 for seg in segments]
    total_w = sum(_text_width(draw, seg[0], seg[2]) for seg in segments) + sum(gaps)
    cur_x = x_center - total_w // 2
    for i, seg in enumerate(segments):
        text, color, font = seg[0], seg[1], seg[2]
        cur_x += gaps[i]
        draw.text((cur_x, y_mid), text, fill=color, font=font, anchor="lm")
        cur_x += _text_width(draw, text, font)
    return total_w


def _draw_pinyin_underline(draw, x_center, y_mid, total_w, font, color=None):
    """在拼音文字下方画一条下划线（橙色）。用于提示该音节在原句中存在。"""
    if total_w <= 0:
        return
    try:
        bbox = font.getbbox("Ag")
        fh = bbox[3] - bbox[1]
    except Exception:
        fh = 22
    if color is None:
        color = (230, 110, 0)
    y_line = y_mid + fh // 2 + 4
    draw.line([x_center - total_w // 2, y_line, x_center + (total_w - total_w // 2), y_line],
              fill=color, width=3)


def _build_layout(engine):
    """构建渲染布局：[(type, index_or_punct)]。
    type='hanzi' 表示汉字列（index 为汉字序号），type='punct' 表示标点列。
    标点 (pos, punct) 表示「第 pos 个汉字之后」，即处理完第 i 个汉字后若 i+1==pos 则加标点。
    """
    layout = []
    punct_map = dict(engine.target_punct)  # {hanzi_pos_after: punct}
    n = len(engine.target_hanzi)
    for i in range(n):
        layout.append(("hanzi", i))
        if (i + 1) in punct_map:
            layout.append(("punct", punct_map[i + 1]))
    return layout


PUNCT_W = 40  # 标点列宽度


def render_grid(engine, output_path, title="猜诗句", max_attempts=10, hint_mode="pinyin"):
    """渲染游戏网格。列 = 汉字列 + 标点列。
    hint_mode: "pinyin" 拼音提示（默认） / "radical" 部首提示（放大汉字+边框信号）。
    """
    layout = _build_layout(engine)
    n_cols = len(layout)
    n_rows = max(1, len(engine.history))

    # 计算每列宽度
    col_widths = [PUNCT_W if t == "punct" else CELL_W for t, _ in layout]
    img_w = PAD * 2 + sum(col_widths) + (n_cols - 1) * GAP
    img_h = PAD + HEADER_H + n_rows * (CELL_H + GAP) + FOOTER_H

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(30)
    f_char = _get_font(54)
    f_rad_char = _get_font(64)
    f_py = _get_font(30)
    f_hint = _get_font(15)
    f_punct = _get_font(36)

    title_label = "猜诗句 · 部首提示" if hint_mode == "radical" else title
    draw.text((img_w // 2, 20), title_label, fill=(30, 30, 30), font=f_title, anchor="mt")

    y_start = PAD + HEADER_H

    # 原句音节集合（声母+韵母），用于下划线提示（仅拼音模式）
    answer_syllable_set = _build_syllable_set(engine.target_parts)

    # 预计算每列 x 坐标
    col_x = []
    cx = PAD
    for i, w in enumerate(col_widths):
        col_x.append(cx)
        cx += w + GAP

    for row_idx, (guess_word, guess_parts, comp_result) in enumerate(engine.history):
        y = y_start + row_idx * (CELL_H + GAP)
        hanzi_pos = 0
        # 部首模式：预先计算该行各格部首状态
        guess_chars = [p["char"] for p in guess_parts]
        answer_chars = [p["char"] for p in engine.target_parts]
        rad_cs, rad_rs = radical_status(guess_chars, answer_chars) if hint_mode == "radical" else (None, None)
        for col_idx, (ltype, lval) in enumerate(layout):
            x = col_x[col_idx]
            if ltype == "punct":
                # 标点列：显示标点（深色，不参与比较）
                draw.rounded_rectangle([x, y, x + PUNCT_W, y + CELL_H], radius=6, fill=(245, 245, 248),
                                       outline=(210, 210, 215), width=1)
                draw.text((x + PUNCT_W // 2, y + int(CELL_H * 0.72)), lval, fill=(90, 90, 95), font=f_punct, anchor="mm")
                continue

            cell = comp_result[hanzi_pos] if hanzi_pos < len(comp_result) else None
            gp = guess_parts[hanzi_pos] if hanzi_pos < len(guess_parts) else None
            hanzi_pos += 1

            if cell is None:
                draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=CELL_BG, outline=BORDER_COLOR, width=2)
                continue

            if hint_mode == "radical":
                # 部首模式：放大汉字 + 边框信号
                cs = rad_cs[hanzi_pos - 1] if rad_cs else "absent"
                rs = rad_rs[hanzi_pos - 1] if rad_rs else "absent"
                if rs == "present":
                    border = (230, 110, 0)
                elif rs == "absent":
                    border = (160, 160, 160)
                else:  # correct
                    border = (0, 150, 40) if cs == "correct" else (230, 110, 0)
                draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=CELL_BG, outline=border, width=5)
                ch = gp["char"] if gp else ""
                cc = {"correct": (0, 150, 40), "present": (230, 110, 0), "absent": (160, 160, 160)}[cs]
                draw.text((x + CELL_W // 2, y + CELL_H // 2), ch, fill=cc, font=f_rad_char, anchor="mm")
                continue

            draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=CELL_BG, outline=BORDER_COLOR, width=2)

            ch = gp["char"] if gp else ""
            ch_color = STATUS_COLOR.get(cell.get("char", "default"), STATUS_COLOR["default"])
            draw.text((x + CELL_W // 2, y + CELL_H // 2), ch, fill=ch_color, font=f_char, anchor="mm")

            # 拼音
            py_mid = y + CELL_H // 2 - 46
            if gp:
                initial = gp.get("initial", "")
                final = gp.get("final", "")
                tone = str(gp.get("tone", "")) if gp.get("tone", 0) > 0 else ""

                init_color = STATUS_COLOR.get(cell.get("initial", "default"), STATUS_COLOR["default"])
                final_color = STATUS_COLOR.get(cell.get("final", "default"), STATUS_COLOR["default"])
                tone_color = STATUS_COLOR.get(cell.get("tone", "default"), STATUS_COLOR["default"])

                segments = []
                if initial:
                    segments.append((initial, init_color, f_py))
                if final:
                    segments.append((final, final_color, f_py))
                if tone:
                    segments.append((tone, tone_color, f_py, 8))
                total_w = _draw_pinyin_joined(draw, x + CELL_W // 2, py_mid, segments)
                # 音节(声母+韵母)在原句中出现过，且整字未完全正确 → 拼音下加下划线
                if _syllable_underlined(gp, cell, answer_syllable_set):
                    _draw_pinyin_underline(draw, x + CELL_W // 2, py_mid, total_w, f_py)

    # 底部
    if max_attempts is None:
        footer = f"不限次数    已猜 {len(engine.history)} 次    答案 {len(engine.target_hanzi)} 字"
    else:
        remaining = max_attempts - len(engine.history)
        footer = f"剩余机会: {remaining} / {max_attempts}    答案 {len(engine.target_hanzi)} 字"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def render_blank(engine, output_path):
    """渲染空白占位框：□ □ □ ， □ □ □ ！ 显示格式。"""
    layout = _build_layout(engine)
    col_widths = [PUNCT_W if t == "punct" else CELL_W for t, _ in layout]
    n_cols = len(layout)
    img_w = PAD * 2 + sum(col_widths) + (n_cols - 1) * GAP
    img_h = PAD + HEADER_H + CELL_H + GAP + FOOTER_H

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(30)
    f_blank = _get_font(46)
    f_punct = _get_font(36)
    f_hint = _get_font(15)

    draw.text((img_w // 2, 20), "猜诗句·空白框", fill=(30, 30, 30), font=f_title, anchor="mt")

    col_x = []
    cx = PAD
    for i, w in enumerate(col_widths):
        col_x.append(cx)
        cx += w + GAP

    y = PAD + HEADER_H
    for col_idx, (ltype, lval) in enumerate(layout):
        x = col_x[col_idx]
        if ltype == "punct":
            draw.rounded_rectangle([x, y, x + PUNCT_W, y + CELL_H], radius=6, fill=(245, 245, 248),
                                   outline=(210, 210, 215), width=1)
            draw.text((x + PUNCT_W // 2, y + int(CELL_H * 0.72)), lval, fill=(90, 90, 95), font=f_punct, anchor="mm")
        else:
            draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=CELL_BG, outline=BORDER_COLOR, width=2)
            draw.text((x + CELL_W // 2, y + CELL_H // 2), "□", fill=(170, 170, 175), font=f_blank, anchor="mm")

    footer = f"共 {len(engine.target_hanzi)} 字，发送对应字数的完整句（标点不限）"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def render_answer(engine, output_path):
    """渲染答案揭示图（含标点）。"""
    layout = _build_layout(engine)
    col_widths = [PUNCT_W if t == "punct" else 100 for t, _ in layout]
    n_cols = len(layout)
    img_w = PAD * 2 + sum(col_widths) + (n_cols - 1) * GAP
    img_h = 210
    img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_sm = _get_font(18)
    f_char = _get_font(48)
    f_punct = _get_font(34)
    f_info = _get_font(16)

    draw.text((img_w // 2, 18), "正确诗句", fill=(40, 40, 40), font=f_sm, anchor="mt")

    col_x = []
    cx = PAD
    for i, w in enumerate(col_widths):
        col_x.append(cx)
        cx += w + GAP

    hanzi_pos = 0
    for col_idx, (ltype, lval) in enumerate(layout):
        x = col_x[col_idx]
        if ltype == "punct":
            draw.text((x + PUNCT_W // 2, 105), lval, fill=(90, 90, 95), font=f_punct, anchor="mm")
        else:
            part = engine.target_parts[hanzi_pos]
            draw.text((x + 100 // 2, 90), part["char"], fill=(24, 144, 255), font=f_char, anchor="mm")
            hanzi_pos += 1

    info = f"《{engine.title}》 [{engine.dynasty}] {engine.author}"
    draw.text((img_w // 2, 180), info, fill=(80, 80, 80), font=f_info, anchor="mm")

    img.save(output_path, "PNG")
    return output_path


def render_verse_list(uid, uname, verses, output_path):
    """渲染玩家积累诗句列表图。"""
    pad = 30
    title_h = 70
    row_h = 34
    item_font = _get_font(20)
    title_font = _get_font(26)
    col_gap = 18
    per_row = 5

    sorted_items = sorted(verses.items(), key=lambda kv: kv[1].get("first", 0))
    items = [v for v, _ in sorted_items]
    total = len(items)
    rows = (total + per_row - 1) // per_row if total else 1

    cell_w = 150
    img_w = pad * 2 + per_row * cell_w + (per_row - 1) * col_gap
    img_h = pad + title_h + rows * (row_h + 4) + pad

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    draw.text((img_w // 2, 20), f"📚 {uname} 的诗词积累（{total} 句）", fill=(40, 40, 40), font=title_font, anchor="mt")

    y = pad + title_h
    for idx, v in enumerate(items):
        col = idx % per_row
        row = idx // per_row
        x = pad + col * (cell_w + col_gap)
        cy = y + row * (row_h + 4)
        draw.rounded_rectangle([x, cy, x + cell_w, cy + row_h], radius=6, fill=(255, 255, 255),
                               outline=(200, 200, 205), width=1)
        draw.text((x + cell_w // 2, cy + row_h // 2), v, fill=(50, 50, 50), font=item_font, anchor="mm")

    img.save(output_path, "PNG")
    return output_path


def render_hint(engine, output_path):
    """渲染声母韵母提示图。

    - 黑色：已排除（不在答案中）
    - 绿色：正确且位置正确
    - 黄色：答案中存在但位置错误
    - 浅灰：尚未在猜测中出现（可能仍在答案中）
    """
    pad = 30
    title_h = 60
    cell_h = 44
    gap = 6

    init_cell_w = 52
    final_cell_w = 64

    f_title = _get_font(26)
    f_label = _get_font(18)
    f_item = _get_font(20)
    f_legend = _get_font(15)

    def line_width(n, cw):
        return n * cw + (n - 1) * gap

    init_per_row = 12
    init_rows = (len(INITIALS_LIST) + init_per_row - 1) // init_per_row
    final_per_row = 10
    final_rows = (len(FINALS_LIST) + final_per_row - 1) // final_per_row

    content_w = max(line_width(init_per_row, init_cell_w), line_width(final_per_row, final_cell_w))
    img_w = pad * 2 + content_w

    init_area_h = 30 + init_rows * (cell_h + gap)
    final_area_h = 30 + final_rows * (cell_h + gap)
    legend_h = 40
    img_h = pad + title_h + init_area_h + final_area_h + legend_h + pad

    img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.text((img_w // 2, 18), "声母韵母提示", fill=(30, 30, 30), font=f_title, anchor="mt")

    y = pad + title_h

    def draw_group(label, items, status_map, cell_w, per_row, yy):
        draw.text((pad, yy), label, fill=(80, 80, 80), font=f_label, anchor="lt")
        yy += 28
        for idx, item in enumerate(items):
            row = idx // per_row
            col = idx % per_row
            x = pad + col * (cell_w + gap)
            cy = yy + row * (cell_h + gap)
            status = status_map.get(item, "default")
            bg = HINT_COLOR.get(status, HINT_COLOR["default"])
            fg = (40, 40, 40) if status == "default" else (255, 255, 255)
            draw.rounded_rectangle([x, cy, x + cell_w, cy + cell_h], radius=6, fill=bg, outline=(200, 200, 205), width=1)
            draw.text((x + cell_w // 2, cy + cell_h // 2), item, fill=fg, font=f_item, anchor="mm")
        return yy + (len(items) + per_row - 1) // per_row * (cell_h + gap)

    y = draw_group("声母", INITIALS_LIST, engine.initial_status, init_cell_w, init_per_row, y)
    y += 6
    draw_group("韵母", FINALS_LIST, engine.final_status, final_cell_w, final_per_row, y)

    legend_y = img_h - pad - legend_h + 10
    lx = pad
    for label, color in [("绿色=正确", HINT_COLOR["correct"]), ("黄色=错位", HINT_COLOR["present"]),
                         ("黑色=排除", HINT_COLOR["absent"]), ("浅灰=未用", HINT_COLOR["default"])]:
        draw.rectangle([lx, legend_y, lx + 14, legend_y + 14], fill=color, outline=(200, 200, 205))
        draw.text((lx + 18, legend_y - 2), label, fill=(60, 60, 60), font=f_legend, anchor="lt")
        lx += 110

    img.save(output_path, "PNG")
    return output_path


# ============ 邀战对战模式 ============

_BATTLE_RE = None


def pick_battle_target(classic_poems):
    """从教材诗词分类随机选一句「N，N。」结构（两等长分句，N=4/5/6）。返回 poem 或 None。"""
    candidates = []
    for p in classic_poems:
        if p.get("category", "") != "教材诗词":
            continue
        sent = p.get("sentence", "")
        m = re.match(r'^([\u4e00-\u9fff]{4,6})，([\u4e00-\u9fff]{4,6})[。！？]$', sent)
        if m and len(m.group(1)) == len(m.group(2)):
            p = dict(p)
            p["first"] = m.group(1)
            p["second"] = m.group(2)
            p["mid_punct"] = "，"
            p["end_punct"] = sent[-1]
            candidates.append(p)
    if not candidates:
        return None
    return random.choice(candidates)


class BattleVerseEngine:
    """邀战猜诗词对战引擎：两人各猜半句，先猜中者胜。"""

    def __init__(self, poem, first_id, first_name, second_id, second_name):
        self.poem = poem
        self.first_id = str(first_id)       # 1号，猜前句
        self.first_name = first_name
        self.second_id = str(second_id)     # 2号，猜后句
        self.second_name = second_name

        self.first = poem["first"]
        self.second = poem["second"]
        self.first_parts = decompose_text(self.first)
        self.second_parts = decompose_text(self.second)

        self.first_history = []   # [(text, parts, comp)]
        self.second_history = []  # [(text, parts, comp)]
        self.current = "first"    # 轮到谁
        self.winner = None
        self.round = 0

    def guess(self, user_id, text):
        """处理某位玩家的半句猜测。返回 (ok, msg, side, comp, all_correct)。"""
        uid = str(user_id)
        text = text.strip()
        clean = extract_hanzi(text)
        if not clean:
            return False, "请输入汉字诗句", None, None, False

        side = "first" if uid == self.first_id else "second"
        target_parts = self.first_parts if side == "first" else self.second_parts
        target_len = len(target_parts)

        if len(clean) != target_len:
            return False, f"你的半句是 {target_len} 个字，当前 {len(clean)} 字", side, None, False

        guess_parts = decompose_text(clean)
        comp = compare_guess(guess_parts, target_parts)
        if side == "first":
            self.first_history.append((clean, guess_parts, comp))
        else:
            self.second_history.append((clean, guess_parts, comp))

        all_correct = all(c is not None and all(v == "correct" for v in c.values()) for c in comp)
        if all_correct:
            self.winner = side
        return True, "", side, comp, all_correct

    def switch_turn(self):
        self.round += 1
        self.current = "second" if self.current == "first" else "first"

    def is_turn(self, user_id):
        return str(user_id) == (self.first_id if self.current == "first" else self.second_id)

    def current_name(self):
        return self.first_name if self.current == "first" else self.second_name

    def current_side(self):
        return self.current


def render_battle(engine, output_path):
    """渲染对战共享棋盘：左=1号前句，右=2号后句。"""
    n = len(engine.first_parts)

    pad = 30
    header_h = 70
    sub_h = 50
    cell_h = 150
    gap = 8

    cell_w = 120
    mid_gap = 40  # 左右区之间的间距

    rows_first = max(1, len(engine.first_history))
    rows_second = max(1, len(engine.second_history))
    n_rows = max(rows_first, rows_second)

    side_w = n * cell_w + (n - 1) * gap
    img_w = pad * 2 + side_w * 2 + mid_gap
    img_h = pad + header_h + sub_h + (n_rows + 1) * (cell_h + gap) + 70

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(28)
    f_sub = _get_font(20)
    f_char = _get_font(50)
    f_py = _get_font(26)
    f_hint = _get_font(14)

    draw.text((img_w // 2, 18), "猜诗词对战", fill=(30, 30, 30), font=f_title, anchor="mt")

    x_left = pad
    x_right = pad + side_w + mid_gap

    draw.text((x_left + side_w // 2, pad + header_h - 20), f"{engine.first_name}·前句", fill=(40, 40, 40), font=f_sub, anchor="mm")
    draw.text((x_right + side_w // 2, pad + header_h - 20), f"{engine.second_name}·后句", fill=(40, 40, 40), font=f_sub, anchor="mm")

    y0 = pad + header_h + sub_h

    # 首行空白框
    for side, x0 in (("first", x_left), ("second", x_right)):
        for i in range(n):
            x = x0 + i * (cell_w + gap)
            draw.rounded_rectangle([x, y0, x + cell_w, y0 + cell_h], radius=8, fill=CELL_BG, outline=BORDER_COLOR, width=2)

    # 猜测历史
    for row_idx in range(n_rows):
        y = y0 + (row_idx + 1) * (cell_h + gap)
        if row_idx < len(engine.first_history):
            text, parts, comp = engine.first_history[row_idx]
            _draw_battle_row(draw, x_left, y, n, cell_w, cell_h, gap, parts, comp, f_char, f_py)
        if row_idx < len(engine.second_history):
            text, parts, comp = engine.second_history[row_idx]
            _draw_battle_row(draw, x_right, y, n, cell_w, cell_h, gap, parts, comp, f_char, f_py)

    if engine.winner:
        wname = engine.first_name if engine.winner == "first" else engine.second_name
        footer = f"🏆 {wname} 获胜！答案：{engine.first}，{engine.second}{engine.poem['end_punct']}"
    else:
        footer = f"轮到：{engine.current_name()}"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def _build_duel_layout(punct, n):
    """构建对垒渲染布局：[(type, index_or_punct)]。
    type='hanzi' 汉字列（index 为汉字序号）；type='punct' 标点列（显示标点）。
    punct: [(汉字位置, 标点)]，标点插在对应汉字之后。
    """
    layout = []
    punct_map = dict(punct)
    for i in range(n):
        layout.append(("hanzi", i))
        if (i + 1) in punct_map:
            layout.append(("punct", punct_map[i + 1]))
    return layout


def _draw_battle_row(draw, x0, y, layout, col_w, punct_w, cell_w, cell_h, gap, parts, comp, f_char, f_py, f_punct, syllable_set=None):
    if syllable_set is None:
        syllable_set = set()
    cx = x0
    hanzi_pos = 0
    for col_type, col_val in layout:
        if col_type == "punct":
            # 标点列
            draw.rounded_rectangle([cx, y, cx + punct_w, y + cell_h], radius=6, fill=(245, 245, 248),
                                   outline=(210, 210, 215), width=1)
            draw.text((cx + punct_w // 2, y + cell_h // 2), col_val, fill=(90, 90, 95), font=f_punct, anchor="mm")
            cx += punct_w + gap
            continue
        i = col_val
        x = cx
        cell = comp[i] if i < len(comp) else None
        gp = parts[i] if i < len(parts) else None
        draw.rounded_rectangle([x, y, x + cell_w, y + cell_h], radius=8, fill=CELL_BG, outline=BORDER_COLOR, width=2)
        if not gp:
            cx += cell_w + gap
            continue
        ch_color = STATUS_COLOR.get(cell.get("char", "default"), STATUS_COLOR["default"]) if cell else STATUS_COLOR["default"]
        draw.text((x + cell_w // 2, y + cell_h // 2 + 16), gp["char"], fill=ch_color, font=f_char, anchor="mm")

        if cell:
            py_mid = y + cell_h // 2 - 40
            initial = gp.get("initial", "")
            final = gp.get("final", "")
            tone = str(gp.get("tone", "")) if gp.get("tone", 0) > 0 else ""
            init_color = STATUS_COLOR.get(cell.get("initial", "default"), STATUS_COLOR["default"])
            final_color = STATUS_COLOR.get(cell.get("final", "default"), STATUS_COLOR["default"])
            tone_color = STATUS_COLOR.get(cell.get("tone", "default"), STATUS_COLOR["default"])
            segments = []
            if initial:
                segments.append((initial, init_color, f_py))
            if final:
                segments.append((final, final_color, f_py))
            if tone:
                segments.append((tone, tone_color, f_py, 6))
            total_w = _draw_pinyin_joined(draw, x + cell_w // 2, py_mid, segments)
            # 音节在原句中出现过，且整字未完全正确 → 拼音下加下划线
            if _syllable_underlined(gp, cell, syllable_set):
                _draw_pinyin_underline(draw, x + cell_w // 2, py_mid, total_w, f_py)
        cx += cell_w + gap


# ============ 诗词对垒（互猜对方诗句）============


def pick_puzzle_verse(classic_poems, word_len):
    """从曲库随机选一句纯汉字数恰好为 word_len 的诗句（含标点）。返回 sentence 或 None。"""
    cands = [p.get("sentence", "") for p in classic_poems
             if len(extract_hanzi(p.get("sentence", ""))) == word_len]
    if not cands:
        return None
    return random.choice(cands)


class DuelVerseEngine:
    """诗词对垒引擎：双方各出题（4-7字），互相猜对方诗句，先猜中者胜。

    出题字数在创建时统一，双方同字数。
    """

    def __init__(self, a_puzzle, b_puzzle, a_id, a_name, b_id, b_name):
        # a 是挑战者，b 是被挑战者
        self.a_id = str(a_id)
        self.a_name = a_name
        self.b_id = str(b_id)
        self.b_name = b_name
        # 各自出的题（可能含标点，如「离离原上草，一岁一枯荣」）
        self.a_puzzle = a_puzzle
        self.b_puzzle = b_puzzle
        # 各自要猜的目标（对方出的题），比较用纯汉字
        self.a_target_hanzi = extract_hanzi(b_puzzle)
        self.b_target_hanzi = extract_hanzi(a_puzzle)
        self.a_target_parts = decompose_text(self.a_target_hanzi)  # A 猜 B 的题
        self.b_target_parts = decompose_text(self.b_target_hanzi)  # B 猜 A 的题
        self.a_target_punct = extract_punct(b_puzzle)
        self.b_target_punct = extract_punct(a_puzzle)
        # 各自猜测历史
        self.a_history = []  # [(text, parts, comp)] A 猜 B 题
        self.b_history = []  # [(text, parts, comp)] B 猜 A 题
        self.current = "a"
        self.winner = None
        self.round = 0

    def guess(self, user_id, text):
        """处理某位玩家的猜测（猜对方诗句）。返回 (ok, msg, side, comp, all_correct)。
        text 为含标点的完整句；比较用纯汉字。"""
        uid = str(user_id)
        clean = extract_hanzi(text)
        if not clean:
            return False, "请输入汉字诗句", None, None, False

        side = "a" if uid == self.a_id else "b"
        target_parts = self.a_target_parts if side == "a" else self.b_target_parts
        target_hanzi = self.a_target_hanzi if side == "a" else self.b_target_hanzi
        target_punct = self.a_target_punct if side == "a" else self.b_target_punct
        target_len = len(target_hanzi)

        if len(clean) != target_len:
            return False, f"对方诗句是 {target_len} 个字，当前 {len(clean)} 字", side, None, False

        # 格式校验：两句题目要求猜测也带标点且分句字数匹配
        ok_fmt, fmt_msg = self.check_format(text, target_hanzi, target_punct)
        if not ok_fmt:
            return False, fmt_msg or "诗句格式需与对方一致（两句需带标点）", side, None, False

        guess_parts = decompose_text(clean)
        comp = compare_guess(guess_parts, target_parts)
        if side == "a":
            self.a_history.append((clean, guess_parts, comp))
        else:
            self.b_history.append((clean, guess_parts, comp))

        all_correct = all(c is not None and all(v == "correct" for v in c.values()) for c in comp)
        if all_correct:
            self.winner = side
        return True, "", side, comp, all_correct

    @staticmethod
    @staticmethod
    def check_format(text, target_hanzi, target_punct):
        """校验猜测格式与目标一致。末尾标点（位置==汉字总数）不作为分句分隔计入。"""
        hanzi = extract_hanzi(text)
        punct = extract_punct(text)
        if not punct and not target_punct:
            return True, None  # 都是单句
        if punct and target_punct:
            def _lens(h, ps):
                ps = [pp for pp in ps if pp[0] < len(h)]  # 只把后面还有汉字的标点当分隔
                lens = []
                cur = 0
                for pos, p in ps:
                    lens.append(pos - cur)
                    cur = pos
                lens.append(len(h) - cur)
                return lens
            g_lens = _lens(hanzi, punct)
            t_lens = _lens(target_hanzi, target_punct)
            if g_lens == t_lens:
                return True, None
            return False, f"格式需为 {'、'.join(str(x) for x in t_lens)} 字分句（两句需带标点）"
        return False, "诗句格式需与对方一致（两句需带标点，单句无需）"

    def format_desc(self, side):
        """返回某侧题目的格式描述。"""
        punct = self.a_target_punct if side == "a" else self.b_target_punct
        hanzi = self.a_target_hanzi if side == "a" else self.b_target_hanzi
        if not punct:
            return f"{len(hanzi)} 字单句"
        lens = []
        cur = 0
        for pos, p in punct:
            if pos >= len(hanzi):
                continue  # 末尾标点不计
            lens.append(pos - cur)
            cur = pos
        lens.append(len(hanzi) - cur)
        return " + ".join(f"{x} 字" for x in lens) + "（两句）"

    def switch_turn(self):
        self.round += 1
        self.current = "b" if self.current == "a" else "a"

    def is_turn(self, user_id):
        return str(user_id) == (self.a_id if self.current == "a" else self.b_id)

    def current_name(self):
        return self.a_name if self.current == "a" else self.b_name

    def side_name(self, side):
        return self.a_name if side == "a" else self.b_name

    def current_side(self):
        return self.current

    def target_parts_of(self, side):
        return self.a_target_parts if side == "a" else self.b_target_parts

    def history_of(self, side):
        return self.a_history if side == "a" else self.b_history

    def replace_side_puzzle(self, side, new_puzzle):
        """金蝉脱壳：替换某一方出的题（side=a 为挑战者）。对方要猜的目标随之更新并清空对方历史。"""
        clean = extract_hanzi(new_puzzle)
        parts = decompose_text(clean)
        punct = extract_punct(new_puzzle)
        if side == "a":
            self.a_puzzle = new_puzzle
            self.b_target_hanzi = clean
            self.b_target_parts = parts
            self.b_target_punct = punct
            self.b_history = []
        else:
            self.b_puzzle = new_puzzle
            self.a_target_hanzi = clean
            self.a_target_parts = parts
            self.a_target_punct = punct
            self.a_history = []
        return True

    def replace_my_target(self, side, new_puzzle):
        """（备用）替换自己要猜的目标；一般用不到，留作通用。"""
        clean = extract_hanzi(new_puzzle)
        parts = decompose_text(clean)
        punct = extract_punct(new_puzzle)
        if side == "a":
            self.b_puzzle = new_puzzle
            self.a_target_hanzi = clean
            self.a_target_parts = parts
            self.a_target_punct = punct
            self.a_history = []
        else:
            self.a_puzzle = new_puzzle
            self.b_target_hanzi = clean
            self.b_target_parts = parts
            self.b_target_punct = punct
            self.b_history = []
        return True


def render_duel(engine, output_path, hint_mode="pinyin"):
    """渲染诗词对垒棋盘：左=A猜B的题，右=B猜A的题。

    hint_mode: "pinyin" 拼音提示（默认） / "radical" 部首提示。
    支持单句与两句（含标点分隔列）题目。
    """
    if hint_mode == "radical":
        return render_duel_radical(engine, output_path)
    a_punct = engine.a_target_punct  # A 猜 B 的题（B 出的题）标点
    b_punct = engine.b_target_punct  # B 猜 A 的题（A 出的题）标点
    n_a = len(engine.a_target_hanzi)
    n_b = len(engine.b_target_hanzi)
    layout_a = _build_duel_layout(a_punct, n_a)
    layout_b = _build_duel_layout(b_punct, n_b)

    pad = 30
    header_h = 70
    sub_h = 50
    cell_h = 150
    gap = 8
    cell_w = 120
    punct_w = 36
    mid_gap = 40

    def side_w(layout):
        w = 0
        for t, _ in layout:
            w += (cell_w if t == "hanzi" else punct_w) + gap
        return w

    sw_a = side_w(layout_a)
    sw_b = side_w(layout_b)
    side_w_max = max(sw_a, sw_b)

    rows_a = max(1, len(engine.a_history))
    rows_b = max(1, len(engine.b_history))
    n_rows = max(rows_a, rows_b)

    img_w = pad * 2 + side_w_max * 2 + mid_gap
    img_h = pad + header_h + sub_h + (n_rows + 1) * (cell_h + gap) + 70

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(28)
    f_sub = _get_font(20)
    f_char = _get_font(50)
    f_py = _get_font(26)
    f_punct = _get_font(32)
    f_hint = _get_font(14)

    draw.text((img_w // 2, 18), "诗词对垒", fill=(30, 30, 30), font=f_title, anchor="mt")

    x_left = pad
    x_right = pad + side_w_max + mid_gap

    draw.text((x_left + side_w_max // 2, pad + header_h - 20), f"{engine.a_name} 猜 {engine.b_name} 的题", fill=(40, 40, 40), font=f_sub, anchor="mm")
    draw.text((x_right + side_w_max // 2, pad + header_h - 20), f"{engine.b_name} 猜 {engine.a_name} 的题", fill=(40, 40, 40), font=f_sub, anchor="mm")

    y0 = pad + header_h + sub_h

    # 首行空白框（按各自布局）
    for layout, x0 in ((layout_a, x_left), (layout_b, x_right)):
        cx = x0
        for t, _ in layout:
            w = cell_w if t == "hanzi" else punct_w
            if t == "hanzi":
                draw.rounded_rectangle([cx, y0, cx + w, y0 + cell_h], radius=8, fill=CELL_BG, outline=BORDER_COLOR, width=2)
            else:
                draw.rounded_rectangle([cx, y0, cx + w, y0 + cell_h], radius=6, fill=(245, 245, 248),
                                       outline=(210, 210, 215), width=1)
            cx += w + gap

    # 猜测历史
    a_syl_set = _build_syllable_set(engine.a_target_parts)  # A 猜 B 的题
    b_syl_set = _build_syllable_set(engine.b_target_parts)  # B 猜 A 的题
    for row_idx in range(n_rows):
        y = y0 + (row_idx + 1) * (cell_h + gap)
        if row_idx < len(engine.a_history):
            _draw_battle_row(draw, x_left, y, layout_a, cell_w, punct_w, cell_w, cell_h, gap,
                             engine.a_history[row_idx][1], engine.a_history[row_idx][2], f_char, f_py, f_punct,
                             a_syl_set)
        if row_idx < len(engine.b_history):
            _draw_battle_row(draw, x_right, y, layout_b, cell_w, punct_w, cell_w, cell_h, gap,
                             engine.b_history[row_idx][1], engine.b_history[row_idx][2], f_char, f_py, f_punct,
                             b_syl_set)

    if engine.winner:
        wname = engine.side_name(engine.winner)
        footer = f"🏆 {wname} 猜中了对方的诗句！"
    else:
        footer = f"轮到：{engine.current_name()}"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def render_duel_radical(engine, output_path):
    """渲染诗词对垒棋盘（部首提示模式）。

    每个汉字格只显示放大的字（无拼音、无部首字符），颜色信号：
    - 字：对位绿 / 错位存在橙 / 不存在灰
    - 边框：部首对位绿（字对位时） / 部首错位存在橙 / 部首不存在灰
    """
    a_punct = engine.a_target_punct
    b_punct = engine.b_target_punct
    n_a = len(engine.a_target_hanzi)
    n_b = len(engine.b_target_hanzi)
    layout_a = _build_duel_layout(a_punct, n_a)
    layout_b = _build_duel_layout(b_punct, n_b)

    pad = 30
    header_h = 70
    sub_h = 50
    cell_h = 150
    gap = 8
    cell_w = 120
    punct_w = 36
    mid_gap = 40

    def side_w(layout):
        w = 0
        for t, _ in layout:
            w += (cell_w if t == "hanzi" else punct_w) + gap
        return w

    side_w_max = max(side_w(layout_a), side_w(layout_b))

    rows_a = max(1, len(engine.a_history))
    rows_b = max(1, len(engine.b_history))
    n_rows = max(rows_a, rows_b)

    img_w = pad * 2 + side_w_max * 2 + mid_gap
    img_h = pad + header_h + sub_h + (n_rows + 1) * (cell_h + gap) + 70

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(28)
    f_sub = _get_font(20)
    f_char = _get_font(64)
    f_punct = _get_font(32)
    f_hint = _get_font(14)

    draw.text((img_w // 2, 18), "诗词对垒 · 部首提示", fill=(30, 30, 30), font=f_title, anchor="mt")

    x_left = pad
    x_right = pad + side_w_max + mid_gap

    draw.text((x_left + side_w_max // 2, pad + header_h - 20), f"{engine.a_name} 猜 {engine.b_name} 的题", fill=(40, 40, 40), font=f_sub, anchor="mm")
    draw.text((x_right + side_w_max // 2, pad + header_h - 20), f"{engine.b_name} 猜 {engine.a_name} 的题", fill=(40, 40, 40), font=f_sub, anchor="mm")

    y0 = pad + header_h + sub_h

    # 首行空白框
    for layout, x0 in ((layout_a, x_left), (layout_b, x_right)):
        cx = x0
        for t, _ in layout:
            w = cell_w if t == "hanzi" else punct_w
            if t == "hanzi":
                draw.rounded_rectangle([cx, y0, cx + w, y0 + cell_h], radius=8, fill=CELL_BG, outline=BORDER_COLOR, width=2)
            else:
                draw.rounded_rectangle([cx, y0, cx + w, y0 + cell_h], radius=6, fill=(245, 245, 248),
                                       outline=(210, 210, 215), width=1)
            cx += w + gap

    # 猜测历史（部首模式）
    for row_idx in range(n_rows):
        y = y0 + (row_idx + 1) * (cell_h + gap)
        if row_idx < len(engine.a_history):
            _draw_radical_row(draw, x_left, y, layout_a, cell_w, punct_w, cell_h, gap,
                              engine.a_history[row_idx][0], engine.a_target_parts, f_char, f_punct)
        if row_idx < len(engine.b_history):
            _draw_radical_row(draw, x_right, y, layout_b, cell_w, punct_w, cell_h, gap,
                              engine.b_history[row_idx][0], engine.b_target_parts, f_char, f_punct)

    if engine.winner:
        wname = engine.side_name(engine.winner)
        footer = f"🏆 {wname} 猜中了对方的诗句！"
    else:
        footer = f"轮到：{engine.current_name()}"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def _draw_radical_row(draw, x0, y, layout, cell_w, punct_w, cell_h, gap, guess_text, target_parts, f_char, f_punct):
    """渲染一行部首模式的猜测：每格放大汉字 + 颜色信号边框，支持标点分隔列。"""
    guess_chars = list(extract_hanzi(guess_text))
    answer_chars = [p["char"] for p in target_parts]
    char_status, rad_status = radical_status(guess_chars, answer_chars)
    cx = x0
    for col_type, col_val in layout:
        if col_type == "punct":
            draw.rounded_rectangle([cx, y, cx + punct_w, y + cell_h], radius=6, fill=(245, 245, 248),
                                   outline=(210, 210, 215), width=1)
            draw.text((cx + punct_w // 2, y + cell_h // 2), col_val, fill=(90, 90, 95), font=f_punct, anchor="mm")
            cx += punct_w + gap
            continue
        i = col_val
        x = cx
        # 边框颜色：部首错位存在 -> 橙；部首不存在 -> 灰；部首对位 -> 绿（字对位时）
        if rad_status[i] == "present":
            border = (230, 110, 0)
        elif rad_status[i] == "absent":
            border = (160, 160, 160)
        else:  # correct
            border = (0, 150, 40) if char_status[i] == "correct" else (230, 110, 0)
        draw.rounded_rectangle([x, y, x + cell_w, y + cell_h], radius=8, fill=CELL_BG, outline=border, width=5)
        ch = guess_chars[i] if i < len(guess_chars) else ""
        if ch:
            cc = {"correct": (0, 150, 40), "present": (230, 110, 0), "absent": (160, 160, 160)}[char_status[i]]
            draw.text((x + cell_w // 2, y + cell_h // 2), ch, fill=cc, font=f_char, anchor="mm")
        cx += cell_w + gap


def render_achievements(uid, uname, achs, output_path):
    """渲染玩家成就列表图。

    achs: 成就字典 {id: {unlocked, time, progress}}（含未解锁）。
    """
    try:
        from ..player_data import ACHIEVEMENTS, closer_level_name, duel_streak_name
    except ImportError:
        from player_data import ACHIEVEMENTS, closer_level_name, duel_streak_name
    pad = 30
    title_h = 70
    row_h = 40
    item_font = _get_font(19)
    title_font = _get_font(26)
    small_font = _get_font(14)

    unlocked = {k: v for k, v in achs.items() if v.get("unlocked")}
    progress_items = [
        k for k, v in achs.items() if not v.get("unlocked") and v.get("progress")
    ]
    total = len(unlocked)
    rows = len(unlocked) + len(progress_items)
    lines_extra = 1 if progress_items else 0  # "进行中" 分隔行

    img_w = 720
    img_h = pad + title_h + rows * (row_h + 6) + lines_extra * (row_h + 6) + pad

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    draw.text((img_w // 2, 20), f"[成就] {uname}（{total}/{len(ACHIEVEMENTS)}）", fill=(40, 40, 40), font=title_font, anchor="mt")

    y = pad + title_h
    if not unlocked:
        draw.text((img_w // 2, y), "（暂无成就，快去玩游戏吧！）", fill=(120, 120, 125), font=item_font, anchor="mm")
        y += row_h + 6
    for k in unlocked:
        v = achs[k]
        name = ACHIEVEMENTS.get(k, (k, ""))[0]
        desc = ACHIEVEMENTS.get(k, (k, ""))[1]
        if k == "closer":
            name = closer_level_name(v.get("progress", 0))
            desc = f"累计 {v.get('progress', 0)} 次"
        elif k == "pig":
            name = f"猪 x{v.get('progress', 0)}"
            desc = "猪" * v.get("progress", 0)
        elif k == "duel_streak":
            name = duel_streak_name(v.get("progress", 0))
            desc = f"最高 {v.get('progress', 0)} 连"
        elif k == "beloved_verse":
            name = f"挚爱诗句-{v.get('verse', '')}"
            desc = f"使用 {v.get('progress', 0)} 次"
        draw.rounded_rectangle([pad, y, img_w - pad, y + row_h], radius=8, fill=(255, 255, 255),
                               outline=(200, 200, 205), width=1)
        draw.text((pad + 14, y + row_h // 2), f"[已解锁] {name}", fill=(40, 40, 40), font=item_font, anchor="lm")
        draw.text((img_w - pad - 14, y + row_h // 2), desc, fill=(120, 120, 125), font=small_font, anchor="rm")
        y += row_h + 6
    if progress_items:
        draw.text((pad + 14, y + row_h // 2), "—— 进行中 ——", fill=(160, 160, 165), font=small_font, anchor="lm")
        y += row_h + 6
        for k in progress_items:
            v = achs[k]
            name = ACHIEVEMENTS.get(k, (k, ""))[0]
            desc = f"进度：{v['progress']}"
            if k == "duel_streak":
                name = duel_streak_name(v.get("progress", 0))
                desc = f"最高 {v.get('progress', 0)} 连"
            draw.rounded_rectangle([pad, y, img_w - pad, y + row_h], radius=8, fill=(248, 248, 250),
                                   outline=(220, 220, 225), width=1)
            draw.text((pad + 14, y + row_h // 2), f"[进行中] {name}", fill=(120, 120, 125), font=item_font, anchor="lm")
            draw.text((img_w - pad - 14, y + row_h // 2), desc, fill=(160, 160, 165), font=small_font, anchor="rm")
            y += row_h + 6

    img.save(output_path, "PNG")
    return output_path


def render_poetry_report(report, output_path):
    """渲染个人诗句积累分析报表长图。

    report: dict {
      uname, total, uses, dup,
      top_verses: [dict(text,count,author)],
      top_authors: [dict(name,count)],
      dynasties: [(name,count,pct)],
      word_len: [(label,count)],
      top_chars: [(char,count)],
      unknown: int,
    }
    """
    W = 900
    PAD = 28
    H1 = 70   # 标题
    H2 = 90   # 摘要
    RH = 40   # 行高
    SEC = 50  # 区块标题间隔
    tfont = _get_font(30)
    sfont = _get_font(24)
    afont = _get_font(20)
    nfont = _get_font(17)

    def sec_title(d, y, text):
        d.text((PAD, y), text, fill=(40, 40, 40), font=sfont)
        return y + 34

    # ---- 行内容高度估算（先收集各区块行数）----
    nv = min(8, len(report.get('top_verses', [])))
    na = min(8, len(report.get('top_authors', [])))
    dyn = report.get('dynasties', [])
    wl = report.get('word_len', [])
    tc = report.get('top_chars', [])[:10]
    H = (PAD + H1 + H2 + SEC
         + (34 + nv * RH if nv else 0)
         + SEC + (34 + na * RH if na else 0)
         + SEC + (34 + len(dyn) * RH if dyn else 0)
         + SEC + (34 + (len(wl) or 1) * RH)
         + SEC + (34 + ((len(tc) + 4) // 5) * RH)
         + PAD)
    img = Image.new('RGB', (W, H), (250, 250, 252))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, H], fill=(250, 250, 252))

    # 标题
    d.text((W // 2, 16), f"诗句积累分析报表", fill=(30, 30, 30), font=_get_font(34), anchor='mt')
    d.text((W // 2, 52), f"{report.get('uname','')} · 生成于" , fill=(120,120,120), font=nfont, anchor='mt')

    y = PAD + H1
    # 摘要格子
    import time as _t
    now = _t.strftime('%Y-%m-%d %H:%M')
    d.text((W // 2, 66), f"{report.get('uname','')} · {now}", fill=(120,120,120), font=nfont, anchor='mt')
    y = PAD + H1
    metrics = [
        ('总单句', report.get('total', 0)),
        ('总使用', report.get('uses', 0)),
        ('重复句', report.get('dup', 0)),
        ('未能识别', report.get('unknown', 0)),
    ]
    cw = (W - PAD * 2) // len(metrics)
    for i, (lab, val) in enumerate(metrics):
        x = PAD + i * cw
        d.rounded_rectangle([x + 8, y + 10, x + cw - 8, y + H2 - 10], radius=10, fill=(255,255,255), outline=(210,210,215))
        d.text((x + cw // 2, y + 24), str(val), fill=(60, 120, 200), font=_get_font(30), anchor='mt')
        d.text((x + cw // 2, y + 60), lab, fill=(120,120,120), font=nfont, anchor='mt')
    y += H2 + SEC

    def bar_rows(d, y, items, maxv, color=(70, 130, 220), extra=None):
        for it in items:
            if hasattr(it, 'get'):
                label = it.get('label',''); val = it.get('value',0); sub = it.get('sub','')
            else:
                label, val = it; sub = extra(it) if extra else ''
            yy = y
            # 标签（左）
            d.text((PAD, yy + RH//2), label, fill=(40,40,40), font=afont, anchor='lm')
            # 条
            bw = int((W - PAD*2 - 180) * (val / maxv)) if maxv else 0
            bx = PAD + 170
            d.rounded_rectangle([bx, yy + 8, bx + max(3, bw), yy + RH - 8], radius=6, fill=color)
            d.text((PAD + 175, yy + RH//2), str(val), fill=(90,90,90), font=nfont, anchor='lm')
            if sub:
                d.text((W - PAD, yy + RH//2), sub, fill=(150,150,150), font=nfont, anchor='rm')
            y += RH
        return y

    # 常用诗句
    if nv:
        y = sec_title(d, y, '常用诗句 TOP')
        maxv = max((v.get('count',0) for v in report['top_verses']), default=1)
        for i, v in enumerate(report['top_verses'][:nv]):
            d.text((PAD, y + RH//2), f'{i+1}. {v.get("text","")}', fill=(30,30,30), font=afont, anchor='lm')
            bw = int((W-PAD*2-190) * (v.get('count',0)/maxv))
            bx = PAD + 185
            d.rounded_rectangle([bx, y+8, bx+max(3,bw), y+RH-8], radius=6, fill=(70,130,220))
            d.text((PAD+190, y+RH//2), str(v.get('count',0)), fill=(90,90,90), font=nfont, anchor='lm')
            d.text((W-PAD, y+RH//2), str(v.get('author','') or '未知'), fill=(150,150,150), font=nfont, anchor='rm')
            y += RH
        y += SEC

    # 常用诗人
    if na:
        y = sec_title(d, y, '常用诗人 TOP')
        maxv = max((a.get('count',0) for a in report['top_authors']), default=1)
        for i, a in enumerate(report['top_authors'][:na]):
            d.text((PAD, y + RH//2), f'{i+1}. {a.get("name","")}', fill=(30,30,30), font=afont, anchor='lm')
            bw = int((W-PAD*2-150) * (a.get('count',0)/maxv))
            bx = PAD + 145
            d.rounded_rectangle([bx, y+8, bx+max(3,bw), y+RH-8], radius=6, fill=(90,160,120))
            d.text((PAD+150, y+RH//2), str(a.get('count',0)), fill=(90,90,90), font=nfont, anchor='lm')
            y += RH
        y += SEC

    # 朝代占比
    if dyn:
        y = sec_title(d, y, '朝代占比')
        for name, cnt, pct in dyn:
            d.text((PAD, y+RH//2), name, fill=(40,40,40), font=afont, anchor='lm')
            bw = int((W-PAD*2-140) * pct / 100)
            d.rounded_rectangle([PAD+90, y+8, PAD+90+max(3,bw), y+RH-8], radius=6, fill=(220,150,80))
            d.text((PAD+95, y+RH//2), f'{pct}% ({cnt}句)', fill=(90,90,90), font=nfont, anchor='lm')
            y += RH
        y += SEC

    # 字数分布
    y = sec_title(d, y, '字数分布')
    if wl:
        maxw = max(w[1] for w in wl) or 1
        for label, cnt in wl:
            d.text((PAD, y+RH//2), label, fill=(40,40,40), font=afont, anchor='lm')
            bw = int((W-PAD*2-140) * cnt / maxw)
            d.rounded_rectangle([PAD+90, y+8, PAD+90+max(3,bw), y+RH-8], radius=6, fill=(150,120,200))
            d.text((PAD+95, y+RH//2), str(cnt), fill=(90,90,90), font=nfont, anchor='lm')
            y += RH
    else:
        d.text((PAD, y), '（无数据）', fill=(160,160,160), font=nfont)
        y += RH
    y += SEC

    # 常用字
    y = sec_title(d, y, '常用单字 TOP')
    if tc:
        cell = (W - PAD*2) // 5
        for idx, (ch, cnt) in enumerate(tc):
            cx = PAD + (idx % 5) * cell
            cy = y + (idx // 5) * 46
            d.text((cx + cell//2, cy), f'{ch} x{cnt}', fill=(80,80,80), font=afont, anchor='mm')
    else:
        d.text((PAD, y), '（无数据）', fill=(160,160,160), font=nfont)

    img.save(str(output_path), 'PNG')
    return output_path
