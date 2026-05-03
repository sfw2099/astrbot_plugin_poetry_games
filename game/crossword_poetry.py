import random
import re
import os
import sqlite3
from PIL import Image, ImageDraw, ImageFont
try:
    from .base_game import BaseGameEngine
except ImportError:
    from base_game import BaseGameEngine

class PoetryCrosswordEngine(BaseGameEngine):
    def __init__(self, session_id, db_source, save_dir, width=24, height=24, timeout_seconds=90, save_filename=None):
        super().__init__(session_id, db_source, save_dir, timeout_seconds, save_filename)
        self.WIDTH = width
        self.HEIGHT = height
        self.CELL_SIZE = 40
        self.BOARD_W_PX = width * self.CELL_SIZE
        self.BOARD_H_PX = height * self.CELL_SIZE
        
        # ==========================================
        #  棋盘 UI 颜色全局配置 
        # ==========================================
        self.COLOR_BG = '#F8F9FA'               # 棋盘背景色 (浅灰白)
        self.COLOR_GRID_LINE = '#E0E0E0'        # 网格线颜色 (浅灰)
        self.COLOR_TEXT = '#333333'             # 诗句汉字颜色 (深灰/近黑)
        
        self.COLOR_HIGHLIGHT_FILL = '#FFD700'   # 抉择选项的高亮路径背景色 (金黄)
        self.COLOR_HIGHLIGHT_TEXT = '#E53935'   # 抉择起止点的数字标签色 (醒目红)
        
        self.COLOR_SYSTEM_CELL = '#E6F3FF'      # 系统开局格子的颜色 (淡蓝)
        
        self.COLOR_PALETTE = [
            '#FFB3BA', '#FFDFBA', '#BAFFC9', '#BAE1FF', 
            '#E8BAFF', '#C2F0C2', '#957DAD', 
        ]
        # ==========================================

        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.font_path = os.path.join(current_dir, "STZHONGS.TTF") 
        try:
            self.font = ImageFont.truetype(self.font_path, 28)
            self.label_font = ImageFont.truetype(self.font_path, 16) 
        except:
            self.font = ImageFont.load_default()
            self.label_font = ImageFont.load_default()

        if not self.state.get("custom_data"):
            self.state["custom_data"] = {
                "grid": [[None for _ in range(width)] for _ in range(height)],
                "is_empty": True,
                "pending_verse": None,
                "pending_options": [],
                "pending_player_id": None,
                "player_colors": {"system": self.COLOR_SYSTEM_CELL} 
            }
            
            if not os.path.exists(self.save_file):
                start_verse = self._get_random_verse()
                start_x = (self.WIDTH - len(start_verse)) // 2
                start_y = self.HEIGHT // 2
                self._execute_placement(start_verse, start_x, start_y, 'H', "system", "系统")
                self.state["history"].append(f"{start_verse} (系统开局)")
                self.save_state()

        self.render_path = os.path.join(save_dir, f"crossword_cache_{session_id}.png")

    def _get_random_verse(self):
        fallback_verses = ["天若有情天亦老", "春江潮水连海平", "海上明月共潮生", "黄河之水天上来", "同是天涯沦落人", "人生得意须尽欢", "我言秋日胜春朝"]
        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, 'db_path', None)
        
        if db_path and os.path.exists(db_path):
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT content FROM poems ORDER BY RANDOM() LIMIT 10")
                    for row in cursor.fetchall():
                        content = row[0]
                        sentences = re.split(r'[，。！？\n\r\s]+', content)
                        valid = [s for s in sentences if len(s) in (5, 7) and re.match(r'^[\u4e00-\u9fa5]+$', s)]
                        if valid:
                            return random.choice(valid)
            except Exception as e:
                print(f"[Debug] 抽取随机诗句失败: {e}")
                
        return random.choice(fallback_verses)

    def _get_player_color(self, player_id):
        colors = self.state["custom_data"]["player_colors"]
        if player_id not in colors:
            used_colors = set(colors.values())
            available_colors = [c for c in self.COLOR_PALETTE if c not in used_colors]
            
            if available_colors:
                colors[player_id] = random.choice(available_colors)
            else:
                colors[player_id] = "#{:06x}".format(random.randint(0, 0xFFFFFF))
                
        return colors[player_id]

    def _calculate_territory_scores(self):
        # 🌟 合并全部玩家名单（包括已退出的）
        all_players = self.state["players"] + self.state.get("quit_players", [])
        scores = {p['name']: 0 for p in all_players}
        grid = self.state["custom_data"]["grid"]
        
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                if cell and cell['owner'] in scores:
                    scores[cell['owner']] += 1
                    
        for p in all_players:
            if p['name'] in scores:
                p['score'] = scores[p['name']]

    def check_collision(self, verse, start_x, start_y, direction):
        grid = self.state["custom_data"]["grid"]
        new_cells_count = 0  
        
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT:
                return False
                
            if grid[y][x] is not None:
                if grid[y][x]['char'] != char:
                    return False
            else:
                new_cells_count += 1
                
        return new_cells_count > 0

    def _execute_placement(self, verse, start_x, start_y, direction, player_id, player_name):
        color = self._get_player_color(player_id)
        grid = self.state["custom_data"]["grid"]
        intersection_points = []
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if grid[y][x] is not None:
                intersection_points.append((x, y))
            
            # 🌟 新增：记录该格子的变色/占领次数
            old_owner = grid[y][x]['owner'] if grid[y][x] else None
            changes = grid[y][x].get('changes', 0) if grid[y][x] else 0
            if old_owner != player_name:
                changes += 1
                
            grid[y][x] = {'char': char, 'color': color, 'owner': player_name, 'changes': changes}
            
        self.state["custom_data"]["is_empty"] = False
        
        for ix, iy in intersection_points:
            for dx, dy in [(-1,0), (1,0), (0,-1), (0,1), (-1,-1), (1,1), (-1,1), (1,-1)]:
                nx, ny = ix + dx, iy + dy
                if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                    if grid[ny][nx] is not None:
                        # 🌟 新增：蔓延占领时也记录变色次数
                        old_owner = grid[ny][nx]['owner']
                        if old_owner != player_name:
                            grid[ny][nx]['changes'] = grid[ny][nx].get('changes', 0) + 1
                        grid[ny][nx]['color'] = color
                        grid[ny][nx]['owner'] = player_name

    def render_image(self, pending_options=None, pending_verse=None):
        """🌟 终极版路径渲染：点亮整条路径并在首尾标注数字"""
        image = Image.new('RGB', (self.BOARD_W_PX, self.BOARD_H_PX), color=self.COLOR_BG)
        draw = ImageDraw.Draw(image)
        grid = self.state["custom_data"]["grid"]
        
        # 🌟 预处理：提取所有挂起选项的“路径经过的所有格子”和“首尾标签”
        path_highlights = set()
        labels_map = {}
        if pending_options and pending_verse:
            L = len(pending_verse)
            for i, opt in enumerate(pending_options):
                sx, sy = opt['start_x'], opt['start_y']
                direction = opt['dir']
                label_str = str(i + 1)
                
                # 遍历这句诗要占用的每一个格子
                for k in range(L):
                    cx = sx + (k if direction == 'H' else 0)
                    cy = sy + (0 if direction == 'H' else k)
                    path_highlights.add((cx, cy))
                    
                    # 🌟 核心：只在首 (k==0) 和 尾 (k==L-1) 贴上数字标签
                    if k == 0 or k == L - 1:
                        if (cx, cy) not in labels_map:
                            labels_map[(cx, cy)] = []
                        if label_str not in labels_map[(cx, cy)]:
                            labels_map[(cx, cy)].append(label_str)

        # 绘制网格线
        for i in range(self.WIDTH + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(line_pos, 0), (line_pos, self.BOARD_H_PX)], fill=self.COLOR_GRID_LINE, width=1)
        for i in range(self.HEIGHT + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(0, line_pos), (self.BOARD_W_PX, line_pos)], fill=self.COLOR_GRID_LINE, width=1)
            
        # 绘制内容与高亮
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                x0, y0 = x * self.CELL_SIZE, y * self.CELL_SIZE
                rect_coords = [x0+1, y0+1, x0+self.CELL_SIZE-1, y0+self.CELL_SIZE-1]
                
                is_path = (x, y) in path_highlights
                cell_labels = labels_map.get((x, y))
                
                # 1. 铺设背景色
                if is_path:
                    # 🌟 是预测路径，铺上金黄高亮背景
                    draw.rectangle(rect_coords, fill=self.COLOR_HIGHLIGHT_FILL)
                elif cell is not None:
                    # 已经被占领的正常格子
                    draw.rectangle(rect_coords, fill=cell['color'])
                    
                # 2. 绘制文字 (标签优先，如果没有标签但有汉字，画汉字)
                if cell_labels:
                    # 🌟 画首尾的红色数字标签
                    label = "/".join(cell_labels)
                    font_to_use = self.label_font if len(label) > 1 else self.font
                    bbox = draw.textbbox((0, 0), label, font=font_to_use)
                    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
                    draw.text((x0+(self.CELL_SIZE-tw)/2, y0+(self.CELL_SIZE-th)/2-4), label, fill=self.COLOR_HIGHLIGHT_TEXT, font=font_to_use)
                elif cell is not None:
                    # 🌟 画原本的诗句汉字 (哪怕底下是金黄高亮色，黑字也能完美浮现在上面！)
                    bbox = draw.textbbox((0, 0), cell['char'], font=self.font)
                    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
                    draw.text((x0+(self.CELL_SIZE-tw)/2, y0+(self.CELL_SIZE-th)/2-4), cell['char'], fill=self.COLOR_TEXT, font=self.font)
        
        image.save(self.render_path)
        return self.render_path

    def process_quit(self, user_id, user_name):
        custom = self.state.get("custom_data", {})
        if custom.get("pending_options") and custom.get("pending_player_id") == str(user_id):
            custom["pending_options"] = []
            custom["pending_verse"] = None
        return super().process_quit(user_id, user_name)

    def _finalize_success_turn(self, user_name, verse, title="", author=""):
        self._calculate_territory_scores()
        
        if title: 
            self.state["history"].append(f"{verse} ({author}·《{title}》)")
        else: 
            self.state["history"].append(verse)
        
        self.state["turn_count"] += 1
        self.record_round_scores()
        self.next_turn()
        self.save_state()
        
        players = self.state["players"]
        curr_p = next((p for p in players if p['name'] == user_name), None)
        next_name = players[self.state["current_turn"]]["name"]
        
        msg = (
            f" [{user_name}] 落子成功！\n"
            f" 诗句：{verse} " + (f"({author})" if author else "") + "\n"
            f" 当前领地总分：{curr_p['score']} 格\n"
            f"{'-' * 15}\n"
            f"👉 下一位：[{next_name}]"
        )
        return {"status": "success", "msg": msg, "image": self.render_image()}

    def step(self, action_type, user_id, user_name, payload=""):
        self.update_activity()
        user_id = str(user_id)
        
        if action_type == "join":
            return self.process_join(user_id, user_name)
        if action_type == "quit":
            return self.process_quit(user_id, user_name)
            
        if not self.state["players"]: return {"status": "ignore"}
        
        current_p = self.state["players"][self.state["current_turn"]]
        custom = self.state["custom_data"]

        # ===============================================
        # 1. 处理多选项抉择分支
        # ===============================================
        if custom.get("pending_options"):
            if user_id != custom["pending_player_id"]: return {"status": "ignore"}
            if payload.strip().lower() in ['取消', 'q']:
                custom["pending_options"] = []
                custom["pending_verse"] = None
                return {"status": "success", "msg": "已取消操作。"}

            if payload.strip().isdigit():
                choice_idx = int(payload.strip()) - 1
                options = custom["pending_options"]
                
                if 0 <= choice_idx < len(options):
                    opt = options[choice_idx]
                    self._execute_placement(custom["pending_verse"], opt['start_x'], opt['start_y'], opt['dir'], user_id, user_name)
                    
                    verse_cache = custom["pending_verse"]
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                    custom["pending_player_id"] = None
                    
                    return self._finalize_success_turn(user_name, verse_cache)

            # 🌟 玩家输入错误数字时，直接用缓存数据重绘路线
            image_path = self.render_image(pending_options=custom["pending_options"], pending_verse=custom["pending_verse"])
            msg = (
                f" 选择无效。\n"
                f"请直接发送图片中首尾两端标记的【数字】（如：1）。"
            )
            return {"status": "success", "msg": msg, "image": image_path}

        # ===============================================
        # 处理跳过与回合拦截
        # ===============================================
        if action_type == "skip":
            if len(self.state["players"]) <= 1:
                return {"status": "error", "msg": "当前只有你一个人在玩，没法跳过哦！"}
            import time
            timeout_limit = self.get_timeout()
            if time.time() - self.last_active_time < timeout_limit:
                rem = int(timeout_limit - (time.time() - self.last_active_time))
                return {"status": "error", "msg": f" 还没到超时时间，请再给 TA {rem} 秒吧！"}
            curr_p = self.state["players"][self.state["current_turn"]]
            if custom.get("pending_options") and custom["pending_player_id"] == curr_p["id"]:
                custom["pending_options"] = []
                custom["pending_verse"] = None
            self.next_turn()
            self.update_activity()
            self.save_state()
            next_p = self.state["players"][self.state["current_turn"]]
            return {"status": "success", "msg": f" 已强制跳过超时的 [{curr_p['name']}]！\n👉 下一位：[{next_p['name']}]"}

        if user_id != current_p['id']: return {"status": "ignore"}

        # ===============================================
        # 2. 处理常规诗句接龙分支
        # ===============================================
        user_input = payload.strip()
        verse = re.sub(r'[^\u4e00-\u9fa5]', '', user_input)
        if not verse: return {"status": "ignore"}

        if len(verse) < 3:
            return {"status": "error", "msg": " 落子失败！必须是一句完整的诗，且至少需要 3 个字哦。"}
            
        used_verses = [re.sub(r'[^\u4e00-\u9fa5]', '', h.split(' (')[0]) for h in self.state.get("history", [])]
        if verse in used_verses:
            return {"status": "error", "msg": f" 诗句重复！【{verse}】已在棋盘上，请换一句。"}
        
        poetry_info = self._check_db(verse)
        if not poetry_info: return {"status": "error", "msg": " 库中未查到该句。请确保你输入的是【一整句完整的诗】！"}
        
        title, author, _ = poetry_info
        
        valid_placements = []
        seen_placements = set()

        grid = custom["grid"]
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                if cell is not None and cell['char'] in verse:
                    for idx, c in enumerate(verse):
                        if c == cell['char']:
                            start_x_h = x - idx
                            signature_h = (start_x_h, y, 'H') 
                            if signature_h not in seen_placements and self.check_collision(verse, start_x_h, y, 'H'):
                                seen_placements.add(signature_h)
                                valid_placements.append({'start_x': start_x_h, 'start_y': y, 'dir': 'H', 'dir_str': '水平'})
                                
                            start_y_v = y - idx
                            signature_v = (x, start_y_v, 'V') 
                            if signature_v not in seen_placements and self.check_collision(verse, x, start_y_v, 'V'):
                                seen_placements.add(signature_v)
                                valid_placements.append({'start_x': x, 'start_y': start_y_v, 'dir': 'V', 'dir_str': '垂直'})

        if not valid_placements:
            return {"status": "error", "msg": " 落子失败！找不到合法的交叉点，或者该诗句完全与已有汉字重叠（必须向外延伸，占用至少一个新格子）。"}
            
        elif len(valid_placements) == 1:
            opt = valid_placements[0]
            self._execute_placement(verse, opt['start_x'], opt['start_y'], opt['dir'], user_id, user_name)
            return self._finalize_success_turn(user_name, verse, title, author)
        else:
            custom["pending_verse"] = verse
            custom["pending_player_id"] = user_id
            custom["pending_options"] = valid_placements
            
            # 🌟 传入选项和接龙诗句，底层自动渲染整条路径
            image_path = self.render_image(pending_options=valid_placements, pending_verse=verse)
            
            prompt = (
                f" 发现 {len(valid_placements)} 种合法的落子方式！\n"
                f"图片中已用金色高亮了所有可选路线。\n"
                f"请查看你要选的那条路线，发送首尾两端对应的【数字】（如：1）即可落子。"
            )
            return {"status": "pending", "msg": prompt, "image": image_path}

