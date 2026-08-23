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

        engine = GuessVerseEngine(self.db, self.verse_max_attempts, self.verse_min_len, self.verse_max_len, classic_poems=self.classic_poems)
        ok, msg = engine.new_game()
        if not ok:
            yield event.plain_result(f"❌ 初始化失败：{msg}")
            return

        self.guess_verse_sessions[session_id] = engine
        yield event.plain_result(
            "🎯 【猜诗句】开始！\n"
            f"答案是一句 {len(engine.target_hanzi)} 字的完整诗句，你有 {self.verse_max_attempts} 次机会。\n"
            "发送对应字数的完整句进行猜测，如：床前明月光，疑是地上霜。\n"
            "每次猜测后，每个字的【汉字/声母/韵母/声调】四个属性独立显示颜色：\n"
            "🟢 绿色 = 正确且位置正确\n"
            "🟠 橙色 = 答案中存在但位置错误\n"
            "⚪ 灰色 = 答案中不存在\n"
            "标点符号不限，只要汉字字数一致即可。"
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
            "1. 系统随机选择一句 7-18 字的完整诗句作为答案。\n"
            "2. 发送与答案汉字字数一致的完整句（标点不限）进行猜测。\n"
            "3. 每次猜测后，每个字的【汉字/声母/韵母/声调】独立着色。\n"
            "4. 显示网格为答案字数，标点只作参考。\n"
            "5. 四个属性全绿即猜中！\n"
            "指令：/猜诗句 开始 | /结束猜诗句 退出"
        )
        yield event.plain_result(msg)

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

        target_id = self._extract_at_id(event)
        if not target_id:
            yield event.plain_result("请艾特要挑战的成员，如：/诗词对垒 @某人")
            return
        sender_id = str(event.get_sender_id())
        if target_id == sender_id:
            yield event.plain_result("不能挑战自己哦~")
            return

        target_name = f"用户{target_id}"
        try:
            info = await event.bot.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(target_id))
            if isinstance(info, dict) and "data" in info:
                info = info["data"]
            target_name = info.get("nickname") or info.get("card") or target_name
        except Exception:
            pass

        # 随机出题字数：4字15%、6字15%、5/7字平分剩余70%（各35%）
        import random as _r
        roll = _r.random()
        if roll < 0.15:
            word_len = 4
        elif roll < 0.30:
            word_len = 6
        elif roll < 0.65:
            word_len = 5
        else:
            word_len = 7

        self.duel_sessions[session_id] = {
            "state": "wait_confirm",
            "challenger_id": sender_id,
            "challenger_name": event.get_sender_name() or f"用户{sender_id}",
            "opponent_id": target_id,
            "opponent_name": target_name,
            "word_len": word_len,
            "puzzles": {},        # {user_id: sentence}
            "puzzle_done": set(), # 已出题的人
            "engine": None,
            "group_origin": getattr(event, "unified_msg_origin", None),
            "created_at": time.time(),
        }
        yield event.plain_result(
            f"🍵 【诗词对垒】\n"
            f"{event.get_sender_name()} 向 {target_name} 发起对垒！\n"
            f"双方各出 {word_len} 字诗句（曲库中，前缀「cc」）作为题目，随后互猜对方诗句，先猜中者获胜。\n"
            f"请 {target_name} 回复【接受】开始，或回复【拒绝】。（2 分钟内有效）"
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

        # 判断是否对垒参与者
        is_participant = uid in (duel.get("challenger_id"), duel.get("opponent_id"))
        if not is_participant:
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
            if uid != duel["opponent_id"]:
                # 挑战者消息：不吞，避免 /诗词对垒 命令消息产生空响应
                return
            if msg_raw in ("接受", "同意", "应战"):
                duel["state"] = "wait_puzzle"
                wl = duel["word_len"]
                hint = f"🍵 【诗词对垒】请发送一句 {wl} 字诗句作为你的题目（总库中，前缀「cc」）。\n例：cc 床前明月光"
                ok_a = await self._send_private(event.bot, duel["challenger_id"], hint)
                ok_b = await self._send_private(event.bot, duel["opponent_id"], hint)
                _block_llm()
                if ok_a and ok_b:
                    yield event.plain_result(f"🍵 对垒开始！已私聊双方提示出题（各 {wl} 字），出题完成后在群聊公开互猜。")
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
            if len(hanzi) != duel["word_len"]:
                _block_llm()
                yield event.plain_result(f"题目需为 {duel['word_len']} 字，当前 {len(hanzi)} 字。")
                return
            # 出题也校验总库，保证题目可被对方猜中
            if not self._is_in_library(clean):
                _block_llm()
                yield event.plain_result(f"「{clean}」不在诗词库中，请输入曲库诗句作为题目。")
                return
            duel["puzzles"][uid] = clean
            duel["puzzle_done"].add(uid)
            _block_llm()
            # 双方都出题后进入猜测阶段 —— 状态变更放在 yield 之前，确保执行
            if len(duel["puzzle_done"]) >= 2:
                a_id = duel["challenger_id"]
                b_id = duel["opponent_id"]
                engine = DuelVerseEngine(
                    duel["puzzles"][a_id], duel["puzzles"][b_id],
                    a_id, duel["challenger_name"],
                    b_id, duel["opponent_name"],
                )
                duel["engine"] = engine
                duel["state"] = "playing"
                origin = duel.get("group_origin")
                if origin:
                    try:
                        await self.context.send_message(origin, MessageChain([
                            Plain(f"🍵 双方已出题！开始互猜！\n"
                                  f"{duel['challenger_name']} 猜 {duel['opponent_name']} 的题，{duel['opponent_name']} 猜 {duel['challenger_name']} 的题。\n"
                                  f"先轮到：{engine.current_name()}（发送「cc 诗句」猜测）")
                        ]))
                    except Exception as e:
                        logger.error(f"[duel] 群通知发送失败: {e}")
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
            hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)
            if not hanzi or len(hanzi) != len(engine.target_parts_of(engine.current_side())):
                _block_llm()
                return
            if not self._is_in_library(clean):
                _block_llm()
                yield event.plain_result(f"「{clean}」不在诗词库中，请输入曲库诗句。")
                return
            ok, err, side, comp, all_correct = engine.guess(uid, clean)
            if not ok:
                _block_llm()
                yield event.plain_result(err)
                return
            img_path = os.path.join(str(self.plugin_data_dir), f"duel_{sid}.png")
            render_duel(engine, img_path)
            _block_llm()
            # 先处理状态/胜负，再统一 yield（避免生成器被中断导致不推进）
            if all_correct:
                wname = engine.side_name(side)
                win_text = (
                    f"🏆 {wname} 猜中了对方的诗句！\n"
                    f"{engine.a_name} 的题：{engine.a_puzzle}\n"
                    f"{engine.b_name} 的题：{engine.b_puzzle}"
                )
                self.duel_sessions.pop(sid)
                yield event.image_result(img_path)
                yield event.plain_result(win_text)
            else:
                engine.switch_turn()
                yield event.image_result(img_path)
                yield event.plain_result(f"轮到 {engine.current_name()}。")
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

        # 🎯 猜诗句游戏处理
        if session_id in self.guess_verse_sessions:
            engine = self.guess_verse_sessions[session_id]
            # 提示指令：显示声母韵母状态
            if msg_raw in ("提示", "声韵提示", "拼音提示"):
                hint_path = os.path.join(str(self.plugin_data_dir), f"verse_hint_{session_id}.png")
                render_hint(engine, hint_path)
                yield event.image_result(hint_path)
                return
            ok, err, comp, all_correct = engine.guess(msg_raw)
            if not ok:
                # 不合规的猜测静默忽略，不回复，不占次数
                return
            img_path = os.path.join(str(self.plugin_data_dir), f"verse_{session_id}.png")
            render_grid(engine, img_path, max_attempts=self.verse_max_attempts)
            yield event.image_result(img_path)

            if all_correct:
                ans_path = os.path.join(str(self.plugin_data_dir), f"verse_ans_{session_id}.png")
                render_answer(engine, ans_path)
                yield event.image_result(ans_path)
                yield event.plain_result(f"🎉 猜中了！{engine.target_text}")
                del self.guess_verse_sessions[session_id]
                if os.path.exists(img_path): os.remove(img_path)
            elif engine.is_finished():
                ans_path = os.path.join(str(self.plugin_data_dir), f"verse_ans_{session_id}.png")
                render_answer(engine, ans_path)
                yield event.image_result(ans_path)
                yield event.plain_result(f"机会耗尽！正确诗句：{engine.target_text}")
                del self.guess_verse_sessions[session_id]
                if os.path.exists(img_path): os.remove(img_path)
            else:
                remaining = self.verse_max_attempts - len(engine.history)
                yield event.plain_result(f"继续猜！剩余 {remaining} 次机会")
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
