import re
try:
    # 当作为 AstrBot 插件或模块运行时，使用相对导入
    from .base_game import BaseGameEngine
except ImportError:
    # 当在本地作为单个脚本直接运行时，使用同级绝对导入
    from base_game import BaseGameEngine

class FlowingPetalsEngine(BaseGameEngine):
    # 🌟 修复点：增加 timeout_seconds 参数接收
    def __init__(self, session_id, db_source, save_dir, timeout_seconds=60, save_filename=None):
        # 🌟 透传给父类
        super().__init__(session_id, db_source, save_dir, timeout_seconds, save_filename)
        
        if not self.state.get("custom_data"):
            self.state["custom_data"] = {
                "used_verses_keys": [],
                "banned_score_chars": []
            }

    def get_status_str(self):
        """获取当前局势文本"""
        if not self.state["players"]:
            return "当前无玩家加入。"
        resp = [" 当前局势："]
        for i, p in enumerate(self.state["players"], 1):
            tag = " 👈 当前轮次" if (i - 1) == self.state["current_turn"] else ""
            resp.append(f" {i}. [{p['name']}] 积分：{p['score']}{tag}")
        
        banned = self.state.get("custom_data", {}).get("banned_score_chars", [])
        if banned:
            resp.append(f" 当前冷却字：{'、'.join(banned)}")
            
        # 🌟 核心修改：追加最近两句接龙历史，供玩家参考接龙
        resp.append("-" * 15)
        history = self.state.get("history", [])
        if not history:
            resp.append(" 接龙记录：暂无（请当前玩家发送首句开局）")
        else:
            recent_history = history[-2:]
            start_idx = len(history) - len(recent_history) + 1
            
            if len(recent_history) == 1:
                resp.append(" 场上最新诗句（需包含其中至少一字）：")
            else:
                resp.append(" 场上最新诗句（需包含以下两句各至少一字）：")
                
            for i, h_item in enumerate(recent_history):
                resp.append(f" {start_idx + i}. {h_item}")
                
        return "\n".join(resp)

    def process_join(self, user_id, user_name):
        """重写加入逻辑，附加当前局势"""
        res = super().process_join(user_id, user_name)
        if res["status"] == "success":
            res["msg"] += f"\n{'-'*15}\n{self.get_status_str()}"
        return res

    def process_quit(self, user_id, user_name):
        """重写退出逻辑，附加当前局势"""
        res = super().process_quit(user_id, user_name)
        if res.get("status") == "success" and self.state["players"]:
            res["msg"] += f"\n{'-'*15}\n{self.get_status_str()}"
        return res

    def step(self, action_type, user_id, user_name, payload=""):
        self.update_activity()
        user_id = str(user_id)
        
        if action_type == "join":
            return self.process_join(user_id, user_name)
        if action_type == "quit":
            return self.process_quit(user_id, user_name)
            
        if not self.state["players"]: return {"status": "ignore"}
        
        current_p = self.state["players"][self.state["current_turn"]]
        if user_id != current_p['id']: return {"status": "ignore"}

        msg_raw = payload.strip()
        verse = re.sub(r'[^\u4e00-\u9fa5]', '', msg_raw)
        if not verse: return {"status": "ignore"}

        # 🌟 修复：去掉了 prefix +
        if len(verse) < 3:
            return {"status": "error", "msg": " 接龙失败！必须是一句完整的诗，且至少需要 3 个字哦。"}

        poetry_info = self._check_db(msg_raw)
        
        if not poetry_info: return {"status": "error", "msg": " 库中未查到该句。请确保你输入的是【一整句完整的诗】！"}
        
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
            # 安全提取前两句的纯汉字部分
            prev2 = re.sub(r'[^\u4e00-\u9fa5]', '', self.state["history"][-2].split(' (')[0])
            prev1 = re.sub(r'[^\u4e00-\u9fa5]', '', self.state["history"][-1].split(' (')[0])
            
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
        
        # 提取冷却字信息
        last_banned_str = "、".join(last_banned) if last_banned else "无"
        next_banned_str = "、".join(this_turn_scored_chars) if this_turn_scored_chars else "无"
        
        # 🌟 构造带有【】的历史回顾
        history = self.state["history"]
        display_lines = []
        
        def mark_history(h_item, target_chars):
            if ' (' in h_item:
                v, a_part = h_item.split(' (', 1)
                marked_v = "".join([f"【{c}】" if c in target_chars else c for c in v])
                return f"{marked_v} ({a_part}"
            return "".join([f"【{c}】" if c in target_chars else c for c in h_item])

        if curr_num >= 3:
            v_curr = msg_raw
            v_prev1 = re.sub(r'[^\u4e00-\u9fa5]', '', history[-2].split(' (')[0])
            v_prev2 = re.sub(r'[^\u4e00-\u9fa5]', '', history[-3].split(' (')[0])
            visual_set = set(v_curr) & (set(v_prev1) | set(v_prev2))
            
            display_lines.append(f"{curr_num-2}. " + mark_history(history[-3], set(v_curr)))
            display_lines.append(f"{curr_num-1}. " + mark_history(history[-2], set(v_curr)))
            display_lines.append(f"{curr_num}. " + mark_history(history[-1], visual_set))
        elif curr_num == 2:
            v_curr = msg_raw
            v_prev1 = re.sub(r'[^\u4e00-\u9fa5]', '', history[-2].split(' (')[0])
            visual_set = set(v_curr) & set(v_prev1)
            display_lines.append(f"{curr_num-1}. " + mark_history(history[-2], set(v_curr)))
            display_lines.append(f"{curr_num}. " + mark_history(history[-1], visual_set))
        else:
            display_lines.append(f"{curr_num}. " + history[-1])
            
        history_display = "\n".join(display_lines)
        
        msg = (
            f" [{user_name}] 接龙成功！\n"
            f" 本轮得分：+{score_add} 分 (匹配 {match_count} 字，冷却：{last_banned_str})\n"
            f" 当前总分：{current_p['score']} 分\n"
            f" 产生冷却：{next_banned_str} (下家不可用)\n"
            f"{'-'*15}\n"
            f"{history_display}\n"
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
        save_file = "./saves/game_local_test_flowing.json"
        if os.path.exists(save_file):
            os.remove(save_file)
            
        engine = FlowingPetalsEngine(session_id="local_test", db_source=db_path, save_dir="./saves")
        print(engine.step("join", "u1", "阿麟")["msg"])
        print(engine.step("join", "u2", "测试员张三")["msg"])
        
        while True:
            curr_player = engine.state["players"][engine.state["current_turn"]]
            user_text = input(f"\n[{curr_player['name']}] > ").strip()
            if user_text.lower() == 'q': break
            
            response = engine.step("play", curr_player["id"], curr_player["name"], user_text)
            if response.get("status") != "ignore" and response.get("msg"):
                print(f"\n{response['msg']}")