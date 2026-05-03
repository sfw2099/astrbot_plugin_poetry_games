import os
import asyncio
import aiohttp  # noqa: F401 - used in _install_db at runtime
import json
import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.all import Plain, Image, MessageChain

from .database import PoetryDB
from .game.flowing_petals import FlowingPetalsEngine
from .game.crossword_poetry import PoetryCrosswordEngine
from .game.snake_poetry import PoetrySnakeEngine

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
        if msg_raw.startswith(("(", "（")) and msg_raw.endswith((")", "）")): return
        if not msg_raw or msg_raw.startswith(("/", "查询", "生成战报", "恢复", "结束", "纵横", "衔字", "蛇形", "删除", "安装", "bot")): return

        session_id = str(event.get_group_id() or event.get_session_id())
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
