import os
import re
import asyncio
import aiohttp  # noqa: F401 - used in _install_db at runtime
import json
import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.all import Plain, Image, MessageChain
import astrbot.api.message_components as Comp

from .database import PoetryDB
from .game.flowing_petals import FlowingPetalsEngine
from .game.crossword_poetry import PoetryCrosswordEngine
from .game.snake_poetry import PoetrySnakeEngine
from .game.guess_verse import GuessVerseEngine, render_grid, render_blank, render_answer, render_hint, _init_plugin_dir
from .game.guess_verse import pick_battle_target, BattleVerseEngine, render_battle
from .game.guess_verse import DuelVerseEngine, render_duel, pick_puzzle_verse
from .game.guess_verse import extract_hanzi, extract_punct
from .game.guess_verse import INITIALS_LIST, FINALS_LIST
from .player_data import PlayerManager, ACHIEVEMENTS
from .game.items import ITEMS, roll_win_item, roll_loser_item
from .game.base_game import BOT_ID
from .game.ai_bot import BotPlayer

GITEE_BASE = "https://gitee.com/alin1031/poetry-data/releases/download/v1.0.0/poetry_data.zip"
GITEE_PROBE = GITEE_BASE + ".part01"  # 探测分片而非基文件（基文件不存在）
GITEE_PARTS = 4

GITHUB_ZIP = "https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/download/data-v3.0.0/poetry_data.zip"

PROXY_SOURCES = [
    # (probe_url, download_url, label)
    (GITEE_PROBE, "GITEE",   "Gitee 分片"),
    (GITHUB_ZIP,  GITHUB_ZIP,  "GitHub 直链"),
    ("https://gh.llkk.cc/" + GITHUB_ZIP, "https://gh.llkk.cc/" + GITHUB_ZIP, "gh.llkk.cc"),
    ("https://gh.ddlc.top/" + GITHUB_ZIP, "https://gh.ddlc.top/" + GITHUB_ZIP, "gh.ddlc.top"),
    ("https://ghproxy.net/" + GITHUB_ZIP, "https://ghproxy.net/" + GITHUB_ZIP, "ghproxy.net"),
]

PROBE_TIMEOUT = 12


