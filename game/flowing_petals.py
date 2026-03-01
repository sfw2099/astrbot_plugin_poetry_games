import re
try:
    # 当作为 AstrBot 插件或模块运行时，使用相对导入
    from .base_game import BaseGameEngine
except ImportError:
    # 当在本地作为单个脚本直接运行时，使用同级绝对导入
    from base_game import BaseGameEngine

class FlowingPetalsEngine(BaseGameEngine):
    def __init__(self, session_id, db_source, save_dir):
        super().__init__(session_id, db_source, save_dir)
        if not self.state["custom_data"]:
            self.state["custom_data"] = {
                "used_verses_keys": [],
                "banned_score_chars": []
            }

    def step(self, action_type, user_id, user_name, payload=""):
        self.update_activity()
        user_id = str(user_id)
        
        if action_type == "join":
            return self.process_join(user_id, user_name)
            
        if not self.state["players"]: return {"status": "ignore"}
        
        current_p = self.state["players"][self.state["current_turn"]]
        if user_id != current_p['id']: return {"status": "ignore"}

        msg_raw = payload.strip()
        verse = re.sub(r'[^\u4e00-\u9fa5]', '', msg_raw)
        if not verse: return {"status": "ignore"}

        poetry_info = self._check_db(msg_raw)
        if not poetry_info: return {"status": "error", "msg": "库中未查到该句。"}
        
        title, author, dynasty = poetry_info
        verse_key = f"{title}_{author}_{msg_raw}"
        
        custom = self.state["custom_data"]
        if verse_key in custom["used_verses_keys"]:
            return {"status": "error", "msg": f"诗句重复！本局已出现过该句。"}

        curr_num = len(self.state["history"]) + 1
        score_add = 0
        match_count = 0
        this_turn_scored_chars = set()
        last_banned = set(custom["banned_score_chars"])

        if curr_num > 2:
            prev2 = re.sub(r'[^\u4e00-\u9fa5]', '', self.state["history"][-2])
            prev1 = re.sub(r'[^\u4e00-\u9fa5]', '', self.state["history"][-1])
            
            if not (set(verse) & set(prev2) and set(verse) & set(prev1)):
                return {"status": "error", "msg": "不符衔字规则！需含前两句各至少一字。"}

            sc_list = list(verse)
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

        # 结算
        current_p['score'] += score_add
        custom["used_verses_keys"].append(verse_key)
        self.state["history"].append(f"{msg_raw} ({author}·《{title}》)")
        custom["banned_score_chars"] = list(this_turn_scored_chars)
        
        self.state["turn_count"] += 1
        self.record_round_scores()
        self.next_turn()
        self.save_state()

        next_name = self.state["players"][self.state["current_turn"]]["name"]
        
        # 🌟 提取冷却字信息
        last_banned_str = "、".join(last_banned) if last_banned else "无"
        next_banned_str = "、".join(this_turn_scored_chars) if this_turn_scored_chars else "无"
        
        # 🌟 构造带冷却字标示的反馈信息
        msg = (
            f"✅ [{user_name}] 接龙成功！\n"
            f"📖 诗句：{msg_raw} ({author})\n"
            f"✨ 本轮得分：+{score_add} 分 (匹配 {match_count} 字，冷却排除：{last_banned_str})\n"
            f"📈 当前总分：{current_p['score']} 分\n"
            f"🧊 产生冷却：{next_banned_str} (下家不可用此计分)\n"
            f"{'-'*15}\n"
            f"👉 下一位：[{next_name}]"
        )
        return {"status": "success", "msg": msg}

# ================= 本地运行测试 =================
if __name__ == "__main__":
    import os
    db_path = r"D:\ALin-Data\AstrBot-plugins\poetry_data.db" 
    
    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在: {db_path}")
    else:
        engine = FlowingPetalsEngine(session_id="local_test", db_source=db_path, save_dir="./saves")
        engine.step("join", "u1", "阿麟")
        engine.step("join", "u2", "测试员张三")
        
        while True:
            curr_player = engine.state["players"][engine.state["current_turn"]]
            user_text = input(f"\n[{curr_player['name']}] > ").strip()
            if user_text.lower() == 'q': break
            
            response = engine.step("play", curr_player["id"], curr_player["name"], user_text)
            # 🌟 修复点：确保只在此处统一打印一次
            if response.get("status") != "ignore" and response.get("msg"):
                print(f"\n{response['msg']}")