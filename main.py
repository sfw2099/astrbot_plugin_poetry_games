import os
import asyncio
import json

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .database import PoetryDB
from .db_builder import build_database
from .game.flowing_petals import FlowingPetalsEngine
from .game.crossword_poetry import PoetryCrosswordEngine

@register("astrbot_plugin_poetry_games", "ALin", "诗词游戏引擎", "3.5.0")
class PoetryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 插件目录
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 标准数据目录
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_games")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.plugin_data_dir / 'poetry_data.db'

        # 存档目录
        self.saves_dir = self.plugin_data_dir / 'saves'
        self.saves_dir.mkdir(parents=True, exist_ok=True)

        # JSON 数据源目录（插件内 / 外部）
        self.data_dir = os.path.join(self.plugin_dir, "data")

        self.db = None
        self.active_games = {}

        asyncio.create_task(self.prepare_database())

        # 预构建数据库下载地址
        self.db_release_url = "https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/download/data-v3.0.0/poetry_data.zip"

    async def prepare_database(self):
        """检查并准备数据库：已有 .db > 从 JSON 构建 > 下载预构建 .db"""
        if self.db_file.exists() and self.db_file.stat().st_size > 0:
            try:
                self.db = PoetryDB(str(self.db_file))
                logger.info("✅ 数据库已就绪。")
                return
            except Exception:
                logger.warning("⚠️ 检测到损坏的数据库文件，准备重新获取...")
                os.remove(str(self.db_file))

        # 优先：从本地 JSON 构建
        data_path = self.data_dir
        if os.path.isdir(data_path) and any(f.endswith('.json') for f in os.listdir(data_path)):
            json_files = [f for f in os.listdir(data_path) if f.endswith('.json')]
            logger.info(f"📊 检测到 {len(json_files)} 个数据文件，开始构建数据库 (约需 2-5 分钟)...")
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, build_database, data_path, str(self.db_file))
                self.db = PoetryDB(str(self.db_file))
                db_size_mb = os.path.getsize(str(self.db_file)) / (1024 * 1024)
                logger.info(f"✅ 数据库构建成功 ({db_size_mb:.0f} MB)")
                return
            except Exception as e:
                logger.error(f"❌ 数据库构建失败: {e}")

        # 备选：下载预构建的 poetry_data.zip
        logger.info(f"📡 未检测到本地数据，正在下载预构建数据库 (约 366MB)...")
        logger.info(f"  (可通过安装 data-v3.0.0 Release 中的 JSON 到 data/ 目录跳过此步骤)")
        try:
            import aiohttp
            import zipfile
            import io

            async with aiohttp.ClientSession() as session:
                async with session.get(self.db_release_url, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ 下载失败，状态码: {resp.status}")
                        return
                    total_size = int(resp.headers.get('Content-Length', 0))
                    downloaded = 0
                    chunks = []
                    async for chunk in resp.content.iter_chunked(262144):
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        if total_size > 0 and downloaded % (20 * 1024 * 1024) < 262144:
                            pct = int(downloaded / total_size * 100)
                            mb = downloaded / (1024 * 1024)
                            logger.info(f"  ⏳ {pct}% ({mb:.0f}MB / {total_size/(1024*1024):.0f}MB)")

            data = b''.join(chunks)
            logger.info("📦 正在解压...")
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(str(self.plugin_data_dir))
            self.db = PoetryDB(str(self.db_file))
            db_size_mb = os.path.getsize(str(self.db_file)) / (1024 * 1024)
            logger.info(f"✅ 数据库下载并解压完成 ({db_size_mb:.0f} MB)")
        except Exception as e:
            logger.error(f"❌ 数据库下载失败: {e}")
            logger.info("手动下载: 将 poetry_data.zip 中的 poetry_data.db 放入插件数据目录")

    # ==========================================
    # 基础信息检索 (通用指令)
    # ==========================================
    @filter.command("查询诗句")
    async def find_sentence(self, event: AstrMessageEvent, sentence: str):
        if not self.db:
            yield event.plain_result("数据库正在构建中，请稍后再试...")
            return
        results = self.db.search_by_sentence(sentence)
        if not results:
            yield event.plain_result(f"未找到包含「{sentence}」的内容。")
            return
        resp = ["查询结果："]
        for title, author, dynasty in results:
            resp.append(f"• [{dynasty}] {author} —— 《{title}》")
        yield event.plain_result("\n".join(resp))

    @filter.command("查询诗词")
    async def find_full_poem(self, event: AstrMessageEvent, title_kw: str):
        if not self.db:
            yield event.plain_result("数据库正在构建中，请稍后再试...")
            return
        results = self.db.get_poem_by_title(title_kw)
        if not results:
            yield event.plain_result(f"未找到标题包含「{title_kw}」的诗词。")
            return
        resp = ["检索结果：\n" + "="*20]
        for i, (title, author, dynasty, content, version) in enumerate(results):
            clean_content = content.replace('\r\n', '\n').strip()
            ver_label = f" [版本{version}]" if version > 0 else ""
            resp.append(f"《{title}》{ver_label}\n作者：[{dynasty}] {author}\n\n{clean_content}")
            if i < len(results) - 1:
                resp.append("-" * 15)
        yield event.plain_result("\n".join(resp))

    # ==========================================
    # 游戏建局指令
    # ==========================================
    @filter.command("衔字飞花令")
    async def start_flowing(self, event: AstrMessageEvent):
        if not self.db:
            yield event.plain_result("⏳ 数据库加载中，请稍后再试...")
            return

        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！")
            return

        engine = FlowingPetalsEngine(session_id, self.db, str(self.saves_dir))
        self.active_games[session_id] = engine
        yield event.plain_result("🌸 【衔字飞花令】已建立！\n请群友发送【加入】参与排队，随时可加！第一位加入的玩家即可开始。")

    @filter.command("纵横飞花令")
    async def start_crossword(self, event: AstrMessageEvent):
        if not self.db:
            yield event.plain_result("⏳ 数据库加载中，请稍后再试...")
            return

        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！")
            return

        engine = PoetryCrosswordEngine(session_id, self.db, str(self.saves_dir))
        self.active_games[session_id] = engine
        yield event.plain_result("🌟 【纵横飞花令】已建立！\n请群友发送【加入】参与排队。第一名加入的玩家即可开始接龙！")

        res = engine.step("ignore", "", "")
        if res and "image" in res:
            yield event.image_result(res["image"])

    # ==========================================
    # 游戏管理指令
    # ==========================================
    @filter.command("恢复游戏")
    async def load_game(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前已有进行中的游戏，请先【结束游戏】。")
            return

        save_file = os.path.join(str(self.saves_dir), f"game_{session_id}.json")
        if not os.path.exists(save_file):
            yield event.plain_result("未找到该群的存档文件。")
            return

        try:
            with open(save_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if "Crossword" in data.get("game_type", ""):
                engine = PoetryCrosswordEngine(session_id, self.db, str(self.saves_dir))
            else:
                engine = FlowingPetalsEngine(session_id, self.db, str(self.saves_dir))

            if engine.load_state():
                self.active_games[session_id] = engine
                yield event.plain_result("💾 进度恢复成功！游戏继续。")
            else:
                yield event.plain_result("❌ 存档文件读取失败。")
        except Exception as e:
            yield event.plain_result(f"❌ 恢复失败: {e}")

    @filter.command("生成战报")
    async def generate_report(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if not engine:
            yield event.plain_result("当前没有进行中的游戏。如果要生成旧战报，请先【恢复游戏】。")
            return
        yield event.plain_result(engine.generate_text_report())

    @filter.command("结束游戏")
    async def stop_game(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            engine = self.active_games.pop(session_id)
            yield event.plain_result("⏹️ 游戏已结束。最后战果：\n" + engine.generate_text_report())
        else:
            yield event.plain_result("当前没有正在进行的游戏。")

    # ==========================================
    # 全局监听分发中枢
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_recv_msg(self, event: AstrMessageEvent):
        msg_raw = event.message_str.strip()
        if msg_raw.startswith(("(", "（")) and msg_raw.endswith((")", "）")): return
        if not msg_raw or msg_raw.startswith(("/", "查询", "生成战报", "恢复", "结束", "纵横", "衔字")): return

        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id not in self.active_games: return

        engine = self.active_games[session_id]
        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()

        if msg_raw in ["加入", "+加入", "1+加入", "1 + 加入"]:
            response = engine.step("join", user_id, user_name)
        else:
            response = engine.step("play", user_id, user_name, msg_raw)

        if not response: return

        if response.get("status") == "ignore": return
        if response.get("msg"): yield event.plain_result(response["msg"])
        if "image" in response: yield event.image_result(response["image"])
