import os
import json
import time
import sqlite3
import re

class BaseGameEngine:
    def __init__(self, session_id, db_source, save_dir, timeout_seconds=90, save_filename=None):
        self.session_id = str(session_id)
        self.db_source = db_source
        self.save_dir = save_dir
        
        self.game_type_tag = "crossword" if "Crossword" in self.__class__.__name__ else "flowing"
        
        # 🌟 如果指定了存档名就用指定的，否则用时间戳生成全新的存档！
        if save_filename:
            self.save_file = os.path.join(save_dir, save_filename)
        else:
            timestamp = int(time.time())
            self.save_file = os.path.join(save_dir, f"game_{self.session_id}_{self.game_type_tag}_{timestamp}.json")
        
        self.state = {
            "game_type": self.__class__.__name__,
            "players": [],         
            "current_turn": 0,
            "turn_count": 0,       
            "history": [],         
            "round_records": [],   
            "timeout_seconds": timeout_seconds, 
            "custom_data": {},
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) # 🌟 记录开局时间
        }
        self.last_active_time = time.time()
        os.makedirs(self.save_dir, exist_ok=True)

    def get_timeout(self):
        return self.state.get("timeout_seconds", 90)

    def update_activity(self):
        self.last_active_time = time.time()

    def check_active_timeout(self):
        """🌟 核心新增：后台主动探测是否超时，并返回处理动作"""
        if time.time() - self.last_active_time > self.get_timeout():
            if len(self.state["players"]) == 0:
                return True, "end", " 飞花令超时无人加入，已自动解散。"
            elif len(self.state["players"]) == 1:
                return True, "end", " 飞花令只有 1 人加入且长时间未操作，已自动解散。"
            else:
                curr_p = self.state["players"][self.state["current_turn"]]
                self.next_turn()
                self.update_activity()
                self.save_state()
                next_p = self.state["players"][self.state["current_turn"]]
                
                # 强行重置挂起状态（针对纵横飞花令的保护）
                custom = self.state.get("custom_data", {})
                if custom.get("pending_options"):
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                    
                return True, "skip", f"⏳ [{curr_p['name']}] 思考超时！已自动剥夺其回合。\n👉 现在轮到：[{next_p['name']}]"
        return False, "", ""

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
        
        msg = f" 玩家[{user_name}]成功加入游戏！\n当前排位：第 {len(players)} 号位。"
        if len(players) == 1:
            msg += "\n您是首位玩家，可以直接发送诗句开始接龙！"
        else:
            msg += f"\n👉 当前轮到：[{players[self.state['current_turn']]['name']}]"
            
        return {"status": "success", "msg": msg}

    def process_quit(self, user_id, user_name):
        """通用的退出逻辑"""
        players = self.state["players"]
        idx = -1
        for i, p in enumerate(players):
            if p['id'] == str(user_id):
                idx = i
                break
                
        if idx == -1:
            return {"status": "ignore"} 
            
        curr_turn = self.state["current_turn"]
        if idx < curr_turn:
            self.state["current_turn"] -= 1
            
        # 🌟 核心修改：不直接删除，而是移入已退出玩家列表
        quitter = players.pop(idx)
        self.state.setdefault("quit_players", []).append(quitter)
        
        self.update_activity()
        self.save_state()
        
        if not players:
            self.state["current_turn"] = 0
            return {"status": "success", "msg": f" 玩家[{user_name}]已退出游戏。\n当前对局已无活跃玩家，等待新玩家加入。"}
            
        self.state["current_turn"] %= len(players)
        next_p = players[self.state["current_turn"]]
        
        msg = f" 玩家[{user_name}]已退出游戏。"
        if idx == curr_turn:
            msg += f"\n👉 轮次顺延，现在轮到：[{next_p['name']}]"
            
        return {"status": "success", "msg": msg}

    def next_turn(self):
        players = self.state["players"]
        if len(players) > 1:
            self.state["current_turn"] = (self.state["current_turn"] + 1) % len(players)

    def record_round_scores(self):
        # 🌟 快照记录所有玩家（包含已退出的），这样折线图不会断掉
        all_players = self.state["players"] + self.state.get("quit_players", [])
        snapshot = {p['name']: p['score'] for p in all_players}
        self.state["round_records"].append({
            "round": self.state["turn_count"],
            "scores": snapshot
        })

    def _check_db(self, msg_raw):
        """统一的数据库查询接口（强制完整单句匹配）"""
        clean_verse = re.sub(r'[^\u4e00-\u9fa5]', '', msg_raw)
        
        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, 'db_path', None)
        if db_path and os.path.exists(db_path):
            try:
               
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    # 1. 粗筛：找出包含该片段的全诗
                    cursor.execute("SELECT title, author, dynasty, content FROM poems WHERE content LIKE ?", (f'%{clean_verse}%',))
                    rows = cursor.fetchall()
                    
                    # 2. 精筛：按标点符号切分，必须与其中一整句完全一样才算数
                    for title, author, dynasty, content in rows:
                        sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                        pure_sentences = [re.sub(r'[^\u4e00-\u9fa5]', '', s) for s in sentences if s]
                        if clean_verse in pure_sentences:
                            return title, author, dynasty
            except Exception as e:
                print(f"[Debug] _check_db 查询失败: {e}")
                
        return None

    def generate_text_report(self):
        """纯文本战报生成"""
        # 🌟 合并两份名单一起生成战报
        all_players = self.state["players"] + self.state.get("quit_players", [])
        if not all_players: return "暂无玩家参与，无法生成战报。"
        
        game_name = "纵横飞花令" if "Crossword" in self.state["game_type"] else "衔字飞花令"
        report = [f" 【{game_name}】对局战报 ", "="*20]
        
        players_sorted = sorted(all_players, key=lambda x: x["score"], reverse=True)
        report.append(" 最终排名：")
        for i, p in enumerate(players_sorted, 1):
            # 🌟 如果玩家在退出列表里，给个打上 (已退出) 标签
            status_tag = " (已退出)" if p in self.state.get("quit_players", []) else ""
            report.append(f" {i}. [{p['name']}]{status_tag} - {p['score']} 分")
            
        report.append("-" * 15)
        report.append(f" 游戏总回合：{self.state['turn_count']}")
        report.append(f" 共接龙诗句：{len(self.state['history'])} 句")
        
        if self.state["round_records"]:
            report.append("-" * 15)
            report.append("📈 战局逆转回顾：")
            records = self.state["round_records"]
            step = max(1, len(records) // 5) 
            for idx in range(0, len(records), step):
                r = records[idx]
                report.append(f"[第{r['round']}回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))
            if (len(records) - 1) % step != 0: 
                r = records[-1]
                report.append(f"[最终回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))

        return "\n".join(report)

    def step(self, action_type, user_id, user_name, payload=""):
        raise NotImplementedError