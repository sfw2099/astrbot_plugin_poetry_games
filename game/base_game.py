import os
import json
import time
import sqlite3
import re

class BaseGameEngine:
    def __init__(self, session_id, db_source, save_dir):
        self.session_id = str(session_id)
        self.db_source = db_source
        self.save_dir = save_dir
        self.save_file = os.path.join(save_dir, f"game_{self.session_id}.json")
        
        # 统一的标准状态结构
        self.state = {
            "game_type": self.__class__.__name__,
            "players": [],         # 结构: {'id': '123', 'name': '阿麟', 'score': 0}
            "current_turn": 0,
            "turn_count": 0,       # 记录发生过几次成功接龙
            "history": [],         # 记录所有成功的诗句
            "round_records": [],   # 每回合分数快照
            "custom_data": {}      # 子类自定义数据
        }
        self.last_active_time = time.time()
        os.makedirs(self.save_dir, exist_ok=True)

    def update_activity(self):
        self.last_active_time = time.time()

    def is_timeout(self, timeout_seconds=120):
        return time.time() - self.last_active_time > timeout_seconds

    def save_state(self):
        """持久化保存至 JSON"""
        try:
            with open(self.save_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Debug] 存档失败: {e}")

    def load_state(self):
        """从 JSON 恢复状态"""
        if os.path.exists(self.save_file):
            try:
                with open(self.save_file, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
                return True
            except Exception:
                return False
        return False

    def process_join(self, user_id, user_name):
        """通用的热插拔加入逻辑"""
        players = self.state["players"]
        if any(p['id'] == str(user_id) for p in players):
            return {"status": "ignore"} 
            
        players.append({"id": str(user_id), "name": user_name, "score": 0})
        self.update_activity()
        self.save_state()
        
        msg = f"🎉 玩家[{user_name}]成功加入游戏！\n当前排位：第 {len(players)} 号位。"
        if len(players) == 1:
            msg += "\n您是首位玩家，可以直接发送诗句开始接龙！"
        else:
            msg += f"\n👉 当前轮到：[{players[self.state['current_turn']]['name']}]"
            
        return {"status": "success", "msg": msg}

    def next_turn(self):
        players = self.state["players"]
        if len(players) > 1:
            self.state["current_turn"] = (self.state["current_turn"] + 1) % len(players)

    def record_round_scores(self):
        snapshot = {p['name']: p['score'] for p in self.state["players"]}
        self.state["round_records"].append({
            "round": self.state["turn_count"],
            "scores": snapshot
        })

    def _check_db(self, msg_raw):
        """统一的数据库查询接口（兼容 Bot 和本地测试）"""
        if hasattr(self.db_source, 'check_exact_poetry'):
            return self.db_source.check_exact_poetry(msg_raw)
        if isinstance(self.db_source, str):
            clean_verse = re.sub(r'[^\u4e00-\u9fa5]', '', msg_raw)
            try:
                with sqlite3.connect(self.db_source) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT title, author, dynasty FROM poems WHERE content LIKE ?", (f'%{clean_verse}%',))
                    return cursor.fetchone()
            except Exception:
                pass
        return None

    def generate_text_report(self):
        """纯文本战报生成"""
        if not self.state["players"]: return "暂无玩家参与，无法生成战报。"
        
        game_name = "纵横飞花令" if "Crossword" in self.state["game_type"] else "衔字飞花令"
        report = [f"📊 【{game_name}】对局战报 📊", "="*20]
        
        players_sorted = sorted(self.state["players"], key=lambda x: x["score"], reverse=True)
        report.append("🏆 最终排名：")
        for i, p in enumerate(players_sorted, 1):
            report.append(f" {i}. [{p['name']}] - {p['score']} 分")
            
        report.append("-" * 15)
        report.append(f"📜 游戏总回合：{self.state['turn_count']}")
        report.append(f"📚 共接龙诗句：{len(self.state['history'])} 句")
        
        if self.state["round_records"]:
            report.append("-" * 15)
            report.append("📈 战局逆转回顾：")
            records = self.state["round_records"]
            step = max(1, len(records) // 5) # 挑重点展示
            for idx in range(0, len(records), step):
                r = records[idx]
                report.append(f"[第{r['round']}回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))
            if (len(records) - 1) % step != 0: 
                r = records[-1]
                report.append(f"[最终回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))

        return "\n".join(report)

    def step(self, action_type, user_id, user_name, payload=""):
        raise NotImplementedError