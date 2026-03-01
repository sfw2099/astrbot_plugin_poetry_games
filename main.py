from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools  # <-- 引入 StarTools
from astrbot.api import logger, AstrBotConfig
import os
import shutil  

from .database import PoetryDB
from .game.flowing_petals import FlowingPetalsGame

@register("astrbot_plugin_poetry_games", "ALin", "诗词游戏", "2.1.0")
class PoetryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 1. 获取当前代码所在目录（初始数据库存放处）
        curr_dir = os.path.dirname(__file__)
        original_db_path = os.path.join(curr_dir, 'poetry_data.db')
        
        # 2. 获取 AstrBot 官方标准的持久化数据目录
        plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_games")
        plugin_data_dir.mkdir(parents=True, exist_ok=True) # 确保目录存在
        
        self.db_file = str(plugin_data_dir / 'poetry_data.db')
        
        # 3. 如果标准目录下没有数据库（如首次安装），则从代码目录拷贝过去
        if not os.path.exists(self.db_file):
            if os.path.exists(original_db_path):
                shutil.copyfile(original_db_path, self.db_file)
                logger.info(f" 已将初始数据库复制到持久化目录: {self.db_file}")
            else:
                logger.error(" 严重错误：未找到初始的 poetry_data.db 文件！")
        
        self.db = PoetryDB(self.db_file)
        self.config = config
        
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
