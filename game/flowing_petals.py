import re
import asyncio
from astrbot.api.event import AstrMessageEvent
from .base_game import BaseGame

class FlowingPetalsGame(BaseGame):
    def __init__(self, session_id, db, config, on_game_end):
        super().__init__(session_id, db, config, on_game_end)
        self.players = []
        self.history = []
        self.used_verses = []
        self.banned_score_chars = set()
        self.current_turn = 0
        self.turn_counter = 0
        self.timeout_seconds = self.config.get("timeout_seconds", 90)
        self.latest_event = None

    def get_status_str(self):
        resp = [" 当前局势情况："]
        for i, p in enumerate(self.players, 1):
            tag = " 👈 当前轮次" if (i-1) == self.current_turn else ""
            resp.append(f"{i}. 【{p['name']}】积分：{p['score']}{tag}")
        
        if self.banned_score_chars:
            banned = "、".join(self.banned_score_chars)
            resp.append(f"\n 计分冷却字：{banned} (本轮使用这些字不计分)")
        return "\n".join(resp)

    def get_history_str(self):
        if not self.used_verses:
            return " 暂无接龙记录。"
        resp = [" 本局接龙记录："]
        for i, (t, a, c) in enumerate(self.used_verses, 1):
            resp.append(f"{i}. {c} ({a}·《{t}》)")
        return "\n".join(resp)

    def start_timer(self, event: AstrMessageEvent):
        self.latest_event = event
        if self.timer_task:
            self.timer_task.cancel()
        self.turn_counter += 1
        current_counter = self.turn_counter
        
        async def timer_task():
            try:
                await asyncio.sleep(self.timeout_seconds)
                if self.turn_counter == current_counter:
                    await self._handle_timeout()
            except asyncio.CancelledError:
                pass 
        self.timer_task = asyncio.create_task(timer_task())

    async def _handle_timeout(self):
        if not self.latest_event: return
        event = self.latest_event
        
        if not self.players:
            self.stop_game()
            await event.send(event.plain_result(f" {self.timeout_seconds}秒内无玩家加入，飞花令已自动结束。"))
            return
            
        current_player = self.players[self.current_turn]['name']
        if len(self.players) > 1:
            self.current_turn = (self.current_turn + 1) % len(self.players)
            next_player = self.players[self.current_turn]['name']
            timeout_msg = f" 玩家【{current_player}】超时。跳过本轮。\n👉 当前轮到：【{next_player}】\n\n" + self.get_status_str()
            await event.send(event.plain_result(timeout_msg))
            self.start_timer(event)
        else:
            self.stop_game()
            await event.send(event.plain_result(f" 玩家【{current_player}】超时。\n 飞花令自动结束。"))

    async def process_msg(self, event: AstrMessageEvent, msg_raw: str, user_id: str, user_name: str):
        # 1. 加入逻辑
        join_match = re.match(r'^(\d+)\s*[+＋]\s*加入$', msg_raw)
        if join_match:
            idx = int(join_match.group(1)) - 1
            if idx == len(self.players):
                if user_id in [p['id'] for p in self.players]:
                    yield event.plain_result(" 您已在名单中。")
                    return
                self.players.append({'id': user_id, 'name': user_name, 'score': 0})
                yield event.plain_result(f" 【{user_name}】加入成功！\n" + self.get_status_str())
                self.start_timer(event)
                event.stop_event()
                return
            return

        # 2. 接龙权限校验
        if not self.players: return
        p_ids = [p['id'] for p in self.players]
        if user_id not in p_ids or p_ids.index(user_id) != self.current_turn: return

        # 3. 诗库与查重校验
        poetry_info = self.db.check_exact_poetry(msg_raw)
        if not poetry_info:
            event.stop_event()
            yield event.plain_result(" 库中未查到该句。")
            return
        
        title, author, dynasty = poetry_info
        verse_key = (title, author, msg_raw)
        if verse_key in self.used_verses:
            event.stop_event()
            yield event.plain_result(f" 诗句重复！本局已出现过：\n{msg_raw} ({author}·《{title}》)")
            return

        # 4. 算分与标记逻辑
        curr_num = len(self.history) + 1
        score_add = 0
        match_count = 0
        this_turn_scored_chars = set()
        last_banned = self.banned_score_chars

        if curr_num > 2:
            prev2, prev1 = self.history[-2], self.history[-1]
            if not (set(msg_raw) & set(prev2) and set(msg_raw) & set(prev1)):
                event.stop_event()
                yield event.plain_result(" 不符衔字规则！需含前两句各至少一字。")
                return

            sc_list = list(msg_raw)
            s2_list, s1_list = list(prev2), list(prev1)
            
            sc_rem = []
            for c in sc_list:
                if c in s2_list and c not in last_banned:
                    match_count += 1
                    s2_list.remove(c)
                    this_turn_scored_chars.add(c)
                else: sc_rem.append(c)
            
            for c in sc_rem:
                if c in s1_list and c not in last_banned:
                    match_count += 1
                    s1_list.remove(c)
                    this_turn_scored_chars.add(c)
            
            if match_count > 0: score_add = 2 ** match_count

        # 5. 状态刷新与消息分发
        self.players[self.current_turn]['score'] += score_add
        self.used_verses.append(verse_key)
        self.history.append(msg_raw)
        self.banned_score_chars = this_turn_scored_chars

        res_main = [f" 【{user_name}】接龙成功！"]
        if curr_num > 2:
            res_main.append(f" 匹配字数：{match_count} (冷却排除：{'、'.join(last_banned) if last_banned else '无'})")
            res_main.append(f" 获得积分：{score_add}，总分：{self.players[self.current_turn]['score']}")
        else:
            res_main.append(" 铺垫阶段，暂不计分。")
        
        res_main.append("-" * 15)
        visual_set = set(msg_raw) & (set(self.history[-2] if curr_num > 1 else "") | set(self.history[-3] if curr_num > 2 else ""))
        def mark(t, s): return "".join([f"【{c}】" if c in s else c for c in t])
        
        for i in range(max(0, curr_num-3), curr_num-1):
            res_main.append(f"{i+1}. {mark(self.history[i], set(msg_raw))}")
        res_main.append(f"{curr_num}. {mark(msg_raw, visual_set)} ({author}·《{title}》)")

        yield event.plain_result("\n".join(res_main))

        # 下一玩家提醒
        if len(self.players) > 1:
            self.current_turn = (self.current_turn + 1) % len(self.players)
            next_player = self.players[self.current_turn]['name']
            reminder = [
                f"👉 下一名玩家：【{next_player}】",
                f" 限时：{self.timeout_seconds}s",
                f" 计分冷却字：{'、'.join(this_turn_scored_chars) if this_turn_scored_chars else '无'}"
            ]
            yield event.plain_result("\n".join(reminder)) 
        else:
            yield event.plain_result(" 正在等待更多玩家加入...")
        
        self.start_timer(event)
        event.stop_event()