# ================= 本地运行测试 =================
if __name__ == "__main__":
    import os
    from PIL import Image
    
    db_path = r"D:\ALin-Data\AstrBot-plugins\poetry_data.db" 
    
    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在，请检查路径: {db_path}")
    else:
        save_file = "./saves/game_local_test_crossword.json"
        if os.path.exists(save_file):
            os.remove(save_file)
            
        engine = PoetryCrosswordEngine(session_id="local_test_crossword", db_source=db_path, save_dir="./saves")
        print("纵横飞花令 本地测试引擎启动。")
        
        print(engine.step("join", "u1", "阿麟")["msg"])
        print(engine.step("join", "u2", "测试员张三")["msg"])
        print("\n提示：系统已自动安排两人加入并随机开局。输入 'q' 退出，输入 'report' 查看战报。")
        
        Image.open(engine.render_image()).show()
        
        while True:
            custom = engine.state["custom_data"]
            
            if custom.get("pending_options"):
                active_id = custom["pending_player_id"]
                active_name = next(p["name"] for p in engine.state["players"] if p["id"] == active_id)
            else:
                curr_player = engine.state["players"][engine.state["current_turn"]]
                active_id = curr_player["id"]
                active_name = curr_player["name"]
            
            user_text = input(f"\n[{active_name}] > ").strip()
            
            if user_text.lower() == 'q': 
                break
            if user_text.lower() == 'report':
                print(engine.generate_text_report())
                try:
                    Image.open(engine.render_image()).show()
                except Exception as e:
                    print(f"(当前棋盘图片展示失败: {e})")
                continue
            
            response = engine.step("play", active_id, active_name, user_text)
            
            if response.get("status") != "ignore" and response.get("msg"):
                print(f"\n[系统播报]\n{response['msg']}")
                
            if "image" in response:
                try:
                    Image.open(response["image"]).show()
                except Exception as e:
                    print(f"(图片渲染成功，已保存在: {response['image']})")