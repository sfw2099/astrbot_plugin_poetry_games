import os
import asyncio
import aiohttp
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .database import PoetryDB
from .game.flowing_petals import FlowingPetalsGame

@register("astrbot_plugin_poetry_games", "ALin", "诗词游戏", "2.1.1")
class PoetryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 获取标准数据目录
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_games")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.plugin_data_dir / 'poetry_data.db'
        
        # 直接下载 .db 文件的链接
        self.download_url = "https://github.com/sfw2099/astrbot_plugin_poetry_games/releases/latest/download/poetry_data.db"
        
        self.db = None
        self.active_games = {}
        
        # 异步启动准备任务
        asyncio.create_task(self.prepare_database())

    async def prepare_database(self):
        """检查并准备数据库：直接下载 .db 文件"""
        if self.db_file.exists():
            self.db = PoetryDB(str(self.db_file))
            return

        logger.info(f" 未发现数据库，准备下载: {self.db_file}")
        
        try:
            # 异步流式下载
            async with aiohttp.ClientSession() as session:
                async with session.get(self.download_url) as resp:
                    if resp.status != 200:
                        logger.error(f" 下载失败，状态码: {resp.status}。请检查 Release 中是否存在 poetry_data.db")
                        return
                    
                    # 直接写入 .db 文件
                    with open(self.db_file, "wb") as f:
                        while True:
                            chunk = await resp.content.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
            
            # 实例化数据库对象
            self.db = PoetryDB(str(self.db_file))
            logger.info(" 数据库直接下载并加载成功！")
            
        except Exception as e:
            logger.error(f" 自动下载数据库失败: {e}")
        
        # 游戏会话池：存储所有群组正在进行的游戏引擎实例
        self.active_games = {} 

    def _on_game_ended(self, session_id):
        """回调函数：当游戏结束时清空会话"""
        if session_id in self.active_games:
            del self.active_games[session_id]

    # ==========================================
    # 基础信息检索 (通用指令)
    # ==========================================
    @filter.command("查询诗句")
    async def find_sentence(self, event: AstrMessageEvent, sentence: str):
        if not self.db: 
            yield event.plain_result(" 数据库正在从 GitHub 赶来（首次运行下载中），请稍后再试...")
            return
        results = self.db.search_by_sentence(sentence)
        if not results:
            yield event.plain_result(f" 未找到包含“{sentence}”的内容。")
            return
        resp = [" 查询结果："]
        for title, author, dynasty in results:
            resp.append(f"• [{dynasty}] {author} —— 《{title}》")
        yield event.plain_result("\n".join(resp))

    @filter.command("查询诗词")
    async def find_full_poem(self, event: AstrMessageEvent, title_kw: str):
        if not self.db: 
            yield event.plain_result(" 数据库正在从 GitHub 赶来（首次运行下载中），请稍后再试...")
            return
        results = self.db.get_poem_by_title(title_kw)
        if not results:
            yield event.plain_result(f" 未找到标题包含“{title_kw}”的诗词。")
            return
        resp = [" 检索结果：\n" + "="*20]
        for i, (title, author, dynasty, content) in enumerate(results):
            clean_content = content.replace('\r\n', '\n').strip()
            resp.append(f"《{title}》\n作者：[{dynasty}] {author}\n\n{clean_content}")
            if i < len(results) - 1: resp.append("-" * 15)
        yield event.plain_result("\n".join(resp))

    # ==========================================
    # 游戏生命周期控制 (飞花令专属)
    # ==========================================
    @filter.command("飞花令")
    async def start_flower(self, event: AstrMessageEvent):
        if not self.db: 
            yield event.plain_result(" 数据库正在从 GitHub 赶来（首次运行下载中），请稍后再试...")
            return
        session_id = event.get_group_id() or event.get_session_id()
        if session_id in self.active_games:
            yield event.plain_result(" 当前群聊已有游戏正在进行中。")
            return
        
        # 实例化新的游戏引擎并注册到池中
        game = FlowingPetalsGame(session_id, self.db, self.config, self._on_game_ended)
        self.active_games[session_id] = game
        
        rules = (
            f" 【衔字飞花令】开启！\n"
            f"1. 报名：发送【序号+加入】参与。\n"
            f"2. 衔接：第3句起需包含前2句各至少一字。\n"
            f"3. 计分：上一轮拿分的字本轮进入“冷却”，不计分！\n"
            f"4. 限时：{game.timeout_seconds}s。"
        )
        yield event.plain_result(rules)
        game.start_timer(event)

    @filter.command("查询比赛情况")
    async def query_status(self, event: AstrMessageEvent):
        session_id = event.get_group_id() or event.get_session_id()
        if session_id not in self.active_games:
            yield event.plain_result(" 当前未开启游戏。")
            return
        game = self.active_games[session_id]
        if isinstance(game, FlowingPetalsGame):
            yield event.plain_result(game.get_status_str())

    @filter.command("飞花令记录")
    async def query_history(self, event: AstrMessageEvent):
        session_id = event.get_group_id() or event.get_session_id()
        if session_id not in self.active_games:
            yield event.plain_result(" 当前未开启游戏。")
            return
        game = self.active_games[session_id]
        if isinstance(game, FlowingPetalsGame):
            yield event.plain_result(game.get_history_str())

    @filter.command("结束游戏", alias=["结束飞花令"])
    async def end_game(self, event: AstrMessageEvent):
        session_id = event.get_group_id() or event.get_session_id()
        if session_id in self.active_games:
            self.active_games[session_id].stop_game()
            yield event.plain_result(" 游戏已强制结束。")

    # ==========================================
    # 全局监听分发中枢
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_recv_msg(self, event: AstrMessageEvent):
        msg_raw = event.message_str.strip()
        # 忽略聊天括号和斜杠指令
        if msg_raw.startswith(("(", "（")) and msg_raw.endswith((")", "）")): return
        if not msg_raw or msg_raw.startswith(("/", "查询", "飞花令", "结束")): return

        session_id = event.get_group_id() or event.get_session_id()
        if session_id in self.active_games:
            # 取出当前群组对应的游戏引擎，将信息直接委托给引擎处理
            game = self.active_games[session_id]
            user_id = event.get_sender_id()
            user_name = event.get_sender_name()
            
            async for response in game.process_msg(event, msg_raw, user_id, user_name):
                yield response