@register("astrbot_plugin_poetry_games", "ALin", "诗词游戏引擎", "3.5.0")
class PoetryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_games")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.plugin_data_dir / 'poetry_data.db'

        self.saves_dir = self.plugin_data_dir / 'saves'
        self.saves_dir.mkdir(parents=True, exist_ok=True)

        # 玩家个人数据目录
        self.players_dir = self.plugin_data_dir / 'players'
        self.players_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PlayerManager(str(self.players_dir))

        self.db = None
        self.active_games = {}
        self.timeout_tasks = {}
        self.flowing_timeout = self.config.get("flowing_timeout", 90)
        self.crossword_timeout = self.config.get("crossword_timeout", 90)
        self.snake_timeout = self.config.get("snake_timeout", 120)
        self.verse_max_attempts = self.config.get("verse_max_attempts", 10)
        self.verse_min_len = self.config.get("verse_min_len", 5)
        self.verse_max_len = self.config.get("verse_max_len", 10)
        self.guess_verse_sessions = {}
        self.battle_sessions = {}  # 邀战对战会话
        self.duel_sessions = {}    # 诗词对垒会话
        # AI bot 玩家
        self.ai_bot = BotPlayer(
            self,
            enabled=self.config.get("ai_bot_enabled", False),
            cooldown=self.config.get("ai_bot_cooldown", 10),
            puzzle_ai_ratio=self.config.get("ai_bot_puzzle_ai_ratio", 50),
        )
        self._ai_bot_trigger_words = set()
        for w in str(self.config.get("ai_bot_trigger_words", "bot猜,帮忙猜,让bot猜")).split(","):
            w = w.strip()
            if w:
                self._ai_bot_trigger_words.add(w)
        self._ai_bot_cooldown_until = 0.0  # 猜诗句 bot 冷却截止时间戳
        try:
            _init_plugin_dir(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass

        # 加载经典诗词曲库（随插件分发）
        self.plugin_code_dir = os.path.dirname(os.path.abspath(__file__))
        self.classic_poems = self._load_classic_poems()
        # 教材/经典半句集合（邀战猜测用，命中才当猜测）
        self.battle_half_set = self._build_battle_half_set()
        # 经典单句集合（4-7字，对垒出题/猜测用）
        self.classic_clause_set = self._build_classic_clause_set()

    def _build_classic_clause_set(self):
        """构建经典单句集合：所有完整句按逗号/顿号拆出的 4-7 字单句。"""
        clause_set = set()
        for p in self.classic_poems:
            sent = p.get("sentence", "")
            for clause in re.split(r'[，、]', sent):
                pure = re.sub(r'[^\u4e00-\u9fff]', '', clause)
                if 4 <= len(pure) <= 7:
                    clause_set.add(pure)
        return clause_set

    def _build_battle_half_set(self):
        """构建邀战可猜的半句集合：所有经典曲库完整句拆出的前句+后句。"""
        half_set = set()
        for p in self.classic_poems:
            sent = p.get("sentence", "")
            m = re.match(r'^([\u4e00-\u9fff]{4,6})，([\u4e00-\u9fff]{4,6})[。！？]$', sent)
            if m:
                half_set.add(m.group(1))
                half_set.add(m.group(2))
        return half_set

    def _load_classic_poems(self):
        """加载经典诗词曲库。优先 classic_school.json（课本+名句），回退 classic_poems.json。"""
        import json
        # 优先「课本+名句」聚焦库
        for name in ("classic_school.json", "classic_poems.json"):
            path = os.path.join(self.plugin_code_dir, name)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    logger.info(f"[guess_verse] 曲库 {name} 加载成功: {len(data)} 条")
                    return data
                except Exception as e:
                    logger.error(f"[guess_verse] 曲库 {name} 加载失败: {e}")
        logger.warning("[guess_verse] 未找到曲库文件")
        return []

    def _ensure_db(self):
        """惰性加载数据库"""
        if self.db is not None:
            return True
        if self.db_file.exists() and self.db_file.stat().st_size > 0:
            try:
                self.db = PoetryDB(str(self.db_file))
                return True
            except Exception:
                os.remove(str(self.db_file))
        return False

    # ==========================================
    # 🔽 安装数据库指令
    # ==========================================
    @filter.command("安装数据库")
    async def _install_db(self, event: AstrMessageEvent):
        if self._ensure_db():
            db_size_mb = os.path.getsize(str(self.db_file)) / (1024 * 1024)
            yield event.plain_result(f"✅ 数据库已就绪 ({db_size_mb:.0f} MB)，无需重复安装。")
            return

        yield event.plain_result("🔍 正在探测下载源...")

        candidates = []
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for probe_url, dl_url, label in PROXY_SOURCES:
                t0 = time.monotonic()
                try:
                    async with session.head(probe_url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT),
                                             allow_redirects=True) as resp:
                        if resp.status == 200:
                            elapsed = time.monotonic() - t0
                            clen = int(resp.headers.get('Content-Length', 0))
                            candidates.append((dl_url, elapsed, clen, label))
                except Exception:
                    pass

        if not candidates:
            yield event.plain_result(
                "❌ 所有下载源均不可达。\n\n"
                "📥 请手动下载：\n"
                f"  Gitee: https://gitee.com/alin1031/poetry-data/releases\n"
                f"  GitHub: {GITHUB_ZIP}\n"
                "解压后放入: " + str(self.plugin_data_dir)
            )
            return

        candidates.sort(key=lambda x: x[1])
        lines = ["📡 下载源测速结果："]
        for i, (dl_url, elapsed, clen, label) in enumerate(candidates):
            mb = clen / (1024 * 1024) if clen > 0 else 0
            sz = f"{mb:.0f}MB" if mb > 0 else "?"
            lines.append(f"  {i+1}. {elapsed:.1f}s  {sz}  {label}")
        yield event.plain_result("\n".join(lines))

        best_dl_url, best_elapsed, _, best_label = candidates[0]
        yield event.plain_result(f"⬇️ 选用 {best_label} ({best_elapsed:.1f}s)，开始下载...")

        # ---- download ----
        try:
            if best_dl_url == "GITEE":
                async for msg in self._download_gitee(event):
                    yield msg
            else:
                async for msg in self._download_zip(event, best_dl_url):
                    yield msg

            self.db = PoetryDB(str(self.db_file))
            db_size_mb = os.path.getsize(str(self.db_file)) / (1024 * 1024)
            yield event.plain_result(f"✅ 数据库安装完成 ({db_size_mb:.0f} MB)，可以开始游戏了！")
        except Exception as e:
            logger.error(f"下载失败: {e}")
            yield event.plain_result(f"❌ 下载失败: {e}\n请手动下载: {GITHUB_ZIP}")

    async def _download_gitee(self, event: AstrMessageEvent):
        """下载 Gitee 4 个分片，流式写入磁盘后解压（节省内存）"""
        import zipfile
        tmp_zip = str(self.plugin_data_dir / '_poetry_data_tmp.zip')
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for i in range(1, GITEE_PARTS + 1):
                part_url = f"{GITEE_BASE}.part{i:02d}"
                yield event.plain_result(f"  [{i}/{GITEE_PARTS}] 下载中...")
                t0 = time.monotonic()
                async with session.get(part_url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                    if resp.status != 200:
                        raise Exception(f"分片 {i} HTTP {resp.status}")
                    with open(tmp_zip, 'ab' if i > 1 else 'wb') as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
                mb = os.path.getsize(tmp_zip) / (1024 * 1024)
                elapsed = time.monotonic() - t0
                yield event.plain_result(f"  [{i}/{GITEE_PARTS}] ✓ {mb:.0f}MB 累计 ({elapsed:.0f}s)")

        yield event.plain_result("📦 正在解压...")
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(str(self.plugin_data_dir))
        os.remove(tmp_zip)

    async def _download_zip(self, event: AstrMessageEvent, url):
        """下载单个 zip 文件，流式写入磁盘后解压（节省内存）"""
        import zipfile
        tmp_zip = str(self.plugin_data_dir / '_poetry_data_tmp.zip')
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                total_size = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                last_report_time = time.monotonic()

                with open(tmp_zip, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0 and (time.monotonic() - last_report_time) >= 5:
                            pct = int(downloaded / total_size * 100)
                            yield event.plain_result(
                                f"  ⏳ {pct}% ({downloaded/(1024*1024):.0f}/{total_size/(1024*1024):.0f} MB)")
                            last_report_time = time.monotonic()

                if total_size > 0 and downloaded < total_size * 0.9:
                    os.remove(tmp_zip)
                    raise Exception("下载不完整")

        yield event.plain_result("📦 正在解压...")
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(str(self.plugin_data_dir))
        os.remove(tmp_zip)

    # ==========================================
    # 🌟 核心修复：多存档列表获取助手
    # ==========================================
    def get_saves(self, session_id):
        saves = []
        if not os.path.exists(str(self.saves_dir)): return saves

        for f in os.listdir(str(self.saves_dir)):
            if f.startswith(f"game_{session_id}_") and f.endswith(".json"):
                path = os.path.join(str(self.saves_dir), f)
                try:
                    with open(path, 'r', encoding='utf-8') as file:
                        state = json.load(file)
                        saves.append({
                            "filename": f,
                            "path": path,
                            "type": state.get("game_type", "未知"),
                            "start_time": state.get("start_time", "未知 (旧版存档)"),
                            "turn_count": state.get("turn_count", 0),
                            "mtime": os.path.getmtime(path)
                        })
                except: pass
        saves.sort(key=lambda x: x["mtime"], reverse=True)
        return saves

    # ==========================================
    # 基础信息检索
    # ==========================================
    @filter.command("查询诗句")
    async def find_sentence(self, event: AstrMessageEvent, sentence: str):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        results = self.db.search_by_sentence(sentence)
        exact_list = results.get("exact", [])
        fuzzy_list = results.get("fuzzy", [])
        if not exact_list and not fuzzy_list:
            yield event.plain_result(f"未找到包含「{sentence}」的诗词。")
            return
        resp = [f"📖 查询结果：【{sentence}】\n" + "="*15]
        if exact_list:
            resp.append("🎯 [完全一致的单句]：")
            for title, author, dynasty in exact_list:
                resp.append(f" • [{dynasty}] {author} —— 《{title}》")
            resp.append("")
        if fuzzy_list:
            resp.append("🔍 [包含该片段的诗词] (模糊匹配)：")
            for title, author, dynasty in fuzzy_list:
                resp.append(f" • [{dynasty}] {author} —— 《{title}》")
        yield event.plain_result("\n".join(resp).strip())

    @filter.command("查询诗词")
    async def find_full_poem(self, event: AstrMessageEvent, title_kw: str, author_kw: str = ""):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        results = self.db.get_poem_by_title(title_kw, author_kw)
        if not results:
            if author_kw:
                yield event.plain_result(f"未找到标题包含「{title_kw}」，且作者包含「{author_kw}」的诗词。")
            else:
                yield event.plain_result(f"未找到标题包含「{title_kw}」的诗词。")
            return
        MAX_DISPLAY = 3
        total_count = len(results)
        display_results = results[:MAX_DISPLAY]
        resp = [f"检索到 {total_count} 首相关诗词" + (f"（仅展示前 {MAX_DISPLAY} 首）" if total_count > MAX_DISPLAY else "") + "：\n" + "="*20]
        for i, (title, author, dynasty, content) in enumerate(display_results):
            clean_content = content.replace('\r\n', '\n').strip()
            resp.append(f"《{title}》\n作者：[{dynasty}] {author}\n\n{clean_content}")
            if i < len(display_results) - 1: resp.append("-" * 15)
        if total_count > MAX_DISPLAY:
            resp.append(f"\n...\n(搜索结果过多，为防刷屏已截断。请加上作者名精确查询，如：/查询诗词 {title_kw} 纳兰性德)")
        yield event.plain_result("\n".join(resp))

    # ==========================================
    # 游戏建局指令 (动态生成新存档)
    # ==========================================
    @filter.command("衔字飞花令")
    async def start_flowing(self, event: AstrMessageEvent):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！请先【结束游戏】")
            return
        engine = FlowingPetalsEngine(session_id, self.db, str(self.saves_dir), timeout_seconds=self.flowing_timeout)
        self.active_games[session_id] = engine
        if session_id in self.timeout_tasks: self.timeout_tasks[session_id].cancel()
        self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
        yield event.plain_result(f"🌸 【衔字飞花令】已建立新对局！\n限时：{self.flowing_timeout}秒。第一位发送【加入】的玩家即可开始。")

    @filter.command("纵横飞花令")
    async def start_crossword(self, event: AstrMessageEvent, width: int = 24, height: int = 24):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        if not (8 <= width <= 40) or not (8 <= height <= 40):
            yield event.plain_result("📐 棋盘宽和高必须在 8 到 40 之间！")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！请先【结束游戏】")
            return
        engine = PoetryCrosswordEngine(session_id, self.db, str(self.saves_dir), width=width, height=height, timeout_seconds=self.crossword_timeout)
        self.active_games[session_id] = engine
        if session_id in self.timeout_tasks: self.timeout_tasks[session_id].cancel()
        self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
        start_verse_info = engine.state["history"][0] if engine.state["history"] else "随机开局"
        yield event.plain_result(f"🌟 【纵横飞花令】已建立新对局！({width}x{height}棋盘，限时{self.crossword_timeout}秒)\n系统已随机落下首句：{start_verse_info}\n请发送【加入】参与。")
        if hasattr(engine, "render_image"):
            yield event.image_result(engine.render_image())

    @filter.command("蛇形飞花令")
    async def start_snake(self, event: AstrMessageEvent, width: int = 40, height: int = 40):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        if not (20 <= width <= 60) or not (20 <= height <= 60):
            yield event.plain_result("📐 棋盘宽和高必须在 20 到 60 之间！")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！请先【结束游戏】")
            return
        engine = PoetrySnakeEngine(session_id, self.db, str(self.saves_dir), width=width, height=height, timeout_seconds=self.snake_timeout)
        self.active_games[session_id] = engine
        if session_id in self.timeout_tasks: self.timeout_tasks[session_id].cancel()
        self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
        yield event.plain_result(f"🐍 【蛇形飞花令】已建立新对局！({width}x{height}棋盘，限时{self.snake_timeout}秒)\n请发送【加入】参与。")
        if hasattr(engine, "render_image"):
            yield event.image_result(engine.render_image())

    # ==========================================
    # 🎯 猜诗句游戏
    # ==========================================
    @filter.command("猜诗句")
    async def start_guess_verse(self, event: AstrMessageEvent):
        if not self._ensure_db():
            yield event.plain_result("⏳ 数据库未安装，请发送 /安装数据库")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.guess_verse_sessions:
            yield event.plain_result("游戏进行中！发送诗句进行猜测，或发送【结束猜诗句】退出。")
            return

        # ===== 解析参数：格式(4/5/6/7 或 44/34/43/55/77) / 提示方式(声|形)，可组合任意顺序 =====
        fmt = None
        hint_mode = None
        msg_text = str(event.get_message_str() or "")
        remainder = re.sub(r"^[/／]?\s*猜诗句", "", msg_text, flags=re.IGNORECASE)
        remainder = re.sub(r"\s+", " ", remainder).strip()
        for token in remainder.split(" "):
            if not token:
                continue
            parsed = self._parse_poem_format(token)
            if parsed:
                if fmt is not None:
                    yield event.plain_result("❌ 只能指定一种格式（4/5/6/7 单句 或 44/34/43/55/77 两句）。\n用法：/猜诗句 [格式] [声|形]")
                    return
                fmt = parsed
            elif token in ("声", "形", "拼音", "部首"):
                if hint_mode is not None:
                    yield event.plain_result("❌ 只能指定一种提示方式（声=拼音 / 形=部首）。\n用法：/猜诗句 [格式] [声|形]")
                    return
                hint_mode = "pinyin" if token in ("声", "拼音") else "radical"
            else:
                yield event.plain_result(
                    f"❌ 无效参数「{token}」。\n用法：/猜诗句 [格式] [声|形]\n"
                    f"格式：4/5/6/7 单句，或 44/34/43/55/77 两句\n"
                    f"例如：/猜诗句 4 声 ｜ /猜诗句 55 ｜ /猜诗句 形 7"
                )
                return

        # 未指定格式 -> 随机单句：4字10% / 6字10% / 5、7字各40%
        if fmt is None:
            import random as _r
            roll = _r.random()
            if roll < 0.10:
                fmt = ("single", 4)
            elif roll < 0.20:
                fmt = ("single", 6)
            elif roll < 0.60:
                fmt = ("single", 5)
            else:
                fmt = ("single", 7)

        # 未指定提示方式 -> 随机：声70% / 形30%
        if hint_mode is None:
            import random as _r2
            hint_mode = "pinyin" if _r2.random() < 0.70 else "radical"

        engine = GuessVerseEngine(self.db, None, 4, 7,
                                  classic_poems=self.classic_poems, hint_mode=hint_mode)
        if fmt[0] == "single":
            ok, msg = engine.new_game(word_len=fmt[1])
        else:
            ok, msg = engine.new_game(combo=fmt[1])
        if not ok:
            yield event.plain_result(f"❌ 初始化失败：{msg}")
            return

        self.guess_verse_sessions[session_id] = engine
        hint_label = "拼音" if hint_mode == "pinyin" else "部首"
        yield event.plain_result(
            "🎯 【猜诗句】开始！\n"
            f"答案格式：{self._format_desc(fmt)}，提示方式：{hint_label}。\n"
            "发送「cc 诗句」进行猜测（两句需带标点），如：cc 离离原上草，一岁一枯荣\n"
            "每次猜测后，每个字的【汉字/声母/韵母/声调】独立着色（拼音模式）：\n"
            "🟢 绿色 = 正确且位置正确\n"
            "🟠 橙色 = 答案中存在但位置错误\n"
            "⚪ 灰色 = 答案中不存在\n"
            "部首模式下用字色+边框颜色提示。"
        )
        # 发送空白框占位图
        blank_path = os.path.join(str(self.plugin_data_dir), f"verse_blank_{session_id}.png")
        render_blank(engine, blank_path)
        yield event.image_result(blank_path)

    @filter.command("结束猜诗句")
    async def end_guess_verse(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.guess_verse_sessions:
            engine = self.guess_verse_sessions.pop(session_id)
            ans_path = os.path.join(str(self.plugin_data_dir), f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            yield event.image_result(ans_path)
            yield event.plain_result(f"游戏结束，正确诗句：{engine.target_text}")
        else:
            yield event.plain_result("当前没有进行中的猜诗句游戏。")

    @filter.command("猜诗句帮助")
    async def guess_verse_help(self, event: AstrMessageEvent):
        msg = (
            "🎯 【猜诗句】规则说明\n"
            "--------------------\n"
            "1. 系统从总库随机选择一句 4-7 字的单句作为答案。\n"
            "2. 发送「cc 诗句」进行猜测（cc 后跟与答案字数一致的诗句）。\n"
            "3. 每次猜测后，每个字的【汉字/声母/韵母/声调】独立着色（拼音模式）。\n"
            "4. 部首模式：字色 + 边框颜色提示。\n"
            "5. 全部绿色即猜中！\n"
            "指令：/猜诗句 [4|5|6|7] [声|形] 开始 ｜ /结束猜诗句 退出\n"
            "例：/猜诗句 4 声 ｜ /猜诗句 形 ｜ /猜诗句 5"
        )
        yield event.plain_result(msg)

    @filter.command("我的诗句")
    async def my_verses(self, event: AstrMessageEvent):
        """查看个人积累诗句（渲染成图片）。"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        verses = self.pm.get_verses(uid)
        total = len(verses)
        if total == 0:
            yield event.plain_result(f"{uname} 还没有积累任何诗句，快去参与猜诗句/诗词对垒吧！")
            return
        from .game.guess_verse import render_verse_list
        img_path = os.path.join(str(self.plugin_data_dir), f"my_verses_{uid}.png")
        render_verse_list(uid, uname, verses, img_path)
        yield event.plain_result(f"📚 {uname} 已积累 {total} 句诗：")
        yield event.image_result(img_path)

    def _build_clause_meta(self):
        """构建经典曲库单句 → (author, dynasty) 索引（惰性）。"""
        if getattr(self, "_clause_meta_cache", None) is None:
            idx = {}
            for p in self.classic_poems or []:
                author = (p.get("author") or "").strip()
                dynasty = (p.get("dynasty") or "").strip()
                for cl in re.split(r"[，。！？、；：]", (p.get("sentence") or "")):
                    h = re.sub(r"[^\u4e00-\u9fff]", "", cl)
                    if h and h not in idx:
                        idx[h] = (author, dynasty)
            self._clause_meta_cache = idx
        return self._clause_meta_cache

    def _verse_meta(self, clause):
        """返回单句的 (author, dynasty)；经典曲库优先，其次查总库，未知则 (None,None)。"""
        m = self._build_clause_meta()
        if clause in m:
            return m[clause]
        # 总库查询（对无法在曲库识别的句子尝试一次）
        try:
            if self.db is not None:
                r = self.db.check_exact_poetry(clause)
                if r:
                    return r[1], r[2]
        except Exception:
            pass
        return None, None

    def _collect_verse_report(self, uid, uname):
        """同步收集诗句报表数据（放线程执行避免阻塞）。"""
        verses = self.pm.get_verses(uid)
        if not verses:
            return None
        total = len(verses)
        uses = sum(v.get("count", 0) for v in verses.values())
        dup = sum(1 for v in verses.values() if v.get("count", 0) > 1)
        author_count = {}   # 作者 -> 使用频次
        dynasty_count = {}  # 朝代 -> 使用频次
        verse_meta = {}
        unknown = 0
        wordlen = {}
        char_count = {}
        # 优先解析使用频次最高的前 60 句出处（控制总库查询量）
        top_items = sorted(verses.items(), key=lambda kv: -kv[1].get("count", 0))[:60]
        known = {}
        for clause, info in top_items:
            author, dynasty = self._verse_meta(clause)
            known[clause] = (author, dynasty)
        for clause, info in verses.items():
            c = info.get("count", 0)
            L = len(clause)
            wordlen[L] = wordlen.get(L, 0) + c
            for ch in clause:
                if "\u4e00" <= ch <= "\u9fff":
                    char_count[ch] = char_count.get(ch, 0) + 1
            meta = known.get(clause)
            if not meta or not meta[0] and not meta[1]:
                unknown += 1
                verse_meta[clause] = ("未知", "未知")
                continue
            author, dynasty = meta
            author = author or "佚名"
            dynasty = dynasty or "未知"
            author_count[author] = author_count.get(author, 0) + c
            dynasty_count[dynasty] = dynasty_count.get(dynasty, 0) + c
            verse_meta[clause] = (author, dynasty)
        # 常用诗句 TOP（附带作者）
        top_verses = []
        for clause, info in sorted(verses.items(), key=lambda kv: -kv[1].get("count", 0))[:8]:
            author, _ = verse_meta.get(clause, ("未知", "未知"))
            top_verses.append({"text": clause, "count": info.get("count", 0), "author": author})
        # 常用诗人 TOP（排除佚名/未知）
        top_authors = [
            {"name": k, "count": v}
            for k, v in sorted(author_count.items(), key=lambda kv: -kv[1])
            if k not in ("佚名", "未知")
        ][:8]
        # 朝代占比
        total_known = sum(dynasty_count.values())
        dynasties = []
        for name, cnt in sorted(dynasty_count.items(), key=lambda kv: -kv[1]):
            pct = int(cnt * 100 / total_known) if total_known else 0
            dynasties.append((name, cnt, pct))
        # 字数分布标签
        wl = []
        for L, cnt in sorted(wordlen.items()):
            label = {4: "四言", 5: "五言", 6: "六言", 7: "七言"}.get(L, f"{L}字")
            wl.append((label, cnt))
        top_chars = sorted(char_count.items(), key=lambda kv: -kv[1])[:10]
        return {
            "uname": uname, "total": total, "uses": uses, "dup": dup, "unknown": unknown,
            "top_verses": top_verses, "top_authors": top_authors,
            "dynasties": dynasties, "word_len": wl, "top_chars": top_chars,
        }

    @filter.command("诗句报表")
    async def verse_report(self, event: AstrMessageEvent):
        """生成个人诗句积累分析报表。"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        if not self.pm.get_verses(uid):
            yield event.plain_result(f"{uname} 还没有积累任何诗句，快去参与猜诗句/诗词对垒吧！")
            return
        report = await asyncio.to_thread(self._collect_verse_report, uid, uname)
        if not report:
            yield event.plain_result("报表生成失败。")
            return
        from .game.guess_verse import render_poetry_report
        img_path = os.path.join(str(self.plugin_data_dir), f"verse_report_{uid}.png")
        render_poetry_report(report, img_path)
        yield event.image_result(img_path)

    @filter.command("我的诗词道具")
    async def my_items(self, event: AstrMessageEvent):
        """查看我的诗词道具数量。"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        inv = self.pm.get_items(uid, uname)
        owned = {k: v for k, v in inv.items() if v and v > 0}
        if not owned:
            yield event.plain_result(f"{uname} 还没有道具。每局猜诗句/对垒结束，胜者有概率获得道具，败者也有机会。")
            return
        lines = [f"🎒 {uname} 的道具："]
        for k, v in sorted(owned.items(), key=lambda kv: -kv[1]):
            desc = ITEMS.get(k, {}).get("desc", "")
            lines.append(f"· {k} x{v}：{desc}")
        yield event.plain_result("\n".join(lines))

    @filter.command("诗词道具")
    async def use_item(self, event: AstrMessageEvent, item: str = "", n: str = ""):
        """使用诗词道具：/诗词道具 道具名 [数量] [额外参数，如定仙游的字或@玩家]"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        item = (item or "").strip()
        if not item or item not in ITEMS:
            yield event.plain_result(
                "未知道具。可用道具：\n"
                + "\n".join(f"· {k}：{v['desc']}" for k, v in ITEMS.items())
            )
            return
        count = 1
        try:
            count = max(1, min(int(n or "1"), 10))
        except ValueError:
            count = 1
        # 解析剩余参数（@目标 / 定仙游汉字）
        raw = str(event.get_message_str() or "").strip()
        tail = raw
        tail = re.sub(r"^[/／]?\s*诗词道具\s*", "", tail, flags=re.IGNORECASE)
        tail = re.sub(r"^" + re.escape(item) + r"\s*", "", tail).strip()
        mnum = re.match(r"^(\d+)\s*", tail)
        if mnum:
            tail = tail[mnum.end():].strip()
        at_id = self._extract_at_id(event)
        # 校验数量
        if self.pm.item_count(uid, item, uname) < count:
            yield event.plain_result(f"道具【{item}】数量不足（持有 {self.pm.item_count(uid, item, uname)} 个）。")
            return
        # 场景判定
        result = await self._do_use_item(event, uid, uname, item, count, tail, at_id)
        if isinstance(result, str):
            yield event.plain_result(result)
            return
        async for m in result:
            yield m

    async def _do_use_item(self, event, uid, uname, item, count, tail, at_id):
        """按道具分发。返回 str 或 async generator。"""
        from .game.items import ITEMS as _I
        # 需在游戏内使用的道具：定位当前会话
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.guess_verse_sessions.get(session_id)
        duel_sid = None
        duel = None
        for k, d in self.duel_sessions.items():
            if session_id == k or uid in (d.get("challenger_id"), d.get("opponent_id")):
                duel, duel_sid = d, k
                break
        # 火眼金睛 / 三仙归洞 / 仙人指路（提示类，可对猜诗句或对垒目标）
        if item in ("火眼金睛", "三仙归洞", "仙人指路"):
            target_parts = None
            target_text = None
            target_author = None
            if engine is not None and getattr(engine, "target_parts", None):
                target_parts = engine.target_parts
                target_text = getattr(engine, "target_hanzi", "")
                target_author = getattr(engine, "author", "")
            elif duel is not None and duel.get("engine"):
                de = duel["engine"]
                # 当前轮到谁，就提示谁要猜的目标
                cur = de.current_side()
                target_parts = de.target_parts_of(cur)
                target_text = de.a_target_hanzi if cur == "a" else de.b_target_hanzi
                # 作者：从题目元数据查
                try:
                    puzzle = duel.get("puzzles", {}).get(de.b_id if cur == "a" else de.a_id, "")
                    meta = self.db.check_exact_poetry(puzzle) if self.db else None
                    target_author = meta[1] if meta else ""
                except Exception:
                    target_author = ""
            if not target_parts:
                return "当前没有进行中的猜诗句/对垒游戏，无法使用该道具。"
            self.pm.consume_item(uid, item, count, uname)
            if item == "火眼金睛":
                out = []
                used = set()
                for _ in range(count):
                    idx = self._pick_random_index(target_text, used)
                    if idx is None:
                        break
                    used.add(idx)
                    ch = target_text[idx]
                    out.append(f"第{idx+1}个字是「{ch}」")
                return "🔍 火眼金睛：答案中 " + "，".join(out) if out else "答案较短，无法再揭示。"
            if item == "三仙归洞":
                out = []
                used = set()
                for _ in range(count):
                    res = self._pick_random_shengmu(target_parts, used)
                    if not res:
                        break
                    idx, sm, ym = res
                    used.add(idx)
                    out.append(f"第{idx+1}字 声母「{sm}」韵母「{ym}」")
                return "🎯 三仙归洞：" + "；".join(out) if out else "无法继续揭示。"
            # 仙人指路
            if target_author:
                return f"📜 仙人指路：当前答案作者是【{target_author}】"
            return "无法解析答案作者，请稍后再试。"
        # 定仙游：猜诗句换含字题
        if item == "定仙游":
            if engine is None:
                return "定仙游需在猜诗句进行中使用。"
            ch = (tail or "").strip()
            if not ch:
                return "用法：/诗词道具 定仙游 汉字"
            new_verse = self._pick_verse_with_char(engine, ch[0])
            if not new_verse:
                return f"未能找到同格式且含「{ch[0]}」的诗句，换一个试试。"
            # 保留引擎其余参数，仅重置目标
            poem = {"title": new_verse[1], "author": new_verse[2], "dynasty": new_verse[3]}
            engine._set_target(new_verse[0], poem)
            engine.participants = set()
            engine.user_guesses = {}
            engine.user_verses = set()
            engine.user_initials = {}
            engine.user_finals = {}
            self.pm.consume_item(uid, item, count, uname)
            return f"🔮 定仙游：题目已更换为一句含「{ch[0]}」的诗句：{new_verse[0]}"
        # 金蝉脱壳：对垒换自己出的题
        if item == "金蝉脱壳":
            if duel is None or not duel.get("engine"):
                return "金蝉脱壳需在诗词对垒进行中、且轮到你的回合使用。"
            de = duel["engine"]
            side = "a" if uid == de.a_id else "b"
            if not de.is_turn(uid):
                return "现在不是你的回合，无法使用金蝉脱壳。"
            old_puzzle = duel.get("puzzles", {}).get(uid, "")
            new_puzzle = self._pick_duel_replacement(de, side, old_puzzle)
            if not new_puzzle:
                return "未找到合适的同格式题目，请稍后再试。"
            de.replace_side_puzzle(side, new_puzzle)
            duel["puzzles"][uid] = new_puzzle
            duel.setdefault("user_verses", {}).setdefault(
                de.b_id if side == "a" else de.a_id, set()).clear()
            self.pm.consume_item(uid, item, count, uname)
            return f"🪙 金蝉脱壳：你的题目已更换为「{new_puzzle}」（对方要重新猜了）。"
        # 探囊取物：@玩家 偷一个道具
        if item == "探囊取物":
            if not at_id or at_id == uid:
                return "请 @ 一个目标玩家来偷取道具。"
            stolen = self.pm.take_random_item(at_id, uid, self._uid_name(at_id), uname)
            if not stolen:
                return f"对方没有可偷的道具。"
            self.pm.consume_item(uid, item, 1, uname)
            return f"🪝 探囊取物成功！从对方身上顺走了【{stolen}】。"
        # 乐不思蜀 / 百战不殆 / 孤注一掷 / 请君入梦：对垒回合类
        if item in ("乐不思蜀", "百战不殆", "孤注一掷", "请君入梦"):
            if duel is None or not duel.get("engine"):
                return f"{item}需在诗词对垒进行中使用。"
            de = duel["engine"]
            if not de.is_turn(uid):
                return f"{item}需在你的回合使用。"
            side = "a" if uid == de.a_id else "b"
            opp = "b" if side == "a" else "a"
            eff = duel.setdefault("item_effects", {"skip": {}, "immune": {}, "gamble": {}, "dream": {}})
            self.pm.consume_item(uid, item, count, uname)
            if item == "乐不思蜀":
                eff["skip"][opp] = eff["skip"].get(opp, 0) + count
                return "😴 乐不思蜀：对方下一回合将跳过，你获得一次额外回合。"
            if item == "百战不殆":
                eff["immune"][side] = eff["immune"].get(side, 0) + count
                return "🛡 百战不殆：接下来你方将有若干次免疫机会（对方猜中也不结束）。"
            if item == "孤注一掷":
                eff["gamble"][side] = {"active": True, "left": 3 * count}
                return "🎲 孤注一掷：接下来你将连续追加若干次自己的回合；若仍猜不中则由对方获胜。"
            if item == "请君入梦":
                eff["dream"][opp] = eff["dream"].get(opp, 0) + 2 * count
                return "🌙 请君入梦：对方接下来几次轮到将由系统随机代猜。"
        return f"道具【{item}】暂未开放使用。"

    @filter.command("我的成就")
    async def my_achievements(self, event: AstrMessageEvent):
        """查看已解锁成就及进度（图片渲染）。"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        achs = self.pm.get_achievements(uid)
        img_path = os.path.join(str(self.plugin_data_dir), f"my_achievements_{uid}.png")
        from .game.guess_verse import render_achievements
        render_achievements(uid, uname, achs, img_path)
        yield event.image_result(img_path)

    # ==========================================
    # ⚔️ 邀战猜诗词（对战）
    # ==========================================
    def _extract_at_id(self, event):
        """提取消息中第一个艾特的 user_id。"""
        try:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj and hasattr(msg_obj, "message"):
                for comp in msg_obj.message:
                    if isinstance(comp, Comp.At):
                        return str(getattr(comp, "qq", ""))
        except Exception:
            pass
        raw = str(getattr(event, "message_str", "") or "")
        m = re.search(r"\[CQ:at,qq=(\d+)\]", raw)
        if m:
            return m.group(1)
        m = re.search(r"@(\d{5,12})", raw)
        if m:
            return m.group(1)
        return None

    def _pick_random_index(self, target_text, used):
        """从未用过的下标随机挑一个。target_text 纯汉字。"""
        import random as _r
        avail = [i for i in range(len(target_text)) if i not in used]
        if not avail:
            return None
        return _r.choice(avail)

    def _pick_random_shengmu(self, target_parts, used):
        """从未用过的下标随机挑一个字的声母/韵母。返回 (idx, initial, final)。"""
        import random as _r
        avail = [i for i, p in enumerate(target_parts) if i not in used and p and p.get("initial") and p.get("final")]
        if not avail:
            return None
        idx = _r.choice(avail)
        p = target_parts[idx]
        return idx, p["initial"], p["final"]

    def _current_engine_fmt(self, engine):
        """返回当前猜诗句的格式 ('single', n)/(combo, (a,b))。"""
        punct = getattr(engine, "target_punct", []) or []
        hanzi = getattr(engine, "target_hanzi", "") or ""
        if not punct:
            return ("single", len(hanzi))
        lens = []
        cur = 0
        for pos, _p in punct:
            if pos >= len(hanzi):
                continue
            lens.append(pos - cur)
            cur = pos
        lens.append(len(hanzi) - cur)
        return ("combo", tuple(lens))

    def _pick_verse_with_char(self, engine, ch):
        """猜诗句同格式下随机找一句含 ch 的诗句。返回 (sentence,title,author,dynasty) 或 None。"""
        import random as _r
        fmt = self._current_engine_fmt(engine)
        # 经典曲库候选
        cands = []
        for p in self.classic_poems or []:
            sent = (p.get("sentence") or "")
            h = re.sub(r"[^\u4e00-\u9fff]", "", sent)
            if ch not in h:
                continue
            punct = extract_punct(sent)
            if fmt[0] == "single":
                if not punct and len(h) == fmt[1]:
                    cands.append((h, p))
            else:
                if punct:
                    segs = re.split(r"[，。！？、；：]", sent)
                    segs = [re.sub(r"[^\u4e00-\u9fff]", "", s) for s in segs if re.sub(r"[^\u4e00-\u9fff]", "", s)]
                    if len(segs) == len(fmt[1]) and all(len(s) == n for s, n in zip(segs, fmt[1])):
                        cands.append((sent, p))
        if cands:
            sent, p = _r.choice(cands)
            return (sent, p.get("title", ""), p.get("author", ""), p.get("dynasty", ""))
        # 总库候选（随机抽同格式句筛含字，最多尝试若干）
        if self.db:
            try:
                if fmt[0] == "single":
                    rows = self.db.get_random_verse(fmt[1], fmt[1], target_count=40, max_scan=200)
                else:
                    rows = self.db.get_random_verse_by_combo(fmt[1][0], fmt[1][1], target_count=40, max_scan=400)
                for verse, title, author, dynasty in rows:
                    if ch in verse and verse != engine.target_text:
                        return (verse, title, author, dynasty)
            except Exception:
                pass
        return None

    def _pick_duel_replacement(self, de, side, old_puzzle):
        """对垒金蝉脱壳：给 side 方找同格式新题（单句或同分句两句）。返回新题文本或 None。"""
        import random as _r
        old_h = extract_hanzi(old_puzzle)
        old_punct = extract_punct(old_puzzle)
        target_hanzi = de.a_target_hanzi if side == "a" else de.b_target_hanzi
        target_punct = de.a_target_punct if side == "a" else de.b_target_punct
        # 单句
        if not old_punct:
            n = len(old_h)
            cands = []
            for p in self.classic_poems or []:
                h = re.sub(r"[^\u4e00-\u9fff]", "", p.get("sentence") or "")
                if len(h) == n and not extract_punct(p.get("sentence") or "") and h != old_h and h != target_hanzi:
                    cands.append(h)
            if cands:
                return _r.choice(cands)
            if self.db:
                try:
                    rows = self.db.get_random_verse(n, n, target_count=20, max_scan=200)
                    for verse, *_ in rows:
                        if verse != old_h and verse != target_hanzi and self._is_in_library(verse):
                            return verse
                except Exception:
                    pass
            return None
        # 两句
        a_len = len(target_hanzi) if False else None
        # 用 engine 的目标长度
        lens = []
        cur = 0
        for pos, _p in target_punct:
            if pos >= len(target_hanzi):
                continue
            lens.append(pos - cur)
            cur = pos
        lens.append(len(target_hanzi) - cur)
        if len(lens) == 2:
            if self.db:
                try:
                    rows = self.db.get_random_verse_by_combo(lens[0], lens[1], target_count=20, max_scan=400)
                    for verse, *_ in rows:
                        if verse != old_puzzle:
                            return verse
                except Exception:
                    pass
        return None

    def _pick_auto_guess(self, engine, side):
        """请君入梦：随机给 side 方挑一句他要猜的目标句。"""
        import random as _r
        target_hanzi = engine.a_target_hanzi if side == "a" else engine.b_target_hanzi
        target_punct = engine.a_target_punct if side == "a" else engine.b_target_punct
        if not target_punct:
            n = len(target_hanzi)
            cands = [h for h in self.classic_clause_set if len(h) == n]
            if cands:
                return _r.choice(cands)
            if self.db:
                try:
                    rows = self.db.get_random_verse(n, n, target_count=20, max_scan=300)
                    if rows:
                        return rows[0][0]
                except Exception:
                    pass
            return None
        # 两句
        segs = []
        cur = 0
        for pos, _p in target_punct:
            if pos >= len(target_hanzi):
                continue
            segs.append(pos - cur)
            cur = pos
        segs.append(len(target_hanzi) - cur)
        if len(segs) == 2:
            if self.db:
                try:
                    rows = self.db.get_random_verse_by_combo(segs[0], segs[1], target_count=10, max_scan=300)
                    if rows:
                        return rows[0][0]
                except Exception:
                    pass
        return None


    @staticmethod
    def _parse_poem_format(token):
        """解析诗句格式 token。返回 ('single', n) 或 ('combo', (a, b)) 或 None。
        支持：4/5/6/7 单句；44/34/43/55/77 两句。
        """
        token = str(token).strip()
        if token in ("4", "5", "6", "7"):
            return ("single", int(token))
        combos = {"44": (4, 4), "34": (3, 4), "43": (4, 3), "55": (5, 5), "77": (7, 7)}
        if token in combos:
            return ("combo", combos[token])
        return None

    @staticmethod
    def _format_desc(fmt):
        """格式化描述：('single',5)->「5字单句」；('combo',(5,5))->「5字+5字」"""
        if fmt[0] == "single":
            return f"{fmt[1]} 字单句"
        a, b = fmt[1]
        return f"{a} 字+{b} 字（两句）"

    @filter.command("邀战猜诗词")
    async def invite_battle(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id())
        if not group_id or group_id == "None":
            yield event.plain_result("邀战猜诗词仅支持群聊哦~")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.battle_sessions:
            yield event.plain_result("当前群已有进行中的邀战对局。")
            return

        target_id = self._extract_at_id(event)
        if not target_id:
            yield event.plain_result("请艾特要挑战的成员，如：/邀战猜诗词 @某人")
            return
        sender_id = str(event.get_sender_id())
        if target_id == sender_id:
            yield event.plain_result("不能挑战自己哦~")
            return

        target_name = None
        try:
            info = await event.bot.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(target_id))
            if isinstance(info, dict) and "data" in info:
                info = info["data"]
            target_name = info.get("nickname") or info.get("card") or f"用户{target_id}"
        except Exception:
            target_name = f"用户{target_id}"

        self.battle_sessions[session_id] = {
            "state": "waiting_confirm",
            "challenger_id": sender_id,
            "challenger_name": event.get_sender_name() or f"用户{sender_id}",
            "opponent_id": target_id,
            "opponent_name": target_name,
            "engine": None,
            "created_at": time.time(),
        }
        yield event.plain_result(
            f"⚔️ 【邀战猜诗词】\n"
            f"{event.get_sender_name()} 向 {target_name} 发起挑战！\n"
            f"规则：随机一句课本古诗（如「对酒当歌，人生几何」），挑战者猜前句，被挑战者猜后句，先猜中自己半句者获胜。\n"
            f"请 {target_name} 回复【接受】开始对战，或回复【拒绝】。（2 分钟内有效）"
        )
        # 启动 2 分钟超时自动取消
        try:
            origin = getattr(event, "unified_msg_origin", None)
            asyncio.create_task(self._battle_confirm_timeout(session_id, origin))
        except Exception:
            pass

    @filter.command("结束邀战")
    async def end_battle(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.battle_sessions:
            self.battle_sessions.pop(session_id)
            yield event.plain_result("邀战已取消。")
        else:
            yield event.plain_result("当前没有进行中的邀战对局。")

    # ==========================================
    # 🍵 诗词对垒（双方各出题，互猜对方诗句）
    # ==========================================
    async def _send_private(self, bot, user_id, text):
        """给指定用户发私聊消息（OneBot send_private_msg）。返回是否成功。"""
        try:
            await bot.api.call_action(
                "send_private_msg",
                user_id=int(user_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
            return True
        except Exception as e:
            logger.error(f"[duel] 私聊发送失败 user={user_id}: {e}")
            return False

    def _is_in_library(self, text):
        """判断诗句是否在总库（119万首）中；总库未装则回退经典曲库单句集合。
        同时尝试简体/繁体查询（总库可能繁体存储）。
        """
        # 确保总库已加载（惰性加载，DB 存在才启用）
        self._ensure_db()
        hanzi = re.sub(r'[^\u4e00-\u9fff]', '', text)
        if not hanzi:
            return False
        # 总库优先（简体 + 繁体各查一次）
        if self.db is not None:
            queries = [hanzi]
            try:
                from opencc import OpenCC
                queries.append(OpenCC('s2t').convert(hanzi))  # 简体转繁体
            except Exception:
                pass
            for q in queries:
                try:
                    if self.db.is_complete_sentence(q):
                        return True
                except Exception:
                    continue
        # 回退经典曲库单句集合
        return hanzi in self.classic_clause_set

    @filter.command("诗词对垒")
    async def start_duel(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id())
        if not group_id or group_id == "None":
            yield event.plain_result("诗词对垒仅支持群聊哦~")
            return
        session_id = group_id
        if session_id in self.duel_sessions:
            yield event.plain_result("当前群已有进行中的诗词对垒。")
            return
        if session_id in self.battle_sessions:
            yield event.plain_result("当前群已有进行中的邀战对局，请先结束。")
            return

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or f"用户{sender_id}"

        # ===== 解析参数：@某人 / bot / 格式(4-7 或 44/34/43/55/77) / 提示方式(声|形)，可组合任意顺序 =====
        target_id = self._extract_at_id(event)
        # 机器人自身账号 ID（用于识别 @机器人 挑战 bot）
        bot_self_id = None
        try:
            bot_self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        except Exception:
            pass
        fmt = None
        hint_mode = None
        msg_text = str(event.get_message_str() or "")
        # 去掉指令前缀（可能带/或不带/）和 @ 片段后的剩余文本
        remainder = re.sub(r"^[/／]?\s*诗词对垒", "", msg_text, flags=re.IGNORECASE)
        remainder = re.sub(r"\[CQ:at,qq=\d+\]", " ", remainder)
        remainder = re.sub(r"@\d{5,12}", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()

        for token in remainder.split(" "):
            if not token:
                continue
            parsed = self._parse_poem_format(token)
            if parsed:
                if fmt is not None:
                    yield event.plain_result("❌ 只能指定一种格式（4/5/6/7 单句 或 44/34/43/55/77 两句）。\n用法：/诗词对垒 [@某人|bot] [格式] [声|形]")
                    return
                fmt = parsed
            elif token in ("声", "形", "拼音", "部首"):
                if hint_mode is not None:
                    yield event.plain_result("❌ 只能指定一种提示方式（声=拼音 / 形=部首）。\n用法：/诗词对垒 [@某人|bot] [格式] [声|形]")
                    return
                hint_mode = "pinyin" if token in ("声", "拼音") else "radical"
            elif token.lower() in ("bot", "机器人", "ai"):
                if not self.ai_bot.enabled:
                    yield event.plain_result("⚠️ AI 机器人未启用（请在插件配置中开启「启用 AI 机器人参与游戏」）。")
                    return
                target_id = BOT_ID
            else:
                yield event.plain_result(
                    f"❌ 无效参数「{token}」。\n用法：/诗词对垒 [@某人|bot] [格式] [声|形]\n"
                    f"格式：4/5/6/7 单句，或 44/34/43/55/77 两句\n"
                    f"例如：/诗词对垒 4 声 ｜ /诗词对垒 55 形 @某人 ｜ /诗词对垒 bot 7"
                )
                return

        # @机器人 视为挑战 bot
        if bot_self_id and target_id == bot_self_id:
            target_id = BOT_ID
        if target_id == BOT_ID and not self.ai_bot.enabled:
            yield event.plain_result("⚠️ AI 机器人未启用（请在插件配置中开启「启用 AI 机器人参与游戏」）。")
            return

        if target_id == sender_id:
            yield event.plain_result("不能挑战自己哦~")
            return

        # 未指定格式 -> 随机单句：4字10% / 6字10% / 5、7字各40%
        if fmt is None:
            import random as _r
            roll = _r.random()
            if roll < 0.10:
                fmt = ("single", 4)
            elif roll < 0.20:
                fmt = ("single", 6)
            elif roll < 0.60:
                fmt = ("single", 5)
            else:
                fmt = ("single", 7)

        # 未指定提示方式 -> 随机：声70% / 形30%
        if hint_mode is None:
            import random as _r2
            hint_mode = "pinyin" if _r2.random() < 0.70 else "radical"

        target_name = None
        if target_id == BOT_ID:
            target_name = self.ai_bot.bot_name
        elif target_id:
            target_name = f"用户{target_id}"
            try:
                info = await event.bot.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(target_id))
                if isinstance(info, dict) and "data" in info:
                    info = info["data"]
                target_name = info.get("nickname") or info.get("card") or target_name
            except Exception:
                pass

        self.duel_sessions[session_id] = {
            "state": "wait_puzzle" if target_id == BOT_ID else "wait_confirm",
            "challenger_id": sender_id,
            "challenger_name": sender_name,
            "opponent_id": target_id,   # 无@则为 None，由第一个回复【接受】者担任
            "opponent_name": target_name,
            "fmt": fmt,
            "hint_mode": hint_mode,
            "puzzles": {},        # {user_id: sentence}
            "puzzle_done": set(), # 已出题的人
            "engine": None,
            "group_origin": getattr(event, "unified_msg_origin", None),
            "created_at": time.time(),
            "bot_thinking": False,
        }

        hint_label = "拼音" if hint_mode == "pinyin" else "部首"
        fmt_desc = self._format_desc(fmt)
        if target_id == BOT_ID:
            # 挑战 bot：跳过接受确认，直接进入出题阶段；bot 自动出题
            hint = (f"🍵 【诗词对垒】提示方式：{hint_label}，格式：{fmt_desc}\n"
                    f"请发送「{fmt_desc}」诗句作为你的题目（总库中，前缀「cc」）。\n"
                    f"单句例：cc 床前明月光 ｜ 两句例：cc 离离原上草，一岁一枯荣")
            await self._send_private(event.bot, sender_id, hint)
            asyncio.create_task(self._bot_do_puzzle(session_id, fmt, event))
            yield event.plain_result(
                f"🍵 【诗词对垒】\n"
                f"{sender_name} 向 {target_name} 发起对垒！\n"
                f"双方各出「{fmt_desc}」诗句，随后互猜对方诗句，先猜中者获胜。\n"
                f"提示方式：{hint_label}\n"
                f"已私聊你出题提示，{target_name} 正在出题中..."
            )
        elif target_id:
            yield event.plain_result(
                f"🍵 【诗词对垒】\n"
                f"{sender_name} 向 {target_name} 发起对垒！\n"
                f"双方各出「{fmt_desc}」诗句（曲库中，前缀「cc」）作为题目，随后互猜对方诗句，先猜中者获胜。\n"
                f"提示方式：{hint_label}\n"
                f"请 {target_name} 回复【接受】开始，或回复【拒绝】。（2 分钟内有效）"
            )
        else:
            yield event.plain_result(
                f"🍵 【诗词对垒】\n"
                f"{sender_name} 发起自由对垒！\n"
                f"双方各出「{fmt_desc}」诗句（曲库中，前缀「cc」）作为题目，随后互猜对方诗句，先猜中者获胜。\n"
                f"提示方式：{hint_label}\n"
                f"第一个回复【接受】的群成员将作为对手。（2 分钟内有效）"
            )
        try:
            origin = getattr(event, "unified_msg_origin", None)
            asyncio.create_task(self._duel_confirm_timeout(session_id, origin))
        except Exception:
            pass

    @filter.command("结束对垒")
    async def end_duel(self, event: AstrMessageEvent):
        # 群聊按群号匹配；私聊按参与者 uid 匹配
        target = None
        if event.get_group_id():
            gid = str(event.get_group_id())
            if gid in self.duel_sessions:
                target = gid
        if not target:
            uid = str(event.get_sender_id())
            for k, d in self.duel_sessions.items():
                if uid in (d.get("challenger_id"), d.get("opponent_id")):
                    target = k
                    break
        if target:
            self.duel_sessions.pop(target)
            yield event.plain_result("对垒已取消。")
        else:
            yield event.plain_result("当前没有进行中的诗词对垒。")

    async def _duel_confirm_timeout(self, session_id, msg_origin):
        """对垒确认 2 分钟超时自动取消。"""
        try:
            await asyncio.sleep(120)
            if session_id in self.duel_sessions:
                d = self.duel_sessions[session_id]
                if d.get("state") == "wait_confirm":
                    self.duel_sessions.pop(session_id)
                    if msg_origin:
                        await self.context.send_message(msg_origin, MessageChain([
                            Plain("⏰ 对垒挑战超时（2 分钟未响应），已自动取消。")
                        ]))
        except Exception as e:
            logger.error(f"对垒超时任务异常: {e}")

    async def _handle_duel_message(self, event, msg_raw, is_private, handled):
        """处理诗词对垒相关消息（前缀「cc」）。
        私聊：确认/出题；群聊：确认/猜测。
        async generator：用 yield 发送消息；handled[0]=True 表示消息已被对垒处理。

        关键：不能调用 event.stop_event()。AstrBot 在消息发送后若事件被停止，
        会丢弃 handler 生成器，导致 yield 之后的代码（如进入猜测阶段、换人）不执行。
        用 event.should_call_llm(True) 阻断默认 LLM 即可，不会中断生成器。
        """
        uid = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        # 私聊判断：无群号即为私聊（不依赖 is_private_chat，更可靠）
        is_private = is_private or not group_id or group_id == "None"

        # 确保总库已加载（惰性加载，DB 存在才启用；对垒出题/猜测的库校验依赖它）
        self._ensure_db()

        # 命令类消息不进入对垒处理（避免 /诗词对垒 命令被二次吞掉）
        if msg_raw.startswith("/"):
            return

        # 找到当前用户/群相关的对垒会话
        duel = None
        sid = None
        for k, d in self.duel_sessions.items():
            if group_id and group_id != "None" and group_id == k:
                duel, sid = d, k
                break
            if uid in (d.get("challenger_id"), d.get("opponent_id")):
                duel, sid = d, k
                break
        if not duel:
            return

        # 判断是否对垒参与者。
        # 自由对垒（opponent_id 为空）在 wait_confirm 阶段：任何群成员回复【接受】都可成为对手，
        # 因此群聊中所有成员在等待确认期间都可进入处理；其他阶段仅参与者可进入。
        is_free_confirm = (
            duel.get("state") == "wait_confirm"
            and not duel.get("opponent_id")
            and not is_private
        )
        is_participant = uid in (duel.get("challenger_id"), duel.get("opponent_id"))
        if not is_participant and not is_free_confirm:
            # 非参与者：放行（不吞消息，让正常流程/LLM处理）
            return

        def _block_llm():
            """标记消息已被对垒处理，并阻断默认 LLM（不停止生成器）。"""
            handled[0] = True
            try:
                event.should_call_llm(True)
            except Exception:
                pass

        # ===== 等待确认阶段（群聊）=====
        if duel["state"] == "wait_confirm":
            if time.time() - duel.get("created_at", 0) > 120:
                self.duel_sessions.pop(sid)
                _block_llm()
                yield event.plain_result("⏰ 对垒挑战超时，已自动取消。")
                return
            opp_id = duel.get("opponent_id")
            if opp_id:
                # 指定对手模式：仅对手可接受/拒绝
                if uid != opp_id:
                    return
                if msg_raw in ("接受", "同意", "应战"):
                    duel["state"] = "wait_puzzle"
                    fmt_desc = self._format_desc(duel.get("fmt"))
                    hl = "拼音" if duel.get("hint_mode") == "pinyin" else "部首"
                    hint = (f"🍵 【诗词对垒】提示方式：{hl}，格式：{fmt_desc}\n"
                            f"请发送「{fmt_desc}」诗句作为你的题目（总库中，前缀「cc」）。\n"
                            f"单句例：cc 床前明月光 ｜ 两句例：cc 离离原上草，一岁一枯荣")
                    ok_a = await self._send_private(event.bot, duel["challenger_id"], hint)
                    ok_b = await self._send_private(event.bot, duel["opponent_id"], hint)
                    _block_llm()
                    if ok_a and ok_b:
                        yield event.plain_result(f"🍵 对垒开始！已私聊双方提示出题（{fmt_desc}），出题完成后在群聊公开互猜。")
                    else:
                        yield event.plain_result(
                            f"⚠️ 私聊出题失败（机器人需与双方互为好友才能私聊）。\n"
                            f"请先让双方添加机器人为好友，再重新发起对垒。"
                        )
                        self.duel_sessions.pop(sid)
                    return
                elif msg_raw in ("拒绝", "拒绝挑战", "不接受"):
                    self.duel_sessions.pop(sid)
                    _block_llm()
                    yield event.plain_result(f"{duel['opponent_name']} 拒绝了挑战。")
                    return
                return
            else:
                # 自由对垒：第一个回复【接受】的群成员成为对手（挑战者除外）
                if uid == duel["challenger_id"]:
                    if msg_raw in ("接受", "同意", "应战"):
                        _block_llm()
                        yield event.plain_result("不能挑战自己哦~ 请等待其他成员回复【接受】。")
                    return
                if msg_raw in ("接受", "同意", "应战"):
                    duel["opponent_id"] = uid
                    duel["opponent_name"] = event.get_sender_name() or f"用户{uid}"
                    duel["state"] = "wait_puzzle"
                    fmt_desc = self._format_desc(duel.get("fmt"))
                    hl = "拼音" if duel.get("hint_mode") == "pinyin" else "部首"
                    hint = (f"🍵 【诗词对垒】提示方式：{hl}，格式：{fmt_desc}\n"
                            f"请发送「{fmt_desc}」诗句作为你的题目（总库中，前缀「cc」）。\n"
                            f"单句例：cc 床前明月光 ｜ 两句例：cc 离离原上草，一岁一枯荣")
                    ok_a = await self._send_private(event.bot, duel["challenger_id"], hint)
                    ok_b = await self._send_private(event.bot, uid, hint)
                    _block_llm()
                    if ok_a and ok_b:
                        yield event.plain_result(f"🍵 {event.get_sender_name()} 接受对垒！已私聊双方提示出题（{fmt_desc}），出题完成后在群聊公开互猜。")
                    else:
                        yield event.plain_result(
                            f"⚠️ 私聊出题失败（机器人需与双方互为好友才能私聊）。\n"
                            f"请先让双方添加机器人为好友，再重新发起对垒。"
                        )
                        self.duel_sessions.pop(sid)
                    return
                return

        # ===== 出题阶段（私聊）=====
        if duel["state"] == "wait_puzzle":
            if not is_private:
                return
            # 出题需 cc 前缀，否则静默忽略（阻断 LLM 即可，不回复）
            if not msg_raw.startswith("cc"):
                _block_llm()
                return
            if uid in duel["puzzle_done"]:
                _block_llm()
                yield event.plain_result("你已经出过题了，等待对方出题中...")
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)
            fmt = duel.get("fmt")
            fmt_kind = fmt[0] if fmt else "single"
            if fmt_kind == "single":
                need = fmt[1]
                if len(hanzi) != need:
                    _block_llm()
                    yield event.plain_result(f"题目需为 {need} 字单句，当前 {len(hanzi)} 字。")
                    return
                if not self._is_in_library(clean):
                    _block_llm()
                    yield event.plain_result(f"「{clean}」不在诗词库中，请输入曲库诗句作为题目。")
                    return
            else:
                a_len, b_len = fmt[1]
                segs = re.split(r'[，。！？、；：]', clean)
                segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
                if len(segs) != 2 or len(segs[0]) != a_len or len(segs[1]) != b_len:
                    _block_llm()
                    yield event.plain_result(f"题目需为「{a_len} 字+{b_len} 字」两句（带标点），当前格式不符。")
                    return
                if not self.db or not self.db.is_adjacent_pair(segs[0], segs[1]):
                    _block_llm()
                    yield event.plain_result(f"「{clean}」未在诗词库中（两句需为库中某首的相邻两句）。")
                    return
            duel["puzzles"][uid] = clean
            duel["puzzle_done"].add(uid)
            # 记录出题诗句到个人数据
            self.pm.record_verse(uid, clean, event.get_sender_name() or f"用户{uid}")
            _block_llm()
            # 双方都出题后进入猜测阶段
            if len(duel["puzzle_done"]) >= 2:
                engine = self._start_duel_playing(duel, sid)
                # 若 bot 先手，触发 bot 思考
                if engine and engine.is_turn(BOT_ID):
                    self._maybe_bot_duel_turn(duel, sid, event)
            yield event.plain_result(f"✅ 出题成功！题目：{clean}。等待对方出题...")
            return

        # ===== 猜测阶段（群聊）=====
        if duel["state"] == "playing":
            engine = duel["engine"]
            if is_private:
                _block_llm()
                return
            if not engine.is_turn(uid):
                _block_llm()
                return  # 没轮到：静默
            # 猜测需 cc 前缀，否则静默（防误触）
            if not msg_raw.startswith("cc"):
                _block_llm()
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            _block_llm()
            result = self._apply_duel_guess(duel, sid, uid, event.get_sender_name() or f"用户{uid}", clean)
            if not result["ok"]:
                yield event.plain_result(result["err"])
                return
            for kind, payload in result["msgs"]:
                if kind == "text":
                    yield event.plain_result(payload)
                else:
                    yield event.image_result(payload)
            # 乐不思蜀（跳过）/ 请君入梦（代猜）自动推进
            auto_step = 0
            while (not result.get("finished")) and sid in self.duel_sessions:
                auto_step += 1
                if auto_step > 6:
                    break
                engine = duel.get("engine")
                if not engine:
                    break
                eff = duel.get("item_effects", {})
                cur = engine.current_side()
                cur_uid = engine.a_id if cur == "a" else engine.b_id
                if eff.get("skip", {}).get(cur, 0) > 0:
                    eff["skip"][cur] -= 1
                    engine.switch_turn()
                    yield event.plain_result(f"😴 对方被【乐不思蜀】跳过一回合，轮到 {engine.current_name()}。")
                    continue
                if eff.get("dream", {}).get(cur, 0) > 0 and str(cur_uid) != str(BOT_ID):
                    eff["dream"][cur] -= 1
                    guess = self._pick_auto_guess(engine, cur)
                    if not guess:
                        break
                    r2 = self._apply_duel_guess(
                        duel, sid, cur_uid, self._uid_name(cur_uid), guess,
                    )
                    for kind, payload in r2.get("msgs", []):
                        if kind == "text":
                            yield event.plain_result(payload)
                        else:
                            yield event.image_result(payload)
                    result = r2
                    continue
                break
            # bot 回合触发（若轮到 bot）
            self._maybe_bot_duel_turn(duel, sid, event)
            return

    # ==========================================
    # 🤖 Bot 管理指令
    # ==========================================
    @filter.command("bot加入")
    async def add_bot(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if not engine:
            yield event.plain_result("当前没有游戏，请先【/衔字飞花令】或【/纵横飞花令】建局。")
            return
        result = engine.add_bot()
        if result["status"] == "ignore":
            yield event.plain_result("Bot 已在游戏中！")
        elif result["status"] == "error":
            yield event.plain_result(result["msg"])
        else:
            yield event.plain_result(result["msg"])

    @filter.command("bot退出")
    async def remove_bot(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if not engine:
            yield event.plain_result("当前没有进行中的游戏。")
            return
        result = engine.remove_bot()
        if result["status"] == "ignore":
            yield event.plain_result("Bot 未在游戏中。")
        else:
            yield event.plain_result(result["msg"])

    # ==========================================
    # 多存档管理指令
    # ==========================================
    @filter.command("恢复游戏")
    async def load_game(self, event: AstrMessageEvent, arg: str = ""):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前已有进行中的游戏，请先【结束游戏】。")
            return
        saves = self.get_saves(session_id)
        if not saves:
            yield event.plain_result("未找到该群的任何游戏存档。")
            return
        if not arg or not arg.isdigit():
            msg = [f"📂 发现 {len(saves)} 个存档，请发送 /恢复游戏 [序号] 来选择：", "-"*15]
            for i, s in enumerate(saves, 1):
                gtype = "纵横" if "Crossword" in s["type"] else ("蛇形" if "Snake" in s["type"] else "衔字")
                msg.append(f"[{i}] {gtype}飞花令 | 建于: {s['start_time']} | 进度: {s['turn_count']}回合")
            yield event.plain_result("\n".join(msg))
            return
        index = int(arg)
        if index < 1 or index > len(saves):
            yield event.plain_result("❌ 无效的存档序号。")
            return
        target_save = saves[index-1]
        filename = target_save["filename"]
        if "Crossword" in target_save["type"]:
            engine = PoetryCrosswordEngine(session_id, self.db, str(self.saves_dir), save_filename=filename)
        elif "Snake" in target_save["type"]:
            engine = PoetrySnakeEngine(session_id, self.db, str(self.saves_dir), save_filename=filename)
        else:
            engine = FlowingPetalsEngine(session_id, self.db, str(self.saves_dir), save_filename=filename)
        try:
            if engine.load_state():
                self.active_games[session_id] = engine
                if session_id in self.timeout_tasks: self.timeout_tasks[session_id].cancel()
                self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
                yield event.plain_result(f"💾 存档 [{index}] 恢复成功！游戏继续。")
                if "Crossword" in target_save["type"] and hasattr(engine, "render_image"):
                    yield event.image_result(engine.render_image())
                elif "Flowing" in target_save["type"] and hasattr(engine, "get_status_str"):
                    yield event.plain_result(engine.get_status_str())
            else:
                yield event.plain_result("❌ 存档文件读取失败。")
        except Exception as e:
            yield event.plain_result(f"❌ 恢复失败: {e}")

    @filter.command("删除存档")
    async def delete_save(self, event: AstrMessageEvent, arg: str = ""):
        session_id = str(event.get_group_id() or event.get_session_id())
        saves = self.get_saves(session_id)
        if not saves:
            yield event.plain_result("未找到该群的任何游戏存档。")
            return
        if not arg or not arg.isdigit():
            msg = [f"🗑 发现 {len(saves)} 个存档，请发送 /删除存档 [序号] 来永久删除：", "-"*15]
            for i, s in enumerate(saves, 1):
                gtype = "纵横" if "Crossword" in s["type"] else ("蛇形" if "Snake" in s["type"] else "衔字")
                msg.append(f"[{i}] {gtype}飞花令 | 建于: {s['start_time']} | 进度: {s['turn_count']}回合")
            yield event.plain_result("\n".join(msg))
            return
        index = int(arg)
        if index < 1 or index > len(saves):
            yield event.plain_result("❌ 无效的存档序号。")
            return
        target_save = saves[index-1]
        try:
            os.remove(target_save["path"])
            yield event.plain_result(f"🗑 存档 [{index}] 已成功删除！")
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    @filter.command("生成战报")
    async def generate_report(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if not engine:
            yield event.plain_result("当前没有进行中的游戏。如果要生成旧战报，请先【恢复游戏】。")
            return
        yield event.plain_result(engine.generate_text_report())
        if hasattr(engine, "render_image"):
            yield event.image_result(engine.render_image())

    @filter.command("结束游戏")
    async def stop_game(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            engine = self.active_games.pop(session_id)
            yield event.plain_result("⏹️ 游戏已结束。最后战果：\n" + engine.generate_text_report())
        else:
            yield event.plain_result("当前没有正在进行的游戏。")

    # ==========================================
    # 📖 帮助与指南菜单
    # ==========================================
    @filter.command("飞花令帮助")
    async def poetry_help(self, event: AstrMessageEvent, topic: str = ""):
        topic = topic.strip()

        if not topic:
            msg = (
                "📖 【诗词游戏引擎】帮助指南\n"
                "====================\n"
                "欢迎使用本插件！请发送【/飞花令帮助 目录名】（或直接打数字）查看详情：\n\n"
                "📋 目录列表：\n"
                "1. /飞花令帮助 衔字规则  (衔字飞花令玩法说明)\n"
                "2. /飞花令帮助 纵横规则  (纵横飞花令玩法说明)\n"
                "3. /飞花令帮助 蛇形规则  (蛇形飞花令玩法说明)\n"
                "4. /飞花令帮助 基础查询  (查诗词/查诗句指令)\n"
                "5. /飞花令帮助 游戏管理  (建局/读档/跳过等指令)\n"
                "===================="
            )
            yield event.plain_result(msg)
            return

        if topic in ["1", "衔字规则", "衔字"]:
            msg = (
                "🌸 【衔字飞花令】规则说明\n"
                "--------------------\n"
                "1. 玩家需接上一个人发送诗句的【任意一个字】。\n"
                "2. 必须是一整句完整的古诗，且至少需要 4 个字。\n"
                "3. 匹配的字越多，得分越高！\n"
                "4. 被匹配过的字会进入冷却，下一个玩家不能再用这几个字接龙。\n"
                "5. 难度进阶：如果当前回合是第3轮以上，你发送的诗不仅要匹配上一句，还得包含再上一句的一个字！"
            )
        elif topic in ["2", "纵横规则", "纵横"]:
            msg = (
                "🌟 【纵横飞花令】规则说明\n"
                "--------------------\n"
                "1. 在棋盘上拼字！发送一句完整的诗（至少4字），该诗必须包含棋盘上已有的字，从而产生交叉。\n"
                "2. 绝对去重：棋盘上已经存在过的诗句，绝对不可以再发第二遍。\n"
                "3. 极简落子：如果有多个合法交叉点，系统会发送一张【带✨金黄色高亮起点的图片】。你只需要看着图，直接发送你想去的格子里的【数字】（如：1 或 2）即可自动落子！\n"
                "4. 结算：最终占领格子最多的玩家获胜！"
            )
        elif topic in ["3", "蛇形规则", "蛇形"]:
            msg = (
                "🐍 【蛇形飞花令】规则说明\n"
                "--------------------\n"
                "1. 经典贪吃蛇玩法 + 诗词！控制蛇吃诗句中掉落的高亮字。\n"
                "2. 吃到高亮字后，长出对应长度的身体。\n"
                "3. 撞墙或撞到自己则游戏结束。\n"
                "4. 每轮随机生成一句诗在棋盘外圈，找到其中的目标字吃掉即可得分！\n"
                "5. 支持 WASD 和方向键操控。"
            )
        elif topic in ["4", "基础查询", "查询"]:
            msg = (
                "📚 【基础查询】指令说明\n"
                "--------------------\n"
                "• /查询诗词 [诗词名] [作者(可选)]\n"
                "  例如：「/查询诗词 望庐山瀑布 李白」，精确匹配作者，有效避免同名诗词干扰。\n\n"
                "• /查询诗句 [诗句内容]\n"
                "  例如：「/查询诗句 借问新安江」，双核搜索，优先找出完全一致的原句出处，同时展示包含该片段的其他诗词。"
            )
        elif topic in ["5", "游戏管理", "管理", "指令"]:
            msg = (
                "⚙️ 【游戏管理】全指令说明\n"
                "--------------------\n"
                "【建局指令】\n"
                "• /衔字飞花令\n"
                "• /纵横飞花令 [宽] [高] (如: /纵横飞花令 20 20)\n"
                "• /蛇形飞花令 [宽] [高] (如: /蛇形飞花令 40 40)\n\n"
                "【局内操作】 (无需加斜杠 /)\n"
                "• 加入 / 退出：参与或脱离当前游戏队列。\n"
                "• 跳过：若当前玩家迟迟不发，可输入跳过，系统判定超时后将自动强制流转。\n\n"
                "【多存档与结算指令】\n"
                "• /恢复游戏：展示存档列表，输入对应序号可继续未完成的对局！\n"
                "• /删除存档：展示列表并永久删除废弃存档。\n"
                "• /生成战报：随时查看当前玩家占地与得分状况。\n"
                "• /结束游戏：立即清算总分并解散游戏。"
            )
        else:
            msg = "❓ 未知的帮助目录。请直接发送 /飞花令帮助 查看可选的数字或目录名。"
        yield event.plain_result(msg)

    # ==========================================
    # 超时监控
    # ==========================================
    async def _battle_confirm_timeout(self, session_id, msg_origin):
        """邀战确认 2 分钟超时自动取消。"""
        try:
            await asyncio.sleep(120)
            if session_id in self.battle_sessions:
                b = self.battle_sessions[session_id]
                if b.get("state") == "waiting_confirm":
                    self.battle_sessions.pop(session_id)
                    if msg_origin:
                        await self.context.send_message(msg_origin, MessageChain([
                            Plain("⏰ 挑战超时（2 分钟未响应），已自动取消。")
                        ]))
        except Exception as e:
            logger.error(f"邀战超时任务异常: {e}")

    async def _active_timeout_monitor(self, session_id, msg_origin):
        try:
            while session_id in self.active_games:
                await asyncio.sleep(2)
                if session_id not in self.active_games: break
                engine = self.active_games[session_id]

                # 🤖 Bot turn handling
                if engine.is_bot_turn():
                    import random as _random
                    await asyncio.sleep(_random.uniform(2, 4))
                    if session_id not in self.active_games: break
                    try:
                        engine = self.active_games[session_id]
                        bot_resp = engine.bot_play()
                        if bot_resp and bot_resp.get("msg"):
                            await self.context.send_message(msg_origin, MessageChain([Plain(bot_resp["msg"])]))
                        if bot_resp and "image" in bot_resp:
                            await self.context.send_message(msg_origin, MessageChain([Image.fromFileSystem(bot_resp["image"])]))
                    except Exception as e:
                        logger.error(f"🤖 Bot 操作失败: {e}")
                        engine = self.active_games.get(session_id)
                        if engine:
                            engine.next_turn()
                            engine.update_activity()
                            engine.save_state()
                    # Bot 行动后冷却，避免死循环
                    await asyncio.sleep(3)
                    continue

                is_timeout, action, msg = engine.check_active_timeout()
                if is_timeout:
                    chain = [Plain(msg)]
                    if action == "end":
                        del self.active_games[session_id]
                        await self.context.send_message(msg_origin, MessageChain(chain))
                        break
                    elif action == "skip":
                        if hasattr(engine, "render_image"):
                            chain.append(Image.fromFileSystem(engine.render_image()))
                        elif hasattr(engine, "get_status_str"):
                            chain.append(Plain("\n" + engine.get_status_str()))
                        await self.context.send_message(msg_origin, MessageChain(chain))
        except Exception as e:
            logger.error(f"⏱ 飞花令超时监控任务崩溃: {e}")

    def _uid_name(self, uid):
        """根据 uid 从本地玩家缓存取名字（尽力而为）。"""
        try:
            return self.pm.load(uid).get("name", f"用户{uid}")
        except Exception:
            return f"用户{uid}"

    def _achieve_msg(self, uid, ach_id):
        """生成成就解锁提示文案。"""
        name = ACHIEVEMENTS.get(ach_id, (ach_id, ""))[0]
        uname = self._uid_name(uid)
        return f"🏆 {uname} 达成成就「{name}」！"

    def _apply_verse_guess(self, engine, session_id, uid, uname, clean):
        """执行猜诗句猜测的核心逻辑（库校验 + 状态推进 + 结算 + 渲染）。返回结果 dict，不发送消息。

        clean: 已去掉 cc 前缀的猜测文本。
        返回 {"ok", "err", "comp", "all_correct", "finished", "msgs": [(kind, payload)]}
        """
        msgs = []
        hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)

        def _fail(err):
            return {"ok": False, "err": err, "comp": None, "all_correct": False,
                    "finished": False, "msgs": msgs}

        if not hanzi or len(hanzi) != len(engine.target_hanzi):
            return _fail(f"答案 {len(engine.target_hanzi)} 个字，当前 {len(hanzi)} 字。请输入「cc 诗句」。")
        ok_fmt, fmt_msg = engine.check_format(clean)
        if not ok_fmt:
            return _fail(fmt_msg or "格式不正确。")
        # 判在不在总库中
        if extract_punct(clean):
            segs = re.split(r'[，。！？、；：]', clean)
            segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
            if len(segs) != 2 or not self.db or not self.db.is_adjacent_pair(segs[0], segs[1]):
                return _fail(f"「{clean}」未在诗词库中（需为库中相邻两句）。")
        else:
            if not self._is_in_library(clean):
                return _fail(f"「{clean}」不在诗词库中，请输入曲库诗句。")
        # 记录参与者与单局个人猜测次数
        if not hasattr(engine, "participants"):
            engine.participants = set()
            engine.user_guesses = {}
            engine.user_verses = set()
            engine.user_initials = {}
            engine.user_finals = {}
        engine.participants.add(uid)
        engine.user_guesses[uid] = engine.user_guesses.get(uid, 0) + 1
        # 🐖 重复诗句检测（本局内任意玩家发过即算重复）
        if hanzi in engine.user_verses:
            pig_count = self.pm.add_pig(uid, uname)
            msgs.append(("text", f"🐖 {uname} 重复诗句！猪+1（{'🐖' * pig_count}）"))
        else:
            engine.user_verses.add(hanzi)
        # 记录诗句到个人数据
        self.pm.record_verse(uid, clean, uname)
        self.pm.inc_stat(uid, "total_guesses", 1, uname)
        # 检查个人/特殊成就
        for a in self.pm.check_verse_achievements(uid, uname):
            msgs.append(("text", f"🏆 {uname} 达成成就「{ACHIEVEMENTS.get(a, (a,''))[0]}」！"))
        ok, err, comp, all_correct = engine.guess(clean)
        if not ok:
            return _fail(err)
        # 追踪本局声母/韵母使用
        engine.user_initials.setdefault(uid, set())
        engine.user_finals.setdefault(uid, set())
        guess_parts = engine.history[-1][1] if engine.history else []
        for gp in guess_parts:
            if gp.get("initial"):
                engine.user_initials[uid].add(gp["initial"])
            if gp.get("final"):
                engine.user_finals[uid].add(gp["final"])
        # 一事无成
        if comp and all(
            c is not None and c.get("char") == "absent"
            and c.get("initial") == "absent" and c.get("final") == "absent"
            for c in comp
        ):
            if self.pm.unlock_achievement(uid, "all_gray", uname):
                msgs.append(("text", self._achieve_msg(uid, "all_gray")))
        # 旗开得胜
        if len(engine.history) == 1:
            has_char = any(c is not None and c.get("char") in ("correct", "present") for c in comp)
            has_pinyin = any(
                c is not None and c.get("initial") in ("correct", "present")
                and c.get("final") in ("correct", "present")
                for c in comp
            )
            if has_char or has_pinyin:
                if self.pm.unlock_achievement(uid, "first_hit_char", uname):
                    msgs.append(("text", self._achieve_msg(uid, "first_hit_char")))
        img_path = os.path.join(str(self.plugin_data_dir), f"verse_{session_id}.png")
        render_grid(engine, img_path, max_attempts=None, hint_mode=engine.hint_mode)
        msgs.append(("image", img_path))
        finished = False
        if all_correct:
            ans_path = os.path.join(str(self.plugin_data_dir), f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            msgs.append(("image", ans_path))
            n_participants = len(getattr(engine, "participants", set()))
            msgs.append(("text", f"🎉 猜中了！{engine.target_text}\n（本局参与 {n_participants} 人）"))
            for m in self._settle_guess_verse_achievements(engine, uid, uname):
                msgs.append(("text", m))
            # 道具掉落：胜者按自身猜测次数概率，败者(其他参与者)固定 5%
            wg = engine.user_guesses.get(uid, 0) if hasattr(engine, "user_guesses") else len(engine.history)
            win_item = roll_win_item(wg, "verse")
            if win_item:
                self.pm.add_item(uid, win_item, 1, uname)
                msgs.append(("text", f"🎁 {uname} 获得道具【{win_item}】！"))
            for puid in list(participants):
                if str(puid) == str(uid):
                    continue
                loser_item = roll_loser_item("verse")
                if loser_item:
                    pname = self._uid_name(puid)
                    self.pm.add_item(puid, loser_item, 1, pname)
                    msgs.append(("text", f"🎁 {pname} 获得道具【{loser_item}】！"))
            self.guess_verse_sessions.pop(session_id, None)
            finished = True
        elif engine.is_finished():
            ans_path = os.path.join(str(self.plugin_data_dir), f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            msgs.append(("image", ans_path))
            msgs.append(("text", f"机会耗尽！正确诗句：{engine.target_text}"))
            self.guess_verse_sessions.pop(session_id, None)
            finished = True
        else:
            msgs.append(("text", f"继续猜！已猜 {len(engine.history)} 次（不限次数）"))
        return {"ok": True, "err": None, "comp": comp, "all_correct": all_correct,
                "finished": finished, "msgs": msgs}

    def _settle_guess_verse_achievements(self, engine, winner_uid, winner_name):
        """猜诗句猜中后结算所有参与者成就。返回提示消息列表。"""
        msgs = []
        participants = getattr(engine, "participants", set())
        user_guesses = getattr(engine, "user_guesses", {})
        n = len(participants)
        total = len(engine.history)
        # 本局答案汉字（供季节/内容成就检测）
        target_hanzi = getattr(engine, "target_hanzi", "") or ""
        # 本局所有猜测文本拼接（供「春日影」检测：春/日/影 三字都出现过）
        all_guess_text = ""
        for _t, _p, _c in getattr(engine, "history", []):
            all_guess_text += _t or ""
        # 日志：本局参与人数与参与者，便于排查成就触发
        logger.info(f"[成就] 猜诗句结算：本局参与 {n} 人 → {sorted(participants)}")

        # 道法自然：非成功猜测(≥2条)均出自同一首诗
        dao_trigger = False
        non_win_guesses = [t for t, _p, _c in getattr(engine, "history", [])]
        if len(non_win_guesses) >= 3 and len(engine.history) >= 2:
            # 去掉最后一条（成功的）
            non_win = engine.history[:-1]
            if len(non_win) >= 2:
                common = None
                for t, _p, _c in non_win:
                    titles = set(self._poem_titles_of(t or ""))
                    if not titles:
                        common = None
                        break
                    common = titles if common is None else (common & titles)
                    if not common:
                        break
                dao_trigger = bool(common)
        crychic_trigger = n >= 3 and all(ch in all_guess_text for ch in "苦来兮")

        for p_uid in participants:
            p_name = self._uid_name(p_uid)
            pm = self.pm
            # 参与人数成就
            counts = {1: "solo_pass", 2: "double_pass", 3: "triple_pass",
                      4: "four_scholar", 5: "five_poem", 6: "six_scholar",
                      7: "seven_sage", 8: "eight_scholar"}
            if n in counts:
                if pm.unlock_achievement(p_uid, counts[n], p_name):
                    msgs.append(self._achieve_msg(p_uid, counts[n]))
            # 极简主义
            if total <= 10:
                if pm.unlock_achievement(p_uid, "minimalist", p_name):
                    msgs.append(self._achieve_msg(p_uid, "minimalist"))
            # 时段成就
            h = time.localtime().tm_hour
            if h >= 23 or h < 5:
                if pm.unlock_achievement(p_uid, "night_owl", p_name):
                    msgs.append(self._achieve_msg(p_uid, "night_owl"))
            elif 5 <= h < 8:
                if pm.unlock_achievement(p_uid, "early_bird", p_name):
                    msgs.append(self._achieve_msg(p_uid, "early_bird"))
            # 坚持不懈（单局个人≥20次，无论是否猜中）
            if user_guesses.get(p_uid, 0) >= 20:
                if pm.unlock_achievement(p_uid, "persistent", p_name):
                    msgs.append(self._achieve_msg(p_uid, "persistent"))
            # 于无声处听惊雷 / 绕梁余韵：本局使用全部声母/韵母
            user_initials = getattr(engine, "user_initials", {}).get(p_uid, set())
            user_finals = getattr(engine, "user_finals", {}).get(p_uid, set())
            if user_initials >= set(INITIALS_LIST):
                if pm.unlock_achievement(p_uid, "all_initials", p_name):
                    msgs.append(self._achieve_msg(p_uid, "all_initials"))
            if user_finals >= set(FINALS_LIST):
                if pm.unlock_achievement(p_uid, "all_finals", p_name):
                    msgs.append(self._achieve_msg(p_uid, "all_finals"))
            # 怀民亦未寝：多人夜间完成
            if n >= 2 and (h >= 23 or h < 5):
                if pm.unlock_achievement(p_uid, "night_group", p_name):
                    msgs.append(self._achieve_msg(p_uid, "night_group"))
            # 参与局数
            pm.inc_stat(p_uid, "guess_games", 1, p_name)
            # 个人诗句/字数成就
            for a in pm.check_verse_achievements(p_uid, p_name):
                msgs.append(self._achieve_msg(p_uid, a))
            # 挚爱诗句：使用≥50次且为最高频诗句（动态更新）
            beloved = pm.check_beloved_verse(p_uid, p_name)
            if beloved:
                bv, bc = beloved
                msgs.append(f"🏆 {p_name} 达成成就「挚爱诗句-{bv}」！（使用 {bc} 次）")
            # 诗词内容 · 季节成就
            season_map = {"春": 1, "夏": 2, "秋": 4, "冬": 8}
            season_ach = {"春": "spring_redemption", "夏": "summer_apprentice",
                          "秋": "autumn_corpse", "冬": "winter_breath"}
            for s, bit in season_map.items():
                if s in target_hanzi:
                    if pm.unlock_achievement(p_uid, season_ach[s], p_name):
                        msgs.append(self._achieve_msg(p_uid, season_ach[s]))
                    # 斯蒂芬·金：集齐四季
                    if pm.collect_season(p_uid, bit, p_name):
                        msgs.append(self._achieve_msg(p_uid, "king_stephen"))
            # 《春日影》：≥3人参与 且 本局猜测中出现过「春」「日」「影」三字
            if n >= 3 and all(ch in all_guess_text for ch in "春日影"):
                if pm.unlock_achievement(p_uid, "spring_film", p_name):
                    msgs.append(self._achieve_msg(p_uid, "spring_film"))
            # CRYCHIC：≥3人参与 且 出现过「苦」「来」「兮」
            if crychic_trigger:
                if pm.unlock_achievement(p_uid, "crychic", p_name):
                    msgs.append(self._achieve_msg(p_uid, "crychic"))
            # 道法自然：非成功猜测均出自同一首诗（≥2条）
            if dao_trigger:
                if pm.unlock_achievement(p_uid, "dao_fa_zi_ran", p_name):
                    msgs.append(self._achieve_msg(p_uid, "dao_fa_zi_ran"))
            # 赢家统计
            if p_uid == winner_uid:
                pm.inc_stat(p_uid, "guess_wins", 1, p_name)
                # 一发入魂：开局首句（本局第1条合法猜测）即猜中
                if total == 1:
                    if pm.unlock_achievement(p_uid, "first_hit", p_name):
                        msgs.append(self._achieve_msg(p_uid, "first_hit"))
                # 摘桃子：多人在场时本局仅发送一句即猜中
                if n >= 2 and user_guesses.get(p_uid, 0) == 1:
                    if pm.unlock_achievement(p_uid, "peach_picker", p_name):
                        msgs.append(self._achieve_msg(p_uid, "peach_picker"))
                # 收尾人计数（升级制，从成就 progress 累计）
                ach_closer = pm.get_achievements(p_uid).get("closer", {})
                cc = ach_closer.get("progress", 0) + 1
                new_lv = pm.check_closer(p_uid, cc, p_name)
                if new_lv:
                    msgs.append(f"🏆 {p_name} 达成成就「{new_lv}」！")
                # 神机妙算：猜诗句累计获胜 10 场
                st = pm.load(p_uid).get("stats", {})
                if st.get("guess_wins", 0) >= 10:
                    if pm.unlock_achievement(p_uid, "guess_win_10", p_name):
                        msgs.append(self._achieve_msg(p_uid, "guess_win_10"))
        return msgs

    def _settle_duel_achievements(self, duel, engine, winner_side, winner_uid):
        """对垒分出胜负后结算成就。返回提示消息列表。"""
        msgs = []
        a_id, b_id = engine.a_id, engine.b_id
        loser_id = b_id if winner_side == "a" else a_id
        gc = duel.get("guess_counts", {})
        win_guesses = gc.get(str(winner_uid), 0)

        # 胜者统计 + 成就
        self.pm.inc_stat(winner_uid, "duel_wins", 1, self._uid_name(winner_uid))
        self.pm.inc_stat(a_id, "duel_games", 1, self._uid_name(a_id))
        self.pm.inc_stat(b_id, "duel_games", 1, self._uid_name(b_id))
        # 时段成就
        h = time.localtime().tm_hour
        for p in (a_id, b_id):
            ach = None
            if h >= 23 or h < 5:
                ach = "night_owl"
            elif 5 <= h < 8:
                ach = "early_bird"
            if ach and self.pm.unlock_achievement(p, ach, self._uid_name(p)):
                msgs.append(self._achieve_msg(p, ach))
        # 怀民亦未寝：夜间对垒完成（双人）
        if h >= 23 or h < 5:
            for p in (a_id, b_id):
                if self.pm.unlock_achievement(p, "night_group", self._uid_name(p)):
                    msgs.append(self._achieve_msg(p, "night_group"))
        # 于无声处听惊雷 / 绕梁余韵：获胜方本局使用全部声母/韵母
        win_name = self._uid_name(winner_uid)
        win_initials = duel.get("user_initials", {}).get(winner_uid, set())
        win_finals = duel.get("user_finals", {}).get(winner_uid, set())
        if win_initials >= set(INITIALS_LIST):
            if self.pm.unlock_achievement(winner_uid, "all_initials", win_name):
                msgs.append(self._achieve_msg(winner_uid, "all_initials"))
        if win_finals >= set(FINALS_LIST):
            if self.pm.unlock_achievement(winner_uid, "all_finals", win_name):
                msgs.append(self._achieve_msg(winner_uid, "all_finals"))
        # 速通 / 开了！
        if win_guesses <= 10:
            if self.pm.unlock_achievement(winner_uid, "duel_speed", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_speed"))
        if win_guesses <= 5:
            if self.pm.unlock_achievement(winner_uid, "duel_open", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_open"))
        # 先手/后手
        if winner_side == "a" and self.pm.unlock_achievement(a_id, "first_mover", self._uid_name(a_id)):
            msgs.append(self._achieve_msg(a_id, "first_mover"))
        elif winner_side == "b" and self.pm.unlock_achievement(b_id, "second_mover", self._uid_name(b_id)):
            msgs.append(self._achieve_msg(b_id, "second_mover"))
        # 常胜将军 / 百战不殆：对垒累计胜场
        win_stats = self.pm.load(winner_uid).get("stats", {})
        dw = win_stats.get("duel_wins", 0)
        if dw >= 5:
            if self.pm.unlock_achievement(winner_uid, "duel_win_5", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_win_5"))
        if dw >= 10:
            if self.pm.unlock_achievement(winner_uid, "duel_win_10", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_win_10"))
        # 连胜追踪：胜者连胜 +1，败者清零（记录历史最高连胜）
        cur_streak = win_stats.get("duel_streak", 0) + 1
        win_stats["duel_streak"] = cur_streak
        win_stats["max_duel_streak"] = max(win_stats.get("max_duel_streak", 0), cur_streak)
        self.pm.save(winner_uid)
        new_streak = self.pm.check_duel_streak(winner_uid, cur_streak, self._uid_name(winner_uid))
        if new_streak:
            msgs.append(f"🏆 {self._uid_name(winner_uid)} 达成成就「{new_streak}」！")
        los_stats = self.pm.load(loser_id).get("stats", {})
        los_stats["duel_streak"] = 0
        self.pm.save(loser_id)
        # 复仇者：上局输给 loser_id，本局作为 winner_uid 赢回
        prev = self.pm.load(winner_uid).get("stats", {}).get("last_duel_lost_to")
        if prev == loser_id:
            if self.pm.unlock_achievement(winner_uid, "avenger", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "avenger"))
        # 记录输家输给谁（供下局复仇判定）
        self.pm.load(loser_id).setdefault("stats", {})["last_duel_lost_to"] = winner_uid
        self.pm.save(loser_id)
        # 挚爱诗句：使用≥50次且为最高频诗句（双方都检查，动态更新）
        for p in (a_id, b_id):
            beloved = self.pm.check_beloved_verse(p, self._uid_name(p))
            if beloved:
                bv, bc = beloved
                msgs.append(f"🏆 {self._uid_name(p)} 达成成就「挚爱诗句-{bv}」！（使用 {bc} 次）")
        # 道具掉落：胜者按猜测次数概率，败者固定 5%
        win_item = roll_win_item(win_guesses, "duel")
        if win_item:
            self.pm.add_item(winner_uid, win_item, 1, self._uid_name(winner_uid))
            msgs.append(f"🎁 {self._uid_name(winner_uid)} 获得道具【{win_item}】！")
        loser_item = roll_loser_item("duel")
        if loser_item:
            self.pm.add_item(loser_id, loser_item, 1, self._uid_name(loser_id))
            msgs.append(f"🎁 {self._uid_name(loser_id)} 获得道具【{loser_item}】！")
        return msgs

    def _check_soulmate(self, duel, a_id, b_id):
        """心有灵犀：双方所出诗句是否出自同一首诗词。返回提示消息列表。"""
        msgs = []
        a_puzzle = duel.get("puzzles", {}).get(a_id, "")
        b_puzzle = duel.get("puzzles", {}).get(b_id, "")
        if not self.db or not a_puzzle or not b_puzzle:
            return msgs
        try:
            a_titles = set(self._poem_titles_of(a_puzzle))
            b_titles = set(self._poem_titles_of(b_puzzle))
            if a_titles & b_titles:
                for p in (a_id, b_id):
                    if self.pm.unlock_achievement(p, "soulmate", self._uid_name(p)):
                        msgs.append(self._achieve_msg(p, "soulmate"))
        except Exception:
            pass
        return msgs

    def _check_duel_puzzle_achievements(self, duel, a_id, b_id):
        """对垒出题关系成就：双方题目的作者/朝代/相同字/月花酒山江。返回提示消息列表。"""
        msgs = []
        a_puzzle = duel.get("puzzles", {}).get(a_id, "")
        b_puzzle = duel.get("puzzles", {}).get(b_id, "")
        if not a_puzzle or not b_puzzle:
            return msgs
        a_hanzi = set(extract_hanzi(a_puzzle))
        b_hanzi = set(extract_hanzi(b_puzzle))
        # 相同汉字（一字之缘）
        if a_hanzi & b_hanzi:
            for p in (a_id, b_id):
                if self.pm.unlock_achievement(p, "duel_common_char", self._uid_name(p)):
                    msgs.append(self._achieve_msg(p, "duel_common_char"))
        # 月/花/酒/山/江：双方都含该字
        both_char_map = {
            "月": "duel_both_moon", "花": "duel_both_flower", "酒": "duel_both_wine",
            "山": "duel_both_mountain", "江": "duel_both_river",
        }
        for ch, ach in both_char_map.items():
            if ch in a_hanzi and ch in b_hanzi:
                for p in (a_id, b_id):
                    if self.pm.unlock_achievement(p, ach, self._uid_name(p)):
                        msgs.append(self._achieve_msg(p, ach))
        # 作者/朝代
        if self.db:
            try:
                meta_a = self.db.check_exact_poetry(a_puzzle)  # (title, author, dynasty)
                meta_b = self.db.check_exact_poetry(b_puzzle)
                if meta_a and meta_b:
                    _, author_a, dynasty_a = meta_a
                    _, author_b, dynasty_b = meta_b
                    # 同作者：排除佚名/空
                    if author_a and author_b and author_a != "佚名" and author_b != "佚名" and author_a == author_b:
                        for p in (a_id, b_id):
                            if self.pm.unlock_achievement(p, "duel_same_author", self._uid_name(p)):
                                msgs.append(self._achieve_msg(p, "duel_same_author"))
                    # 同朝代：非空且相等
                    if dynasty_a and dynasty_b and dynasty_a == dynasty_b:
                        for p in (a_id, b_id):
                            if self.pm.unlock_achievement(p, "duel_same_dynasty", self._uid_name(p)):
                                msgs.append(self._achieve_msg(p, "duel_same_dynasty"))
            except Exception:
                pass
        return msgs

    def _apply_duel_guess(self, duel, sid, uid, uname, clean):
        """执行对垒猜测的核心逻辑（库校验 + 状态推进 + 结算 + 渲染）。返回结果 dict，不发送消息。

        clean: 已去掉 cc 前缀的猜测文本。
        返回 {"ok", "err", "comp", "all_correct", "finished", "msgs": [(kind, payload)]}
        kind ∈ "text"|"image"。
        """
        msgs = []
        engine = duel["engine"]
        eff = duel.setdefault("item_effects", {"skip": {}, "immune": {}, "gamble": {}, "dream": {}})
        hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)
        side = "a" if uid == engine.a_id else "b"
        target_punct = engine.a_target_punct if side == "a" else engine.b_target_punct
        target_len = len(engine.a_target_hanzi if side == "a" else engine.b_target_hanzi)

        def _fail(err):
            return {"ok": False, "err": err, "comp": None, "all_correct": False,
                    "finished": False, "msgs": msgs}

        if not hanzi or len(hanzi) != target_len:
            return _fail(f"字数不符（需 {target_len} 字）")
        # 库校验（两句需为库中相邻两句）
        if target_punct:
            segs = re.split(r'[，。！？、；：]', clean)
            segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
            if len(segs) != 2 or not self.db or not self.db.is_adjacent_pair(segs[0], segs[1]):
                return _fail(f"「{clean}」未在诗词库中（需为库中相邻两句）")
        else:
            if not self._is_in_library(clean):
                return _fail(f"「{clean}」不在诗词库中，请输入曲库诗句")
        ok, err, side, comp, all_correct = engine.guess(uid, clean)
        if not ok:
            return _fail(err)
        # 记录猜测诗句到个人数据（仅合法猜测）
        self.pm.record_verse(uid, clean, uname)
        self.pm.inc_stat(uid, "total_guesses", 1, uname)
        # 🐖 重复诗句检测（本局内自己发过的纯汉字）
        if "user_verses" not in duel:
            duel["user_verses"] = {}
        duel["user_verses"].setdefault(uid, set())
        if hanzi in duel["user_verses"][uid]:
            pig_count = self.pm.add_pig(uid, uname)
            msgs.append(("text", f"🐖 {uname} 重复诗句！猪+1（{'🐖' * pig_count}）"))
        else:
            duel["user_verses"][uid].add(hanzi)
        # 追踪对垒双方猜测次数
        if "guess_counts" not in duel:
            duel["guess_counts"] = {}
        duel["guess_counts"][uid] = duel["guess_counts"].get(uid, 0) + 1
        # 追踪本局声母/韵母使用
        if "user_initials" not in duel:
            duel["user_initials"] = {}
            duel["user_finals"] = {}
        duel["user_initials"].setdefault(uid, set())
        duel["user_finals"].setdefault(uid, set())
        side_parts = []
        if side == "a" and engine.a_history:
            side_parts = engine.a_history[-1][1]
        elif side == "b" and engine.b_history:
            side_parts = engine.b_history[-1][1]
        for gp in side_parts:
            if gp.get("initial"):
                duel["user_initials"][uid].add(gp["initial"])
            if gp.get("final"):
                duel["user_finals"][uid].add(gp["final"])
        # 一事无成
        if comp and all(
            c is not None and c.get("char") == "absent"
            and c.get("initial") == "absent" and c.get("final") == "absent"
            for c in comp
        ):
            if self.pm.unlock_achievement(uid, "all_gray", uname):
                msgs.append(("text", self._achieve_msg(uid, "all_gray")))
        # 旗开得胜
        if len(engine.a_history) + len(engine.b_history) == 1:
            has_char = any(c is not None and c.get("char") in ("correct", "present") for c in comp)
            has_pinyin = any(
                c is not None and c.get("initial") in ("correct", "present")
                and c.get("final") in ("correct", "present")
                for c in comp
            )
            if has_char or has_pinyin:
                if self.pm.unlock_achievement(uid, "first_hit_char", uname):
                    msgs.append(("text", self._achieve_msg(uid, "first_hit_char")))
        img_path = os.path.join(str(self.plugin_data_dir), f"duel_{sid}.png")
        render_duel(engine, img_path, hint_mode=duel.get("hint_mode", "pinyin"))
        msgs.append(("image", img_path))
        finished = False
        immune_opp = "b" if side == "a" else "a"  # 被猜中题的一方（题目归属方）
        if all_correct and eff["immune"].get(immune_opp, 0) > 0:
            # 百战不殆：被猜中方免疫一次，不结束，给其反杀回合
            eff["immune"][immune_opp] -= 1
            engine.winner = None
            left = eff["immune"].get(immune_opp, 0)
            msgs.append(("text", f"🛡 对方使用【百战不殆】免疫了这次命中！（剩余 {left} 次）"))
            engine.switch_turn()
            msgs.append(("text", f"轮到 {engine.current_name()}。"))
        elif all_correct:
            wname = engine.side_name(side)
            win_text = (
                f"🏆 {wname} 猜中了对方的诗句！\n"
                f"{engine.a_name} 的题：{engine.a_puzzle}\n"
                f"{engine.b_name} 的题：{engine.b_puzzle}"
            )
            for m in self._settle_duel_achievements(duel, engine, side, uid):
                win_text += "\n" + m
            msgs.append(("text", win_text))
            self.duel_sessions.pop(sid, None)
            finished = True
        else:
            # 孤注一掷：出手方连续多次未命中
            gamble = eff["gamble"].get(side)
            if gamble and gamble.get("active"):
                left = int(gamble.get("left", 0)) - 1
                if left <= 0:
                    eff["gamble"][side]["active"] = False
                    eff["gamble"][side]["left"] = 0
                    # 孤注耗尽仍未中 -> 判出手方失败
                    loser_name = self._uid_name(uid)
                    win_name = self._uid_name(engine.a_id if side == "b" else engine.b_id)
                    msgs.append(("text", f"🎲 【孤注一掷】耗尽仍未猜中，{loser_name} 判定失败，{win_name} 获胜！"))
                    self.duel_sessions.pop(sid, None)
                    finished = True
                else:
                    eff["gamble"][side]["left"] = left
                    msgs.append(("text", f"🎲 【孤注一掷】继续你的回合（还剩 {left} 次）。"))
                if finished:
                    return {"ok": True, "err": None, "comp": comp, "all_correct": False,
                            "finished": True, "msgs": msgs}
            else:
                engine.switch_turn()
                gc = duel.get("guess_counts", {})
                if gc.get(engine.a_id, 0) >= 20 and gc.get(engine.b_id, 0) >= 20:
                    for p in (engine.a_id, engine.b_id):
                        if self.pm.unlock_achievement(p, "too_dark", self._uid_name(p)):
                            msgs.append(("text", self._achieve_msg(p, "too_dark")))
                msgs.append(("text", f"轮到 {engine.current_name()}。"))
        return {"ok": True, "err": None, "comp": comp, "all_correct": all_correct,
                "finished": finished, "msgs": msgs}

    def _maybe_bot_duel_turn(self, duel, sid, event=None):
        """对垒猜测后，若轮到 bot 且未结束，触发 bot 思考并猜测。"""
        if not self.ai_bot.enabled:
            return
        engine = duel.get("engine")
        if not engine:
            return
        if not engine.is_turn(BOT_ID):
            return
        if duel.get("bot_thinking"):
            return
        duel["bot_thinking"] = True
        origin = duel.get("group_origin")
        asyncio.create_task(self._bot_duel_think(duel, sid, origin, event))

    async def _bot_duel_think(self, duel, sid, origin, event=None):
        """bot 思考并提交对垒猜测，随后发消息到群。"""
        self.ai_bot._origin = origin
        try:
            engine = duel.get("engine")
            if not engine:
                return
            guess, speech = await self.ai_bot.think_and_guess_duel(duel, engine, event)
            if not guess:
                if origin and speech:
                    await self.context.send_message(origin, MessageChain([Plain(speech)]))
                return
            result = self._apply_duel_guess(duel, sid, BOT_ID, self.ai_bot.bot_name, guess)
            if origin:
                if speech:
                    await self.context.send_message(origin, MessageChain([Plain(speech)]))
                for kind, payload in result["msgs"]:
                    if kind == "text":
                        await self.context.send_message(origin, MessageChain([Plain(payload)]))
                    else:
                        await self.context.send_message(origin, MessageChain([Image.fromFileSystem(payload)]))
        except Exception as e:
            logger.error(f"[ai_bot] 对垒 bot 思考失败: {e}")
            if origin:
                try:
                    await self.context.send_message(origin, MessageChain([Plain("（AI 思考出错了，跳过本回合）")]))
                except Exception:
                    pass
        finally:
            duel["bot_thinking"] = False

    def _start_duel_playing(self, duel, sid):
        """双方出题完成，进入互猜阶段。群通知 + 成就消息主动发送。返回 engine。"""
        a_id = duel["challenger_id"]
        b_id = duel["opponent_id"]
        engine = DuelVerseEngine(
            duel["puzzles"][a_id], duel["puzzles"][b_id],
            a_id, duel["challenger_name"], b_id, duel["opponent_name"],
        )
        duel["engine"] = engine
        duel["state"] = "playing"
        soulmate_msgs = self._check_soulmate(duel, a_id, b_id)
        puzzle_msgs = self._check_duel_puzzle_achievements(duel, a_id, b_id)
        origin = duel.get("group_origin")
        if origin:
            lines = [
                "🍵 双方已出题！开始互猜！",
                f"{duel['challenger_name']} 猜 {duel['opponent_name']} 的题，{duel['opponent_name']} 猜 {duel['challenger_name']} 的题。",
                f"先轮到：{engine.current_name()}（发送「cc 诗句」猜测）",
            ]
            lines += soulmate_msgs + puzzle_msgs
            try:
                asyncio.create_task(self.context.send_message(origin, MessageChain([Plain("\n".join(lines))])))
            except Exception as e:
                logger.error(f"[duel] 群通知发送失败: {e}")
        return engine

    async def _bot_do_puzzle(self, sid, fmt, event=None):
        """bot 出题（对垒）。出题后校验格式与库，双方都出题则进入 playing。"""
        try:
            duel = self.duel_sessions.get(sid)
            if not duel:
                return
            puzzle = await self.ai_bot.think_puzzle(fmt, event)
            if puzzle:
                hanzi = re.sub(r'[^\u4e00-\u9fff]', '', puzzle)
                if fmt[0] == "single":
                    if len(hanzi) != fmt[1] or not self._is_in_library(puzzle):
                        puzzle = None
                else:
                    a_len, b_len = fmt[1]
                    segs = re.split(r'[，。！？、；：]', puzzle)
                    segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
                    if len(segs) != 2 or len(segs[0]) != a_len or len(segs[1]) != b_len or not self.db or not self.db.is_adjacent_pair(segs[0], segs[1]):
                        puzzle = None
            if not puzzle:
                # fallback 总库随机
                try:
                    if fmt[0] == "single":
                        cands = self.db.get_random_verse(fmt[1], fmt[1], target_count=10)
                    else:
                        cands = self.db.get_random_verse_by_combo(fmt[1][0], fmt[1][1], target_count=10)
                    if cands:
                        import random as _r
                        puzzle = _r.choice(cands)[0]
                except Exception:
                    puzzle = None
            if not puzzle:
                logger.error("[ai_bot] bot 出题失败，无可用诗句")
                return
            duel = self.duel_sessions.get(sid)
            if not duel:
                return
            duel["puzzles"][BOT_ID] = puzzle
            duel["puzzle_done"].add(BOT_ID)
            self.pm.record_verse(BOT_ID, puzzle, self.ai_bot.bot_name)
            if len(duel["puzzle_done"]) >= 2:
                engine = self._start_duel_playing(duel, sid)
                if engine and engine.is_turn(BOT_ID):
                    self._maybe_bot_duel_turn(duel, sid, event)
        except Exception as e:
            logger.error(f"[ai_bot] bot 出题异常: {e}")

    # ============ 猜诗句 bot 帮猜 ============

    def _bot_self_id(self, event):
        try:
            return str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        except Exception:
            return ""

    def _is_at_bot(self, event):
        """判断消息是否 @ 了机器人自身。"""
        at_id = self._extract_at_id(event)
        if not at_id:
            return False
        self_id = self._bot_self_id(event)
        return bool(self_id) and at_id == self_id

    def _is_bot_trigger(self, event, msg_raw):
        """判断是否触发猜诗句 bot 帮猜（@机器人 或自定义触发词）。"""
        if self._is_at_bot(event):
            return True
        for w in self._ai_bot_trigger_words:
            if w and w in msg_raw:
                return True
        return False

    def _try_bot_verse_guess(self, event, session_id):
        """触发猜诗句 bot 猜一句（含冷却锁）。返回 True 表示已处理（阻断后续）。"""
        engine = self.guess_verse_sessions.get(session_id)
        if not engine:
            return False
        # 冷却检查
        now = time.time()
        if now < self._ai_bot_cooldown_until:
            return False
        if getattr(engine, "bot_thinking", False):
            return True
        engine.bot_thinking = True
        self._ai_bot_cooldown_until = now + self.ai_bot.cooldown
        origin = getattr(event, "unified_msg_origin", None)
        asyncio.create_task(self._bot_verse_think(engine, session_id, origin, event))
        return True

    async def _bot_verse_think(self, engine, session_id, origin, event=None):
        """bot 思考并提交猜诗句猜测，随后发消息到群。"""
        self.ai_bot._origin = origin
        try:
            guess, speech = await self.ai_bot.think_and_guess_verse(engine, session_id, event)
            if not guess:
                if origin and speech:
                    await self.context.send_message(origin, MessageChain([Plain(speech)]))
                return
            result = self._apply_verse_guess(engine, session_id, BOT_ID, self.ai_bot.bot_name, guess)
            if origin:
                if speech:
                    await self.context.send_message(origin, MessageChain([Plain(speech)]))
                for kind, payload in result["msgs"]:
                    if kind == "text":
                        await self.context.send_message(origin, MessageChain([Plain(payload)]))
                    else:
                        await self.context.send_message(origin, MessageChain([Image.fromFileSystem(payload)]))
        except Exception as e:
            logger.error(f"[ai_bot] 猜诗句 bot 思考失败: {e}")
            if origin:
                try:
                    await self.context.send_message(origin, MessageChain([Plain("（AI 思考出错了）")]))
                except Exception:
                    pass
        finally:
            engine.bot_thinking = False

    def _poem_titles_of(self, text):
        """返回包含该诗句（分句）的所有诗词标题。"""
        titles = []
        if not self.db:
            return titles
        clauses = re.split(r'[，。！？、；：]', text)
        clauses = [re.sub(r'[^\u4e00-\u9fff]', '', c) for c in clauses if re.sub(r'[^\u4e00-\u9fff]', '', c)]
        if not clauses:
            return titles
        import sqlite3
        try:
            conn = sqlite3.connect(self.db.db_path)
            cur = conn.cursor()
            cur.execute("SELECT title FROM poems WHERE content LIKE ? LIMIT 50", (f'%{clauses[0]}%',))
            titles = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception:
            pass
        return titles

    # ==========================================
    # 全局监听分发中枢
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_recv_msg(self, event: AstrMessageEvent):
        msg_raw = event.message_str.strip()
        # 🍵 诗词对垒处理（优先，含确认/私聊出题/群聊猜测）
        is_private = bool(event.is_private_chat()) if hasattr(event, "is_private_chat") else False
        if msg_raw.startswith("cc") or self.duel_sessions:
            handled = [False]
            async for result in self._handle_duel_message(event, msg_raw, is_private, handled):
                yield result
            if handled[0]:
                return
        if msg_raw.startswith(("(", "（")) and msg_raw.endswith((")", "）")): return
        if not msg_raw or msg_raw.startswith(("/", "查询", "生成战报", "恢复", "结束", "纵横", "衔字", "蛇形", "删除", "安装", "bot")): return
        # 显式排除猜诗句相关指令词，避免被当作猜测
        if msg_raw in ("猜诗句", "猜诗句帮助", "结束猜诗句", "猜诗句规则") or msg_raw.startswith("猜诗句 "): return

        session_id = str(event.get_group_id() or event.get_session_id())

        # ⚔️ 邀战猜诗词处理
        if session_id in self.battle_sessions:
            b = self.battle_sessions[session_id]
            uid = str(event.get_sender_id())

            # 等待确认阶段
            if b["state"] == "waiting_confirm":
                # 惰性超时检查（2 分钟）
                if time.time() - b.get("created_at", 0) > 120:
                    self.battle_sessions.pop(session_id)
                    yield event.plain_result("⏰ 挑战超时（2 分钟未响应），已自动取消。")
                    return
                if uid != b["opponent_id"]:
                    return
                if msg_raw in ("接受", "同意", "应战"):
                    poem = pick_battle_target(self.classic_poems)
                    if not poem:
                        self.battle_sessions.pop(session_id)
                        yield event.plain_result("❌ 未找到合适的出题诗句。")
                        return
                    engine = BattleVerseEngine(poem, b["challenger_id"], b["challenger_name"], b["opponent_id"], b["opponent_name"])
                    b["engine"] = engine
                    b["state"] = "playing"
                    img_path = os.path.join(str(self.plugin_data_dir), f"battle_{session_id}.png")
                    render_battle(engine, img_path)
                    yield event.plain_result(
                        f"⚔️ 对战开始！\n"
                        f"答案结构：{len(engine.first)} 字 + {len(engine.second)} 字（前句/后句各半）\n"
                        f"1号 {engine.first_name} 猜【前句】，2号 {engine.second_name} 猜【后句】\n"
                        f"先轮到：{engine.current_name()}"
                    )
                    yield event.image_result(img_path)
                    return
                elif msg_raw in ("拒绝", "拒绝挑战", "不接受"):
                    self.battle_sessions.pop(session_id)
                    yield event.plain_result(f"{b['opponent_name']} 拒绝了挑战。")
                    return
                return

            # 对战阶段
            if b["state"] == "playing":
                engine = b["engine"]
                if uid not in (engine.first_id, engine.second_id):
                    return
                if not engine.is_turn(uid):
                    # 没轮到的人静默
                    return
                clean = re.sub(r'[^\u4e00-\u9fff]', '', msg_raw)
                if not clean or len(clean) != len(engine.first_parts):
                    return
                # 命中半句集合才当猜测，否则静默
                if clean not in self.battle_half_set:
                    yield event.plain_result(
                        f"「{clean}」不在经典诗词库中，请输入完整的半句诗句进行猜测。"
                    )
                    return
                # 记录猜测诗句到个人数据
                self.pm.record_verse(uid, clean, event.get_sender_name() or f"用户{uid}")
                ok, err, side, comp, all_correct = engine.guess(uid, msg_raw)
                if not ok:
                    yield event.plain_result(err)
                    return
                img_path = os.path.join(str(self.plugin_data_dir), f"battle_{session_id}.png")
                render_battle(engine, img_path)
                yield event.image_result(img_path)
                if all_correct:
                    wname = engine.first_name if engine.winner == "first" else engine.second_name
                    yield event.plain_result(
                        f"🏆 {wname} 猜中了！答案：{engine.first}，{engine.second}{engine.poem['end_punct']}"
                    )
                    self.battle_sessions.pop(session_id)
                else:
                    engine.switch_turn()
                    yield event.plain_result(f"轮到 {engine.current_name()}。")
                return

        # 🤖 AI bot 帮猜（@机器人 或自定义触发词）
        if self.ai_bot.enabled and session_id in self.guess_verse_sessions:
            if self._is_bot_trigger(event, msg_raw):
                if self._try_bot_verse_guess(event, session_id):
                    # 已触发 bot 帮猜，阻断默认 LLM/Agent 回复
                    try:
                        event.should_call_llm(True)
                    except Exception:
                        pass
                    return

        # 🎯 猜诗句游戏处理
        if session_id in self.guess_verse_sessions:
            engine = self.guess_verse_sessions[session_id]
            # 提示指令：显示声母韵母状态（仅拼音模式）
            if msg_raw in ("提示", "声韵提示", "拼音提示"):
                if engine.hint_mode != "pinyin":
                    yield event.plain_result("当前为部首提示模式，无拼音提示。")
                    return
                hint_path = os.path.join(str(self.plugin_data_dir), f"verse_hint_{session_id}.png")
                render_hint(engine, hint_path)
                yield event.image_result(hint_path)
                return
            # 猜测需 cc 前缀
            if not msg_raw.startswith("cc"):
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            uid = str(event.get_sender_id())
            uname = event.get_sender_name() or f"用户{uid}"
            result = self._apply_verse_guess(engine, session_id, uid, uname, clean)
            if not result["ok"]:
                yield event.plain_result(result["err"])
                return
            for kind, payload in result["msgs"]:
                if kind == "text":
                    yield event.plain_result(payload)
                else:
                    yield event.image_result(payload)
            return

        if session_id not in self.active_games: return

        engine = self.active_games[session_id]
        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()

        if msg_raw in ["加入", "+加入", "1+加入", "1 + 加入"]:
            response = engine.step("join", user_id, user_name)
        elif msg_raw in ["退出", "退出游戏"]:
            response = engine.step("quit", user_id, user_name)
        elif msg_raw in ["跳过", "催更", "超时"]:
            response = engine.step("skip", user_id, user_name)
        else:
            response = engine.step("play", user_id, user_name, msg_raw)

        if not response: return
        if response.get("status") == "ignore": return
        if response.get("msg"): yield event.plain_result(response["msg"])
        if "image" in response: yield event.image_result(response["image"])
