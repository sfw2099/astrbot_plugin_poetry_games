# -*- coding: utf-8 -*-
"""AI bot：让机器人通过 Function Calling 参与诗词对垒/猜诗句。

架构：
- 读工具（get_verse_state / get_duel_state / search_verses / get_pinyin_hint）
  + 写工具 submit_guess（提交猜测）通过 FunctionTool 暴露给 LLM。
- 猜测由 submit_guess 工具执行（结构化调用，不依赖自由文本格式）。
- 方案 C 降级：不支持 FC 时，用 llm_generate + 候选列表 + 解析「猜：诗句」。
"""

import random

from .base_game import BOT_ID, BOT_NAME
from .guess_verse import extract_hanzi
from .search import search_verses_candidates, parse_search_query
from .bot_utils import (
    build_verse_state_text, build_duel_state_text, parse_guess_from_speech,
)

try:
    from pydantic import Field
    from pydantic.dataclasses import dataclass
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.api.all import Plain, Image, MessageChain
    _ASTRBOT_OK = True
except Exception:
    _ASTRBOT_OK = False
    dataclass = None
    ContextWrapper = None
    FunctionTool = None
    ToolExecResult = None
    ToolSet = None
    AstrAgentContext = None
    Field = None
    Plain = None
    Image = None
    MessageChain = None


SYSTEM_PROMPT = """你是诗词游戏玩家「{bot_name}」。你在和群友玩诗词游戏（猜诗句 / 诗词对垒）。

你的任务：通过工具获取局势、检索诗句，最终用 submit_guess 工具提交猜测。

可用工具：
- get_verse_state：查看「猜诗句」当前局势与已猜反馈
- get_duel_state：查看「诗词对垒」当前局势与双方反馈
- search_verses：按自然语言检索候选诗句
- get_pinyin_hint：查看拼音模式下的声母韵母提示
- submit_guess：提交你的猜测（必须调用！）

检索策略（search_verses 用自然语言描述，如「第3个字是烟、第5个字声母j的7字诗句」）：
- 绿字（字和位置都对）→ 用「第N个字是X」检索
- 橙字（字对但位置错）→ 用「包含X」检索
- 声母/韵母对 → 用「第N个字声母X」「第N个字韵母X」检索
- 不确定时放宽条件，不要一次叠加太多约束

重要：
- 你本身就懂大量诗词，可凭反馈直接猜，不必依赖检索。
- 最多检索 2 次，然后必须调用 submit_guess 提交一个合理猜测（即使不确定，猜错会得到反馈）。
- 不要用文本格式「猜：XXX」，一律通过 submit_guess 工具提交。
- 最终可输出一句简短自然发言（≤30字），不要提及工具或原始数据。
"""


# ============ FunctionTool 定义（依赖 astrbot） ============

if _ASTRBOT_OK:

    @dataclass
    class GetVerseStateTool(FunctionTool[AstrAgentContext]):
        name: str = "get_verse_state"
        description: str = "获取「猜诗句」当前局势：答案格式、已猜次数、每次猜测的逐格反馈。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话ID，可留空"}},
        })
        bot: object = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.bot.get_verse_state(kwargs.get("session_id", ""))

    @dataclass
    class GetDuelStateTool(FunctionTool[AstrAgentContext]):
        name: str = "get_duel_state"
        description: str = "获取「诗词对垒」当前局势：我方目标、双方历史与逐格反馈、当前轮到谁。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话ID，可留空"}},
        })
        bot: object = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.bot.get_duel_state(kwargs.get("session_id", ""))

    @dataclass
    class SearchVersesTool(FunctionTool[AstrAgentContext]):
        name: str = "search_verses"
        description: str = "按自然语言描述检索候选诗句。例如「第3个字是烟、第5个字声母j的7字诗句」「包含明月、不含风雨、5字」。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言检索条件，如：第3个字是烟 / 第5个字声母j / 包含明月 / 不含风雨 / 7字"},
            },
            "required": ["query"],
        })
        bot: object = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.bot.search_verses(kwargs.get("query", ""))

    @dataclass
    class GetPinyinHintTool(FunctionTool[AstrAgentContext]):
        name: str = "get_pinyin_hint"
        description: str = "获取拼音模式下的声母韵母提示。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话ID，可留空"}},
        })
        bot: object = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.bot.get_pinyin_hint(kwargs.get("session_id", ""))

    @dataclass
    class SubmitGuessTool(FunctionTool[AstrAgentContext]):
        name: str = "submit_guess"
        description: str = "提交你的诗句猜测。text 为猜测的诗句（纯汉字，两句需带标点）。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要猜测的诗句"},
                "session_id": {"type": "string", "description": "会话ID，可留空"},
            },
            "required": ["text"],
        })
        bot: object = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            event = getattr(context.context, "event", None)
            origin = getattr(event, "unified_msg_origin", None) if event is not None else None
            return await self.bot.submit_guess(
                kwargs.get("session_id", ""), kwargs.get("text", ""), origin,
            )


