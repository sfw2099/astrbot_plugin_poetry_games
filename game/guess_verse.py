"""猜诗句 - 参考猜成语的 Wordle 风格诗句猜测游戏引擎。"""

import os
import re
import random
from PIL import Image, ImageDraw, ImageFont

# pypinyin 惰性加载
_py_available = False


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
    "v": "ü", "ve": "üe", "vn": "ün",   # ü 系列
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


class GuessVerseEngine:
    """猜诗句游戏引擎（单机，无存档）—— 完整句含标点"""

    def __init__(self, db_source, max_attempts=10, min_len=7, max_len=18, classic_poems=None):
        self.db_source = db_source
        self.max_attempts = max_attempts
        self.min_len = min_len
        self.max_len = max_len
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

    def new_game(self):
        """从经典曲库随机抽取一句完整句（含标点）作为答案。返回 (ok, msg)。"""
        classic = self._random_classic_verse()
        if classic:
            sent, poem = classic
            self._set_target(sent, poem)
            return True, sent

        # 回退：全库随机抽取（无标点，仅汉字，尽量不触发）
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

    def guess(self, text):
        """处理一次猜测。返回 (ok, msg, compare_result, all_correct)。
        输入为含标点的完整句。标点需与答案匹配，汉字做 Wordle 比较。"""
        text = text.strip()
        clean_hanzi = extract_hanzi(text)
        if not clean_hanzi:
            return False, "请输入汉字诗句（含标点）", None, False
        if len(clean_hanzi) > self.max_len:
            return False, f"诗句最长 {self.max_len} 字（不含标点），当前 {len(clean_hanzi)} 字", None, False
        if len(clean_hanzi) < self.min_len:
            return False, f"诗句最短 {self.min_len} 字（不含标点），当前 {len(clean_hanzi)} 字", None, False

        # 标点匹配校验
        guess_punct = extract_punct(text)
        if guess_punct != self.target_punct:
            return False, "标点位置或类型不匹配，请按格式输入（如：床前明月光，疑是地上霜。）", None, False

        # 合规校验：输入句须在曲库或总库中
        if not self._is_valid_poem_line(text, clean_hanzi):
            return False, f"「{text}」不是一句完整的古诗，请发送合规诗句", None, False

        guess_parts = decompose_text(clean_hanzi)
        comp = compare_guess(guess_parts, self.target_parts)
        self.history.append((text, guess_parts, comp))
        self._record_pinyin_status(guess_parts, comp)

        all_correct = all(
            c is not None and all(v == "correct" for v in c.values())
            for c in comp
        )
        return True, "", comp, all_correct

    def _is_valid_poem_line(self, text, clean_hanzi):
        """校验是否为曲库或总库中的完整句。"""
        # 曲库按完整句匹配（含标点）
        if text in self._classic_sentences:
            return True
        # 回退：总库按汉字匹配
        db = self.db_source
        if hasattr(db, "is_complete_sentence"):
            try:
                if db.is_complete_sentence(clean_hanzi):
                    return True
            except Exception:
                pass
        return False

    def is_finished(self):
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
    """
    if not segments:
        return
    gaps = [seg[3] if len(seg) > 3 else 0 for seg in segments]
    total_w = sum(_text_width(draw, seg[0], seg[2]) for seg in segments) + sum(gaps)
    cur_x = x_center - total_w // 2
    for i, seg in enumerate(segments):
        text, color, font = seg[0], seg[1], seg[2]
        cur_x += gaps[i]
        draw.text((cur_x, y_mid), text, fill=color, font=font, anchor="lm")
        cur_x += _text_width(draw, text, font)


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


def render_grid(engine, output_path, title="猜诗句", max_attempts=10):
    """渲染游戏网格。列 = 汉字列 + 标点列。"""
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
    f_py = _get_font(30)
    f_hint = _get_font(15)
    f_punct = _get_font(36)

    draw.text((img_w // 2, 20), title, fill=(30, 30, 30), font=f_title, anchor="mt")

    y_start = PAD + HEADER_H

    # 预计算每列 x 坐标
    col_x = []
    cx = PAD
    for i, w in enumerate(col_widths):
        col_x.append(cx)
        cx += w + GAP

    for row_idx, (guess_word, guess_parts, comp_result) in enumerate(engine.history):
        y = y_start + row_idx * (CELL_H + GAP)
        hanzi_pos = 0
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
                _draw_pinyin_joined(draw, x + CELL_W // 2, py_mid, segments)

    # 底部
    remaining = max_attempts - len(engine.history)
    footer = f"剩余机会: {remaining} / {max_attempts}    答案 {len(engine.target_hanzi)} 字（含标点）"
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

    footer = f"共 {len(engine.target_hanzi)} 字，按此格式输入含标点的完整句"
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
