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

        return {"char": char, "initial": initial, "final": final, "tone": tone, "base": base}
    except Exception:
        return {"char": char, "initial": "", "final": "", "tone": 0, "base": char}


def decompose_text(text):
    """分解整句为字符部件列表。"""
    return [_decompose_char(ch) for ch in text]


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
    """猜诗句游戏引擎（单机，无存档）"""

    def __init__(self, db_source, max_attempts=10, min_len=5, max_len=10):
        self.db_source = db_source
        self.max_attempts = max_attempts
        self.min_len = min_len
        self.max_len = max_len
        self.target_text = None
        self.target_parts = None
        self.title = None
        self.author = None
        self.dynasty = None
        self.history = []  # [(guess_text, guess_parts, compare_result)]

    def new_game(self):
        """从数据库随机抽取一句 5-10 字诗句作为答案。返回 (ok, msg)。"""
        if not self.db_source:
            return False, "数据库不可用"
        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, "db_path", None)
        if not db_path or not os.path.exists(db_path):
            return False, "数据库未安装"

        candidates = self.db_source.get_random_verse(self.min_len, self.max_len, target_count=20)
        if not candidates:
            return False, "未找到合适的诗句"

        verse, title, author, dynasty = random.choice(candidates)
        self.target_text = verse
        self.target_parts = decompose_text(verse)
        self.title = title
        self.author = author
        self.dynasty = dynasty
        self.history = []
        return True, verse

    def guess(self, text):
        """处理一次猜测。返回 (ok, msg, compare_result, all_correct)。
        仅计入数据库中的完整诗句；不合规的猜测不计次数。"""
        clean = re.sub(r'[^\u4e00-\u9fa5]', '', text)
        if not clean:
            return False, "请输入汉字诗句", None, False
        if len(clean) > self.max_len:
            return False, f"诗句最长 {self.max_len} 字，当前 {len(clean)} 字", None, False
        if len(clean) < 2:
            return False, "请输入至少 2 个字", None, False

        # 合规校验：必须是数据库中的完整诗句，否则不计入猜测
        if not self._is_valid_poem_line(clean):
            return False, f"「{clean}」不是一句完整的古诗，请发送合规诗句（如：春眠不觉晓）", None, False

        guess_parts = decompose_text(clean)
        comp = compare_guess(guess_parts, self.target_parts)
        self.history.append((clean, guess_parts, comp))

        all_correct = all(
            c is not None and all(v == "correct" for v in c.values())
            for c in comp
        )
        return True, "", comp, all_correct

    def _is_valid_poem_line(self, clean):
        """校验是否为数据库中的完整单句。"""
        db = self.db_source
        if hasattr(db, "is_complete_sentence"):
            try:
                return db.is_complete_sentence(clean)
            except Exception:
                pass
        return False

    def is_finished(self):
        return len(self.history) >= self.max_attempts


# ============ 渲染 ============

CELL_W, CELL_H = 160, 185
GAP = 8
PAD = 40
HEADER_H = 85
FOOTER_H = 70

# (bg, text) — 更鲜明的对比色
COLOR_MAP = {
    "correct": ((76, 195, 84), (255, 255, 255)),      # 亮绿
    "present": ((255, 176, 32), (255, 255, 255)),     # 亮橙
    "absent": ((120, 120, 120), (255, 255, 255)),     # 深灰
    "empty": ((235, 235, 240), (180, 180, 185)),      # 浅空
    "default": ((245, 245, 250), (60, 60, 60)),
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


def render_grid(engine, output_path, title="猜诗句", max_attempts=10):
    """渲染游戏网格。列数 = 答案长度。"""
    n_cols = len(engine.target_parts)
    n_rows = max(1, len(engine.history))

    img_w = PAD * 2 + n_cols * (CELL_W + GAP) - GAP
    img_h = PAD + HEADER_H + n_rows * (CELL_H + GAP) + FOOTER_H

    img = Image.new("RGB", (img_w, img_h), (250, 250, 252))
    draw = ImageDraw.Draw(img)

    f_title = _get_font(30)
    f_char = _get_font(54)
    f_py = _get_font(26)
    f_py_lg = _get_font(30)
    f_hint = _get_font(15)

    draw.text((img_w // 2, 20), title, fill=(30, 30, 30), font=f_title, anchor="mt")

    y_start = PAD + HEADER_H

    for row_idx, (guess_word, guess_parts, comp_result) in enumerate(engine.history):
        y = y_start + row_idx * (CELL_H + GAP)
        for col_idx in range(n_cols):
            x = PAD + col_idx * (CELL_W + GAP)
            cell = comp_result[col_idx] if col_idx < len(comp_result) else None
            gp = guess_parts[col_idx] if col_idx < len(guess_parts) else None

            if cell is None:
                bg, _ = COLOR_MAP["empty"]
                draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=bg, outline=(210, 210, 215), width=1)
                continue

            bg, _ = COLOR_MAP.get(cell.get("char", "default"), COLOR_MAP["default"])
            draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=bg, outline=(200, 200, 210), width=1)

            ch = gp["char"] if gp else ""
            ch_color = COLOR_MAP.get(cell.get("char", "default"), (None, (60, 60, 60)))[1]
            draw.text((x + CELL_W // 2, y + CELL_H // 2 + 30), ch, fill=ch_color, font=f_char, anchor="mm")

            # 拼音部件（顶部，加大字号）
            py_y = y + 20
            if gp:
                initial = gp.get("initial", "")
                final = gp.get("final", "")
                tone = str(gp.get("tone", "")) if gp.get("tone", 0) > 0 else ""

                init_color = COLOR_MAP.get(cell.get("initial", "default"), (None, (90, 90, 90)))[1]
                final_color = COLOR_MAP.get(cell.get("final", "default"), (None, (90, 90, 90)))[1]
                tone_color = COLOR_MAP.get(cell.get("tone", "default"), (None, (90, 90, 90)))[1]

                if initial:
                    draw.text((x + 12, py_y), initial, fill=init_color, font=f_py, anchor="lt")
                if final:
                    draw.text((x + CELL_W // 2, py_y), final, fill=final_color, font=f_py_lg, anchor="mt")
                if tone:
                    draw.text((x + CELL_W - 12, py_y), tone, fill=tone_color, font=f_py, anchor="rt")

    # 底部：机会和答案长度提示
    remaining = max_attempts - len(engine.history)
    footer = f"剩余机会: {remaining} / {max_attempts}    答案 {n_cols} 字"
    draw.text((img_w // 2, img_h - 20), footer, fill=(120, 120, 120), font=f_hint, anchor="mb")

    img.save(output_path, "PNG")
    return output_path


def render_answer(engine, output_path):
    """渲染答案揭示图。"""
    n_cols = len(engine.target_parts)
    cell_w = 110
    w = PAD * 2 + n_cols * cell_w
    h = 200
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_sm = _get_font(18)
    f_char = _get_font(52)
    f_info = _get_font(16)

    draw.text((w // 2, 18), "正确诗句", fill=(40, 40, 40), font=f_sm, anchor="mt")

    for i, part in enumerate(engine.target_parts):
        x = PAD + i * cell_w + cell_w // 2
        draw.text((x, 95), part["char"], fill=(24, 144, 255), font=f_char, anchor="mm")

    info = f"《{engine.title}》 [{engine.dynasty}] {engine.author}"
    draw.text((w // 2, 170), info, fill=(80, 80, 80), font=f_info, anchor="mm")

    img.save(output_path, "PNG")
    return output_path