class BotPlayer:
    """AI bot 玩家：决策入口，封装 tool_loop_agent 调用与降级 fallback。"""

    def __init__(self, plugin, enabled=False, bot_name=None, cooldown=10, puzzle_ai_ratio=50):
        self.plugin = plugin
        self.enabled = enabled
        self.bot_name = bot_name or BOT_NAME
        self.cooldown = cooldown
        self.puzzle_ai_ratio = puzzle_ai_ratio
        self._origin = None
        self._common_chars_cache = None

    # ============ 工具实现（供 FunctionTool.call 委托） ============

    def _pick_session(self, sessions, session_id):
        if session_id:
            return session_id
        if sessions:
            return next(iter(sessions))
        return None

    def get_verse_state(self, session_id=""):
        sessions = getattr(self.plugin, "guess_verse_sessions", {})
        sid = self._pick_session(sessions, session_id)
        if not sid:
            return "当前没有进行中的猜诗句游戏。"
        engine = sessions.get(sid)
        if not engine:
            return "找不到该猜诗句会话。"
        return build_verse_state_text(engine, sid)

    def get_duel_state(self, session_id=""):
        sessions = getattr(self.plugin, "duel_sessions", {})
        sid = self._pick_session(sessions, session_id)
        if not sid:
            return "当前没有进行中的诗词对垒。"
        duel = sessions.get(sid)
        if not duel or not duel.get("engine"):
            return "找不到该对垒会话。"
        engine = duel["engine"]
        side = "a" if str(engine.a_id) == BOT_ID else "b"
        return build_duel_state_text(engine, side)

    def search_verses(self, query=""):
        if not getattr(self.plugin, "db", None):
            return "数据库未安装。"
        cond = parse_search_query(query)
        cands = search_verses_candidates(
            self.plugin.db,
            include=cond["include"], exclude=cond["exclude"], length=cond["length"],
            initials=cond["initials"], finals=cond["finals"],
            char_positions=cond["char_positions"],
            initial_positions=cond["initial_positions"],
            final_positions=cond["final_positions"],
            common_chars=self._common_chars(),
            limit=20,
        )
        if not cands:
            return "未找到符合条件的候选诗句，可放宽条件（减少排除字或去掉部分约束）再试。"
        lines = [f"共 {len(cands)} 条候选："]
        for c in cands:
            lines.append(f"{c['text']}（{c['title']}·{c['author']}·{c['dynasty']}）")
        return "\n".join(lines)

    def _common_chars(self):
        """经典曲库中出现过的单字集合（用于无位置约束时优先返回常见字诗句）。"""
        if getattr(self, "_common_chars_cache", None) is None:
            chars = set()
            for p in getattr(self.plugin, "classic_poems", []) or []:
                chars.update(extract_hanzi(p.get("sentence", "") or ""))
            self._common_chars_cache = chars
        return self._common_chars_cache

    def get_pinyin_hint(self, session_id=""):
        sessions = getattr(self.plugin, "guess_verse_sessions", {})
        sid = self._pick_session(sessions, session_id)
        if not sid:
            return "当前没有猜诗句会话。"
        engine = sessions.get(sid)
        if not engine:
            return "找不到该会话。"
        if getattr(engine, "hint_mode", "pinyin") != "pinyin":
            return "当前为部首模式，无拼音提示。"
        init_status = getattr(engine, "initial_status", {}) or {}
        final_status = getattr(engine, "final_status", {}) or {}
        ok_i = sorted([k for k, v in init_status.items() if v == "correct"])
        ok_f = sorted([k for k, v in final_status.items() if v == "correct"])
        return f"已确认声母：{ok_i or '无'}\n已确认韵母：{ok_f or '无'}"

    async def _send_result(self, origin, msgs):
        if not origin or _ASTRBOT_OK is False:
            return
        for kind, payload in msgs:
            try:
                if kind == "text":
                    await self.plugin.context.send_message(origin, MessageChain([Plain(payload)]))
                else:
                    await self.plugin.context.send_message(origin, MessageChain([Image.fromFileSystem(payload)]))
            except Exception:
                pass

    async def submit_guess(self, session_id, text, origin=None):
        """提交猜测（猜诗句或对垒）。执行猜测 + 发图到群，返回反馈文本给 LLM。"""
        plugin = self.plugin
        if not origin:
            origin = self._origin
        hanzi = extract_hanzi(text)
        if not hanzi:
            return "猜测内容无效，请提供诗句。"
        # 猜诗句
        vs = getattr(plugin, "guess_verse_sessions", {})
        sid = self._pick_session(vs, session_id)
        if sid and sid in vs:
            engine = vs[sid]
            result = plugin._apply_verse_guess(engine, sid, BOT_ID, self.bot_name, text)
            if not result["ok"]:
                return f"猜测失败：{result['err']}"
            await self._send_result(origin, result["msgs"])
            if result["all_correct"]:
                return "已猜中！游戏结束。"
            return "已提交猜测。当前反馈：\n" + build_verse_state_text(engine, sid)
        # 对垒
        ds = getattr(plugin, "duel_sessions", {})
        sid = self._pick_session(ds, session_id)
        if sid and sid in ds:
            duel = ds[sid]
            engine = duel.get("engine")
            if not engine:
                return "对垒尚未开始。"
            if not engine.is_turn(BOT_ID):
                return "本轮已提交过猜测（轮到对方），请结束本轮。"
            result = plugin._apply_duel_guess(duel, sid, BOT_ID, self.bot_name, text)
            if not result["ok"]:
                return f"猜测失败：{result['err']}"
            await self._send_result(origin, result["msgs"])
            if result["all_correct"]:
                return "已猜中！对垒结束。"
            side = "a" if str(engine.a_id) == BOT_ID else "b"
            return "已提交猜测。当前反馈：\n" + build_duel_state_text(engine, side)
        return "找不到对应的游戏会话。"

    # ============ LLM 调用 ============

    async def _provider_id(self, event=None):
        try:
            if event is not None and hasattr(event, "unified_msg_origin"):
                return await self.plugin.context.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception:
            pass
        try:
            return await self.plugin.context.get_current_chat_provider_id(None)
        except Exception:
            return None

    def _build_tools(self, include_verse, include_duel):
        if not _ASTRBOT_OK:
            return []
        tools = []
        if include_verse:
            t = GetVerseStateTool()
            t.bot = self
            tools.append(t)
        if include_duel:
            t = GetDuelStateTool()
            t.bot = self
            tools.append(t)
        s = SearchVersesTool()
        s.bot = self
        tools.append(s)
        p = GetPinyinHintTool()
        p.bot = self
        tools.append(p)
        g = SubmitGuessTool()
        g.bot = self
        tools.append(g)
        return tools

    async def _run_agent(self, event, tools, prompt):
        prov_id = await self._provider_id(event)
        if not prov_id:
            return None
        try:
            resp = await self.plugin.context.tool_loop_agent(
                event=event,
                chat_provider_id=prov_id,
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT.format(bot_name=self.bot_name),
                tools=ToolSet(tools),
                max_steps=6,
                tool_call_timeout=30,
            )
            return resp.completion_text
        except Exception:
            return None

    async def _run_fallback(self, event, candidates_text, prompt):
        prov_id = await self._provider_id(event)
        if not prov_id:
            return None
        full_prompt = prompt + "\n\n候选诗句（供参考）：\n" + candidates_text
        try:
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=prov_id,
                prompt=full_prompt,
                system_prompt=SYSTEM_PROMPT.format(bot_name=self.bot_name),
            )
            return resp.completion_text
        except Exception:
            return None

    # ============ 决策入口 ============

    async def think_and_guess_verse(self, engine, session_id, event=None):
        """猜诗句：返回 (guess_text, speech)。guess_text 为降级路径解析的猜测（方案 C）；
        方案 A 下猜测已由 submit_guess 工具提交，guess_text 为 None。"""
        state_text = build_verse_state_text(engine, session_id)
        prompt = f"当前猜诗句局势：\n{state_text}\n\n请推理并用 submit_guess 工具提交你的猜测。"
        tools = self._build_tools(include_verse=True, include_duel=False)
        speech = await self._run_agent(event, tools, prompt) if tools else None
        if not speech:
            cands = search_verses_candidates(self.plugin.db, length=len(engine.target_hanzi),
                                             common_chars=self._common_chars(), limit=20)
            cand_text = "\n".join(c["text"] for c in cands)
            speech = await self._run_fallback(event, cand_text, prompt)
        if not speech:
            return None, "（我想不出来了）"
        return parse_guess_from_speech(speech)

    async def think_and_guess_duel(self, duel, engine, event=None):
        """对垒：返回 (guess_text, speech)。方案 A 下猜测已由 submit_guess 工具提交。"""
        side = "a" if str(engine.a_id) == BOT_ID else "b"
        state_text = build_duel_state_text(engine, side)
        prompt = f"当前对垒局势：\n{state_text}\n\n请推理并用 submit_guess 工具提交你的猜测。"
        tools = self._build_tools(include_verse=False, include_duel=True)
        speech = await self._run_agent(event, tools, prompt) if tools else None
        if not speech:
            my_target = engine.a_target_hanzi if side == "a" else engine.b_target_hanzi
            cands = search_verses_candidates(self.plugin.db, length=len(my_target),
                                             common_chars=self._common_chars(), limit=20)
            cand_text = "\n".join(c["text"] for c in cands)
            speech = await self._run_fallback(event, cand_text, prompt)
        if not speech:
            return None, "（我想不出来了）"
        return parse_guess_from_speech(speech)

    async def think_puzzle(self, fmt, event=None):
        """bot 出题。一半概率总库随机，一半概率 AI 出题（不调工具）。"""
        use_ai = random.random() * 100 < self.puzzle_ai_ratio
        if not use_ai:
            try:
                if fmt[0] == "single":
                    cands = self.plugin.db.get_random_verse(fmt[1], fmt[1], target_count=10)
                else:
                    cands = self.plugin.db.get_random_verse_by_combo(fmt[1][0], fmt[1][1], target_count=10)
                if cands:
                    return random.choice(cands)[0]
            except Exception:
                pass
            return None
        # AI 出题
        prov_id = await self._provider_id(event)
        if not prov_id:
            return None
        if fmt[0] == "single":
            fmt_desc = f"{fmt[1]} 字单句"
        else:
            fmt_desc = f"{fmt[1][0]} 字 + {fmt[1][1]} 字（两句，带标点）"
        prompt = (f"请背出一句中国古典诗词作为题目。格式要求：{fmt_desc}。"
                  f"只输出诗句本身，不要任何其他文字或解释。")
        try:
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=prov_id, prompt=prompt,
                system_prompt="你是古典诗词高手，能准确背诵并引用古典诗词。",
            )
            txt = (resp.completion_text or "").strip()
            for line in txt.splitlines():
                hanzi = extract_hanzi(line)
                if hanzi:
                    return line.strip()
            return None
        except Exception:
            return None
