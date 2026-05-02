import os
import asyncio
import json
import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .database import PoetryDB
from .game.flowing_petals import FlowingPetalsEngine
from .game.crossword_poetry import PoetryCrosswordEngine

GITEE_PARTS = 4  # Gitee 分片数

DEFAULT_PROXY_URLS = [
    "gitee://alin1031/poetry-data/releases/download/v1.0.0/poetry_data.zip",
    "https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/download/data-v3.0.0/poetry_data.zip",
    "https://gh.ddlc.top/https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/download/data-v3.0.0/poetry_data.zip",
    "https://ghproxy.net/https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/download/data-v3.0.0/poetry_data.zip",
]


@register("astrbot_plugin_poetry_games", "ALin", "诗词游戏引擎", "3.5.0")
class PoetryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_games")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.plugin_data_dir / 'poetry_data.db'
        self.saves_dir = self.plugin_data_dir / 'saves'
        self.saves_dir.mkdir(parents=True, exist_ok=True)

        self.db = None
        self.active_games = {}

        asyncio.create_task(self.prepare_database())

    async def _probe_url(self, session, url, timeout=8):
        """探测单个 URL 的响应速度。gitee:// 协议返回固定高分"""
        if url.startswith("gitee://"):
            return (url, 0.01, 365 * 1024 * 1024)  # Gitee 优先
        try:
            t0 = time.monotonic()
            async with session.head(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                     allow_redirects=True) as resp:
                if resp.status == 200:
                    elapsed = time.monotonic() - t0
                    clen = int(resp.headers.get('Content-Length', 0))
                    return (url, elapsed, clen)
        except Exception:
            pass
        return None

    async def _probe_all(self):
        """探测所有代理 URL，返回按延迟排序的有效列表"""
        import aiohttp
        config_urls = self.config.get("proxy_urls", [])
        if isinstance(config_urls, str):
            config_urls = [u.strip() for u in config_urls.split("\n") if u.strip()]
        urls = config_urls if config_urls else DEFAULT_PROXY_URLS

        logger.info(f"🔍 正在探测 {len(urls)} 个下载源...")
        async with aiohttp.ClientSession() as session:
            tasks = [self._probe_url(session, u) for u in urls]
            results = await asyncio.gather(*tasks)

        valid = [r for r in results if r is not None and r[2] > 1024 * 1024]
        valid.sort(key=lambda r: r[1])
        for i, (url, elapsed, clen) in enumerate(valid):
            mb = clen / (1024 * 1024)
            logger.info(f"  {i+1}. {elapsed:.1f}s  {mb:.0f}MB  {url[:80]}")
        if not valid:
            logger.warning("  ⚠️ 所有源均不可达")
        return valid

    async def _download_gitee_parts(self, gitee_base):
        """下载 Gitee 分片文件并合并解压"""
        import aiohttp
        import zipfile
        import io
        all_data = bytearray()
        async with aiohttp.ClientSession() as session:
            for i in range(1, GITEE_PARTS + 1):
                part_url = f"{gitee_base}.part{i:02d}"
                logger.info(f"  [{i}/{GITEE_PARTS}] {part_url}")
                async with session.get(part_url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                    if resp.status != 200:
                        raise Exception(f"Part {i} HTTP {resp.status}")
                    chunk = await resp.read()
                    all_data.extend(chunk)
        logger.info("📦 Reassembling & extracting...")
        with zipfile.ZipFile(io.BytesIO(bytes(all_data))) as zf:
            zf.extractall(str(self.plugin_data_dir))

    async def _download_and_extract(self, url):
        """下载 zip 并解压到数据目录"""
        import aiohttp
        import zipfile
        import io

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                total_size = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                chunks = []
                last_log = 0
                async for chunk in resp.content.iter_chunked(262144):
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = int(downloaded / total_size * 100)
                        if pct >= last_log + 10:
                            logger.info(f"  ⏳ {pct}% ({downloaded/(1024*1024):.0f}MB / {total_size/(1024*1024):.0f}MB)")
                            last_log = pct
                if total_size > 0 and downloaded < total_size * 0.9:
                    raise Exception(f"下载不完整 ({downloaded}/{total_size} bytes)")

        data = b''.join(chunks)
        logger.info("📦 正在解压...")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(str(self.plugin_data_dir))

    async def prepare_database(self):
        """检查数据库：已有 .db 直接加载，否则下载"""
        if self.db_file.exists() and self.db_file.stat().st_size > 0:
            try:
                self.db = PoetryDB(str(self.db_file))
                logger.info("✅ 数据库已就绪。")
                return
            except Exception:
                logger.warning("⚠️ 检测到损坏的数据库文件，准备重新下载...")
                os.remove(str(self.db_file))

        # 探测所有代理，选最快的下载
        candidates = await self._probe_all()
        if not candidates:
            logger.error("❌ 无法连接到任何下载源，请检查网络或在 WebUI 配置 proxy_urls")
            logger.info(f"💡 也可手动将 poetry_data.db 放到: {self.plugin_data_dir}")
            return

        best_url, elapsed, _ = candidates[0]
        logger.info(f"⬇️ 选用最快源 ({elapsed:.1f}s)，开始下载...")
        for url, _, _ in candidates:
            try:
                if url.startswith("gitee://"):
                    gitee_url = "https://gitee.com/" + url[len("gitee://"):]
                    await self._download_gitee_parts(gitee_url)
                else:
                    await self._download_and_extract(url)
                self.db = PoetryDB(str(self.db_file))
                db_size_mb = os.path.getsize(str(self.db_file)) / (1024 * 1024)
                logger.info(f"✅ 数据库就绪 ({db_size_mb:.0f} MB)")
                return
            except Exception as e:
                logger.warning(f"  ⚠️ 下载失败: {e}，尝试下一源...")
                continue

        logger.error("❌ 所有源下载失败，请手动上传 poetry_data.db")
        logger.info(f"💡 目标路径: {self.plugin_data_dir}")

    # ==========================================
    # 基础信息检索
    # ==========================================
    @filter.command("查询诗句")
    async def find_sentence(self, event: AstrMessageEvent, sentence: str):
        if not self.db:
            yield event.plain_result("数据库正在加载中，请稍后再试...")
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
            yield event.plain_result("数据库正在加载中，请稍后再试...")
            return
        results = self.db.get_poem_by_title(title_kw)
        if not results:
            yield event.plain_result(f"未找到标题包含「{title_kw}」的诗词。")
            return
        resp = ["检索结果：\n" + "=" * 20]
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
        yield event.plain_result("🌸 【衔字飞花令】已建立！\n请群友发送【加入】参与排队。")

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
        yield event.plain_result("🌟 【纵横飞花令】已建立！\n请群友发送【加入】参与排队。")
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
            yield event.plain_result("当前没有进行中的游戏。")
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
