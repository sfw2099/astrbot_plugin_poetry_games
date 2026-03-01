import random
import re
import os
import sqlite3
from PIL import Image, ImageDraw, ImageFont
try:
    # 当作为 AstrBot 插件或模块运行时，使用相对导入
    from .base_game import BaseGameEngine
except ImportError:
    # 当在本地作为单个脚本直接运行时，使用同级绝对导入
    from base_game import BaseGameEngine

class PoetryCrosswordEngine(BaseGameEngine):
    def __init__(self, session_id, db_source, save_dir, grid_size=30, cell_size=40):
        super().__init__(session_id, db_source, save_dir)
        self.GRID_SIZE = grid_size
        self.CELL_SIZE = cell_size
        self.BOARD_PX = grid_size * cell_size
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.font_path = os.path.join(current_dir, "STZHONGS.TTF") 
        try:
            self.font = ImageFont.truetype(self.font_path, 28)
        except:
            self.font = ImageFont.load_default()

        if not self.state.get("custom_data"):
            self.state["custom_data"] = {
                "grid": [[None for _ in range(grid_size)] for _ in range(grid_size)],
                "is_empty": True,
                "pending_verse": None,
                "pending_options": [],
                "pending_player_id": None,
                "player_colors": {"system": "#E6F3FF"}
            }
            
            # 🌟 核心修改：从数据库中智能抽取随机诗句开局
            start_verse = self._get_random_verse()
            start_x = (self.GRID_SIZE - len(start_verse)) // 2
            start_y = self.GRID_SIZE // 2
            
            # 系统自动落第一子
            self._execute_placement(start_verse, start_x, start_y, 'H', "system", "系统")
            self.state["history"].append(f"{start_verse} (系统开局)")
            self.save_state()

        self.color_palette = ['#FFB3BA', '#FFDFBA', '#FFFFBA', '#BAFFC9', '#BAE1FF', '#E8BAFF', '#FFBAF3', '#C2F0C2', '#FFD1DC']
        self.render_path = os.path.join(save_dir, f"crossword_cache_{session_id}.png")

    def _get_random_verse(self):
        """从数据库随机抽取一句5言或7言诗作为开局"""
        fallback_verses = ["天若有情天亦老", "春江潮水连海平", "海上明月共潮生", "黄河之水天上来", "同是天涯沦落人", "人生得意须尽欢", "我言秋日胜春朝"]
        
        # 兼容 bot 传入的 db 对象或本地测试传入的 db_path 字符串
        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, 'db_path', None)
        
        if db_path and os.path.exists(db_path):
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    # 随机取10首诗作备选池，防止第一首全是生僻字或长短句
                    cursor.execute("SELECT content FROM poems ORDER BY RANDOM() LIMIT 10")
                    for row in cursor.fetchall():
                        content = row[0]
                        # 按标点符号或换行拆分成单句
                        sentences = re.split(r'[，。！？\n\r\s]+', content)
                        # 挑选纯中文，且长度为 5 或 7 的经典句式
                        valid = [s for s in sentences if len(s) in (5, 7) and re.match(r'^[\u4e00-\u9fa5]+$', s)]
                        if valid:
                            return random.choice(valid)
            except Exception as e:
                print(f"[Debug] 抽取随机诗句失败，使用备选库: {e}")
                
        # 如果数据库报错或没有找到合适句子，使用备选名句
        return random.choice(fallback_verses)

    def _get_player_color(self, player_id):
        colors = self.state["custom_data"]["player_colors"]
        if player_id not in colors:
            colors[player_id] = random.choice(self.color_palette)
        return colors[player_id]

    def _calculate_territory_scores(self):
        """动态清点领地计算得分"""
        scores = {p['name']: 0 for p in self.state["players"]}
        grid = self.state["custom_data"]["grid"]
        for y in range(self.GRID_SIZE):
            for x in range(self.GRID_SIZE):
                cell = grid[y][x]
                if cell and cell['owner'] in scores:
                    scores[cell['owner']] += 1
        for p in self.state["players"]:
            if p['name'] in scores:
                p['score'] = scores[p['name']]

    def check_collision(self, verse, start_x, start_y, direction):
        grid = self.state["custom_data"]["grid"]
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if x < 0 or x >= self.GRID_SIZE or y < 0 or y >= self.GRID_SIZE:
                return False
            if grid[y][x] is not None and grid[y][x]['char'] != char:
                return False
        return True

    def _execute_placement(self, verse, start_x, start_y, direction, player_id, player_name):
        color = self._get_player_color(player_id)
        grid = self.state["custom_data"]["grid"]
        intersection_points = []
        
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if grid[y][x] is not None:
                intersection_points.append((x, y))
            grid[y][x] = {'char': char, 'color': color, 'owner': player_name}
                
        self.state["custom_data"]["is_empty"] = False
        
        # AoE 同化
        for ix, iy in intersection_points:
            for dx, dy in [(-1,0), (1,0), (0,-1), (0,1), (-1,-1), (1,1), (-1,1), (1,-1)]:
                nx, ny = ix + dx, iy + dy
                if 0 <= nx < self.GRID_SIZE and 0 <= ny < self.GRID_SIZE:
                    if grid[ny][nx] is not None:
                        grid[ny][nx]['color'] = color
                        grid[ny][nx]['owner'] = player_name

    def render_image(self):
        image = Image.new('RGB', (self.BOARD_PX, self.BOARD_PX), color='#F8F9FA')
        draw = ImageDraw.Draw(image)
        grid = self.state["custom_data"]["grid"]
        
        for i in range(self.GRID_SIZE + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(0, line_pos), (self.BOARD_PX, line_pos)], fill='#E0E0E0', width=1)
            draw.line([(line_pos, 0), (line_pos, self.BOARD_PX)], fill='#E0E0E0', width=1)
            
        for y in range(self.GRID_SIZE):
            for x in range(self.GRID_SIZE):
                cell = grid[y][x]
                if cell is not None:
                    x0, y0 = x * self.CELL_SIZE, y * self.CELL_SIZE
                    draw.rectangle([x0+1, y0+1, x0+self.CELL_SIZE-1, y0+self.CELL_SIZE-1], fill=cell['color'])
                    bbox = draw.textbbox((0, 0), cell['char'], font=self.font)
                    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
                    draw.text((x0+(self.CELL_SIZE-tw)/2, y0+(self.CELL_SIZE-th)/2-4), cell['char'], fill='#333333', font=self.font)
        image.save(self.render_path)
        return self.render_path

    def _finalize_success_turn(self, user_name, verse, title="", author=""):
        """统一封装接龙成功后的计分与播报"""
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
            f"✅ [{user_name}] 落子成功！\n"
            f"📖 诗句：{verse} " + (f"({author})" if author else "") + "\n"
            f"📈 当前领地总分：{curr_p['score']} 格\n"
            f"{'-' * 15}\n"
            f"👉 下一位：[{next_name}]"
        )
        return {"status": "success", "msg": msg, "image": self.render_image()}

    def step(self, action_type, user_id, user_name, payload=""):
        self.update_activity()
        user_id = str(user_id)
        
        if action_type == "join":
            return self.process_join(user_id, user_name)
            
        if not self.state["players"]: return {"status": "ignore"}
        
        current_p = self.state["players"][self.state["current_turn"]]
        if user_id != current_p['id']: return {"status": "ignore"}

        user_input = payload.strip()
        custom = self.state["custom_data"]

        # 处理多选项挂起状态
        if custom["pending_options"]:
            if user_id != custom["pending_player_id"]: return {"status": "ignore"}
            if user_input.lower() in ['取消', 'q']:
                custom["pending_options"] = []
                custom["pending_verse"] = None
                return {"status": "success", "msg": "已取消操作。"}

            if user_input.isdigit():
                choice_idx = int(user_input) - 1
                if 0 <= choice_idx < len(custom["pending_options"]):
                    best_x, best_y, best_dir, _ = custom["pending_options"][choice_idx]
                    verse = custom["pending_verse"]
                    
                    self._execute_placement(verse, best_x, best_y, best_dir, user_id, user_name)
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                    
                    return self._finalize_success_turn(user_name, verse)
                else:
                    return {"status": "error", "msg": "选项无效，请重新输入。"}
            return {"status": "error", "msg": "检测到多选项，请回复数字选择。"}

        # 处理常规诗句接龙
        verse = re.sub(r'[^\u4e00-\u9fa5]', '', user_input)
        if not verse: return {"status": "ignore"}
        
        poetry_info = self._check_db(verse)
        if not poetry_info: return {"status": "error", "msg": "未在数据库中查找到该诗句。"}
        title, author, _ = poetry_info
        
        valid_placements = []
        grid = custom["grid"]
        for y in range(self.GRID_SIZE):
            for x in range(self.GRID_SIZE):
                cell = grid[y][x]
                if cell is not None and cell['char'] in verse:
                    for idx, c in enumerate(verse):
                        if c == cell['char']:
                            if self.check_collision(verse, x - idx, y, 'H'):
                                valid_placements.append((x - idx, y, 'H', c))
                            if self.check_collision(verse, x, y - idx, 'V'):
                                valid_placements.append((x, y - idx, 'V', c))

        if not valid_placements:
            return {"status": "error", "msg": "找不到合法的交叉点，或空间受限。"}
            
        elif len(valid_placements) == 1:
            best_x, best_y, best_dir, _ = valid_placements[0]
            self._execute_placement(verse, best_x, best_y, best_dir, user_id, user_name)
            return self._finalize_success_turn(user_name, verse, title, author)
        else:
            custom["pending_verse"] = verse
            custom["pending_player_id"] = user_id
            custom["pending_options"] = valid_placements
            prompt = f"发现 {len(valid_placements)} 种方式，回复数字选择：\n"
            for i, p in enumerate(valid_placements, 1):
                prompt += f"{i}. 借用(行{p[1]+1},列{p[0]+1})的[{p[3]}]作[{'横向' if p[2]=='H' else '纵向'}]拼接\n"
            return {"status": "pending", "msg": prompt}

# ================= 本地运行测试 =================
if __name__ == "__main__":
    import os
    from PIL import Image
    
    # 请修改为实际本地数据库路径
    db_path = r"D:\ALin-Data\AstrBot-plugins\poetry_data.db" 
    
    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在，请检查路径: {db_path}")
    else:
        # 为了每次测试都能看到随机开局的效果，我们在本地测试前删掉旧的存档
        save_file = "./saves/game_local_test_crossword.json"
        if os.path.exists(save_file):
            os.remove(save_file)
            
        engine = PoetryCrosswordEngine(session_id="local_test_crossword", db_source=db_path, save_dir="./saves")
        print("纵横飞花令 本地测试引擎启动。")
        
        print(engine.step("join", "u1", "阿麟")["msg"])
        print(engine.step("join", "u2", "测试员张三")["msg"])
        print("\n提示：系统已自动安排两人加入并随机开局。输入 'q' 退出，输入 'report' 查看战报。")
        
        # 🌟 修复点 1：必须先调用 engine.render_image() 实时渲染最新状态，再打开图片
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
                # 🌟 修复点 2：在 report 命令中加入实时渲染并弹图的逻辑
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