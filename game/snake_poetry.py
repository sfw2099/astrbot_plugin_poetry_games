import random
import re
import os
import sqlite3
from PIL import Image, ImageDraw, ImageFont
try:
    from .base_game import BaseGameEngine, BOT_ID, BOT_NAME
except ImportError:
    from base_game import BaseGameEngine, BOT_ID, BOT_NAME

class PoetrySnakeEngine(BaseGameEngine):
    def __init__(self, session_id, db_source, save_dir, width=40, height=40, timeout_seconds=90, save_filename=None):
        super().__init__(session_id, db_source, save_dir, timeout_seconds, save_filename)
        self.WIDTH = width
        self.HEIGHT = height
        self.CELL_SIZE = 28 
        self.BOARD_W_PX = width * self.CELL_SIZE
        self.BOARD_H_PX = height * self.CELL_SIZE
        
        self.COLOR_BG = '#F8F9FA'               
        self.COLOR_GRID_LINE = '#E0E0E0'        
        self.COLOR_TEXT = '#333333'             
        
        self.COLOR_HIGHLIGHT_FILL = '#FFD700'   
        self.COLOR_HIGHLIGHT_TEXT = '#E53935'   
        self.COLOR_AOE_FILL = '#FFF9C4'         
        
        self.COLOR_FOOD_BG = '#4CAF50'          
        self.COLOR_FOOD_TEXT = '#FFFFFF'        
        
        self.COLOR_PALETTE = [
            '#FFB3BA', '#FFDFBA', '#BAFFC9', '#BAE1FF', 
            '#E8BAFF', '#C2F0C2', '#957DAD', '#FFD1DC', '#D4F0F0'
        ]

        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.font_path = os.path.join(current_dir, "STZHONGS.TTF") 
        try:
            self.font = ImageFont.truetype(self.font_path, 20)
            self.label_font = ImageFont.truetype(self.font_path, 14) 
        except:
            self.font = ImageFont.load_default()
            self.label_font = ImageFont.load_default()

        if not self.state.get("custom_data"):
            self.state["custom_data"] = {
                "width": width,      
                "height": height,    
                "snakes": {},          
                "foods": [],           
                "food_id_counter": 0,
                "player_colors": {},
                "pending_verse": None,
                "pending_options": [],
                "pending_player_id": None
            }
            if not os.path.exists(self.save_file):
                for _ in range(3):
                    self._spawn_food()
                self.save_state()

        self.render_path = os.path.join(save_dir, f"snake_cache_{session_id}.png")

    def load_state(self):
        success = super().load_state()
        if success:
            custom = self.state.get("custom_data", {})
            self.WIDTH = custom.get("width", 40)
            self.HEIGHT = custom.get("height", 40)
            self.BOARD_W_PX = self.WIDTH * self.CELL_SIZE
            self.BOARD_H_PX = self.HEIGHT * self.CELL_SIZE
        return success

    def _build_rich_grid(self):
        grid = [[None for _ in range(self.WIDTH)] for _ in range(self.HEIGHT)]
        cell_info = [[[] for _ in range(self.WIDTH)] for _ in range(self.HEIGHT)]
        
        for f in self.state["custom_data"]["foods"]:
            grid[f['y']][f['x']] = f['char']
            cell_info[f['y']][f['x']].append({"type": "food", "food_idx": f["id"]})
            
        for pid, s_data in self.state["custom_data"]["snakes"].items():
            if s_data.get("dead"): continue
            
            expire_age = max(0, s_data.get("life", 3) - 1)
            
            for seg_idx, seg in enumerate(s_data["segments"]):
                if "active_chars" not in seg:
                    seg["active_chars"] = [True] * len(seg["verse"])
                    
                age = s_data.get("turns_played", 0) - seg.get("turn_placed", 0)
                is_expiring = (age + 1 >= expire_age)
                
                for i, c in enumerate(seg['verse']):
                    if not seg["active_chars"][i]: continue
                    
                    cx = seg['x'] + (i if seg['dir'] == 'H' else 0)
                    cy = seg['y'] + (0 if seg['dir'] == 'H' else i)
                    grid[cy][cx] = c
                    cell_info[cy][cx].append({"type": "snake", "pid": pid, "seg_idx": seg_idx, "char_idx": i, "is_expiring": is_expiring})
                    
        return grid, cell_info

    def _spawn_food(self):
        grid, cell_info = self._build_rich_grid()
        
        forbidden = set()
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                if any(i['type'] == 'snake' for i in cell_info[y][x]):
                    for dx in [-1, 0, 1]:
                        for dy in [-1, 0, 1]:
                            nx, ny = x + dx, y + dy
                            if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                                forbidden.add((nx, ny))
                                
        empty = [(x, y) for y in range(self.HEIGHT) for x in range(self.WIDTH) if grid[y][x] is None and (x, y) not in forbidden]
        
        if not empty:
            empty = [(x, y) for y in range(self.HEIGHT) for x in range(self.WIDTH) if grid[y][x] is None]
            if not empty: return
        
        top_chars_pool = list("不人一风山无有天云来日何花春中年如生月时水自上为心我相此清长知秋江君雨未归得白子是千今三见行里空去明") 
        char = random.choice(top_chars_pool)
            
        fx, fy = random.choice(empty)
        fid = self.state["custom_data"].get("food_id_counter", 0)
        self.state["custom_data"]["food_id_counter"] = fid + 1
        self.state["custom_data"]["foods"].append({"id": fid, "char": char, "x": fx, "y": fy})
   
    def _get_player_color(self, player_id):
        colors = self.state["custom_data"]["player_colors"]
        if player_id not in colors:
            used_colors = set(colors.values())
            available_colors = [c for c in self.COLOR_PALETTE if c not in used_colors]
            colors[player_id] = random.choice(available_colors) if available_colors else "#{:06x}".format(random.randint(0, 0xFFFFFF))
        return colors[player_id]

    def _get_lightened_color(self, hex_color, factor=0.6):
        try:
            hex_color = hex_color.lstrip('#')
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            r = int(r + (255 - r) * factor)
            g = int(g + (255 - g) * factor)
            b = int(b + (255 - b) * factor)
            return f'#{r:02x}{g:02x}{b:02x}'
        except:
            return '#E8F5E9'

    def _calculate_territory_scores(self):
        for p in self.state["players"]:
            pid = p['id']
            if pid in self.state["custom_data"]["snakes"]:
                p['score'] = self.state["custom_data"]["snakes"][pid].get("life", 0)

    def _is_valid(self, sx, sy, direction, verse, pid, grid, cell_info):
        eaten = set()
        hit_head = False
        new_to_me = 0
        
        snakes = self.state["custom_data"]["snakes"]
        is_new = True
        head_s_idx = -1
        
        if pid in snakes and not snakes[pid].get("dead"):
            for s_idx in range(len(snakes[pid]["segments"])-1, -1, -1):
                if any(snakes[pid]["segments"][s_idx].get("active_chars", [True]*len(snakes[pid]["segments"][s_idx]["verse"]))):
                    head_s_idx = s_idx
                    is_new = False
                    break
        
        for i, char in enumerate(verse):
            cx = sx + (i if direction == 'H' else 0)
            cy = sy + (0 if direction == 'H' else i)
            
            if cx < 0 or cx >= self.WIDTH or cy < 0 or cy >= self.HEIGHT: return False
            
            if grid[cy][cx] is not None and grid[cy][cx] != char: 
                return False
            
            is_my_body = False
            my_seg_indices_here = []
            
            if grid[cy][cx] is not None:
                for info in cell_info[cy][cx]:
                    if info['type'] == 'snake' and info['pid'] == pid:
                        is_my_body = True
                        my_seg_indices_here.append(info['seg_idx'])
                        
            if not is_my_body: new_to_me += 1
            
            if is_my_body:
                if is_new:
                    return False 
                else:
                    if head_s_idx not in my_seg_indices_here:
                        return False
                    else:
                        hit_head = True
            
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                        for info in cell_info[ny][nx]:
                            if info['type'] == 'food': eaten.add(info['food_idx'])
                
        if new_to_me == 0: return False 
        
        if is_new:
            if len(eaten) == 0: return False
        else:
            if not hit_head: return False
            
        return True

    def _execute_placement(self, verse, start_x, start_y, direction, player_id, player_name):
        grid, cell_info = self._build_rich_grid()
        eaten_foods = set()
        hit_enemy_chars = {}
        overlap_cells = set()
        
        for i, char in enumerate(verse):
            cx = start_x + (i if direction == 'H' else 0)
            cy = start_y + (0 if direction == 'H' else i)
            
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                        for info in cell_info[ny][nx]:
                            if info['type'] == 'food':
                                eaten_foods.add(info['food_idx'])
                                if dx == 0 and dy == 0: overlap_cells.add((nx, ny))
                            elif info['type'] == 'snake' and info['pid'] != player_id:
                                if info['pid'] not in hit_enemy_chars:
                                    hit_enemy_chars[info['pid']] = set()
                                hit_enemy_chars[info['pid']].add((info['seg_idx'], info['char_idx']))
                                if dx == 0 and dy == 0: overlap_cells.add((nx, ny))
                                
        overlap_count = len(overlap_cells)
        
        snakes = self.state["custom_data"]["snakes"]
        if player_id not in snakes:
            snakes[player_id] = {"life": 3, "overlap_count": 0, "turns_played": 0, "segments": [], "dead": False}
        elif snakes[player_id].get("dead"):
            snakes[player_id]["dead"] = False
            snakes[player_id]["life"] = 3
            snakes[player_id]["segments"] = []
            
        my_snake = snakes[player_id]
        if "life" not in my_snake:
            my_snake["life"] = my_snake.get("max_len", 3)
            my_snake["overlap_count"] = 0
            my_snake["turns_played"] = self.state.get("turn_count", 0)
            
        my_snake["overlap_count"] += overlap_count
        
        cut_details = []
        total_stolen_life = 0
        
        for opid, chars_hit in hit_enemy_chars.items():
            if opid not in snakes or snakes[opid].get("dead"): continue
            
            op_snake = snakes[opid]
            hit_count = 0
            for s_idx, c_idx in chars_hit:
                if op_snake["segments"][s_idx].get("active_chars", [True])[c_idx]:
                    op_snake["segments"][s_idx]["active_chars"][c_idx] = False
                    hit_count += 1
                    
            if hit_count == 0: continue
            
            active_nodes = {}
            for s_idx, seg in enumerate(op_snake["segments"]):
                if "active_chars" not in seg: seg["active_chars"] = [True]*len(seg["verse"])
                for c_idx, active in enumerate(seg["active_chars"]):
                    if active:
                        cx = seg['x'] + (c_idx if seg['dir'] == 'H' else 0)
                        cy = seg['y'] + (0 if seg['dir'] == 'H' else c_idx)
                        if (cx, cy) not in active_nodes:
                            active_nodes[(cx, cy)] = []
                        active_nodes[(cx, cy)].append((s_idx, c_idx))
            
            components = []
            visited = set()
            for node in active_nodes:
                if node not in visited:
                    comp = []
                    queue = [node]
                    visited.add(node)
                    while queue:
                        curr = queue.pop(0)
                        comp.append(curr)
                        cx, cy = curr
                        for dx, dy in [(-1,0), (1,0), (0,-1), (0,1)]:
                            nbr = (cx+dx, cy+dy)
                            if nbr in active_nodes and nbr not in visited:
                                visited.add(nbr)
                                queue.append(nbr)
                    components.append(comp)
            
            discarded_comps = 0
            if not components:
                op_snake["dead"] = True
                discarded_comps = 0
            elif len(components) > 1:
                max_size = -1
                best_latest_s_idx = -1
                largest_comp = []
                
                for comp in components:
                    size = len(comp)
                    latest_s_idx = max(max(s for s, c in active_nodes[n]) for n in comp)
                    if size > max_size or (size == max_size and latest_s_idx > best_latest_s_idx):
                        max_size = size
                        best_latest_s_idx = latest_s_idx
                        largest_comp = comp
                        
                largest_comp_set = set(largest_comp)
                for node, indices in active_nodes.items():
                    if node not in largest_comp_set:
                        for s_idx, c_idx in indices:
                            op_snake["segments"][s_idx]["active_chars"][c_idx] = False
                        
                discarded_comps = len(components) - 1
                
            damage = discarded_comps
            op_snake["life"] = max(0, op_snake.get("life", 3) - damage)
            total_stolen_life += damage
            
            op_name = next((p["name"] for p in self.state["players"] if p["id"] == opid), "未知玩家")
            cut_details.append({"name": op_name, "damage": damage, "dead": op_snake["dead"], "discarded_comps": discarded_comps})

        foods = self.state["custom_data"]["foods"]
        eaten_count = len(eaten_foods)
        
        for fid in eaten_foods:
            for i in range(len(foods)-1, -1, -1):
                if foods[i]["id"] == fid:
                    foods.pop(i)
                    
        my_snake["life"] += eaten_count + total_stolen_life
        my_snake["turns_played"] += 1
        
        my_snake["segments"].append({
            "verse": verse, "x": start_x, "y": start_y, "dir": direction, 
            "turn_placed": my_snake["turns_played"],
            "active_chars": [True]*len(verse)
        })
        
        for seg in my_snake["segments"]:
            if "turn_placed" not in seg: seg["turn_placed"] = my_snake["turns_played"]
            age = my_snake["turns_played"] - seg["turn_placed"]
            expire_age = max(0, my_snake["life"] - 1)
            if age >= expire_age:
                seg["active_chars"] = [False] * len(seg["verse"])
        
        while len(self.state["custom_data"]["foods"]) < 3:
            self._spawn_food()
            
        self._get_player_color(player_id)
        return eaten_count, overlap_count, cut_details

    def render_image(self, pending_options=None, pending_verse=None):
        image = Image.new('RGB', (self.BOARD_W_PX, self.BOARD_H_PX), color=self.COLOR_BG)
        draw = ImageDraw.Draw(image)
        grid, cell_info = self._build_rich_grid()
        
        path_highlights = set()
        aoe_highlights = set()
        labels_map = {}
        
        if pending_options and pending_verse:
            L = len(pending_verse)
            for i, opt in enumerate(pending_options):
                sx, sy, direction = opt['start_x'], opt['start_y'], opt['dir']
                label_str = str(i + 1)
                
                for k in range(L):
                    cx = sx + (k if direction == 'H' else 0)
                    cy = sy + (0 if direction == 'H' else k)
                    path_highlights.add((cx, cy))
                    
                    for dx in [-1, 0, 1]:
                        for dy in [-1, 0, 1]:
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                                aoe_highlights.add((nx, ny))
                    
                    if k == 0 or k == L - 1:
                        if (cx, cy) not in labels_map: labels_map[(cx, cy)] = []
                        if label_str not in labels_map[(cx, cy)]: labels_map[(cx, cy)].append(label_str)
                        
            aoe_highlights -= path_highlights

        permanent_aoe = {}
        snakes = self.state["custom_data"].get("snakes", {})
        for pid, s_data in snakes.items():
            if s_data.get("dead"): continue
            head_seg_idx = -1
            for s_idx in range(len(s_data["segments"])-1, -1, -1):
                if any(s_data["segments"][s_idx].get("active_chars", [True]*len(s_data["segments"][s_idx]["verse"]))):
                    head_seg_idx = s_idx
                    break
            
            if head_seg_idx != -1:
                seg = s_data["segments"][head_seg_idx]
                for i, c in enumerate(seg['verse']):
                    if seg.get("active_chars", [True]*len(seg['verse']))[i]:
                        cx = seg['x'] + (i if seg['dir'] == 'H' else 0)
                        cy = seg['y'] + (0 if seg['dir'] == 'H' else i)
                        for dx in [-1, 0, 1]:
                            for dy in [-1, 0, 1]:
                                nx, ny = cx + dx, cy + dy
                                if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                                    permanent_aoe[(nx, ny)] = pid

        for i in range(self.WIDTH + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(line_pos, 0), (line_pos, self.BOARD_H_PX)], fill=self.COLOR_GRID_LINE, width=1)
        for i in range(self.HEIGHT + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(0, line_pos), (self.BOARD_W_PX, line_pos)], fill=self.COLOR_GRID_LINE, width=1)
            
        pending_pid = self.state["custom_data"].get("pending_player_id")
        
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                is_path = (x, y) in path_highlights
                is_aoe = (x, y) in aoe_highlights
                is_perm_aoe = (x, y) in permanent_aoe
                cell_labels = labels_map.get((x, y))
                
                has_food = any(i['type'] == 'food' for i in cell_info[y][x])
                snake_pids = [i['pid'] for i in cell_info[y][x] if i['type'] == 'snake']
                has_enemy = any(pid != pending_pid for pid in snake_pids) if pending_pid else False
                
                rect_coords = [x*self.CELL_SIZE+1, y*self.CELL_SIZE+1, (x+1)*self.CELL_SIZE-1, (y+1)*self.CELL_SIZE-1]
                
                if is_path:
                    draw.rectangle(rect_coords, fill=self.COLOR_HIGHLIGHT_FILL)
                elif has_food:
                    draw.rectangle(rect_coords, fill=self.COLOR_FOOD_BG)
                    if is_aoe: draw.rectangle(rect_coords, outline='#E53935', width=2)
                elif snake_pids:
                    top_snake = [i for i in cell_info[y][x] if i['type'] == 'snake'][-1]
                    pid = top_snake['pid']
                    
                    if top_snake.get('is_expiring'):
                        draw.rectangle(rect_coords, fill=self._get_lightened_color(self._get_player_color(pid), 0.6))
                    else:
                        draw.rectangle(rect_coords, fill=self._get_player_color(pid))
                        
                    if is_aoe and has_enemy: draw.rectangle(rect_coords, outline='#E53935', width=2)
                elif is_aoe:
                    p_color = self._get_lightened_color(self._get_player_color(pending_pid), 0.5) if pending_pid else self.COLOR_AOE_FILL
                    draw.rectangle(rect_coords, fill=p_color)
                elif is_perm_aoe:
                    perm_pid = permanent_aoe[(x, y)]
                    aoe_color = self._get_lightened_color(self._get_player_color(perm_pid), 0.6)
                    draw.rectangle(rect_coords, fill=aoe_color)
                    
                if cell_labels:
                    label = "/".join(cell_labels)
                    f_use = self.label_font if len(label) > 1 else self.font
                    bbox = draw.textbbox((0, 0), label, font=f_use)
                    draw.text((x*self.CELL_SIZE+(self.CELL_SIZE-(bbox[2]-bbox[0]))/2, y*self.CELL_SIZE+(self.CELL_SIZE-(bbox[3]-bbox[1]))/2-4), label, fill=self.COLOR_HIGHLIGHT_TEXT, font=f_use)
                elif grid[y][x] is not None:
                    txt_color = self.COLOR_FOOD_TEXT if (has_food and not is_path) else self.COLOR_TEXT
                    bbox = draw.textbbox((0, 0), grid[y][x], font=self.font)
                    draw.text((x*self.CELL_SIZE+(self.CELL_SIZE-(bbox[2]-bbox[0]))/2, y*self.CELL_SIZE+(self.CELL_SIZE-(bbox[3]-bbox[1]))/2-4), grid[y][x], fill=txt_color, font=self.font)
        
        image.save(self.render_path)
        return self.render_path

    def _finalize_success_turn(self, user_name, verse, eaten_count=0, overlap_count=0, cut_details=None):
        self._calculate_territory_scores()
        self.state["turn_count"] += 1
        
        dead_names = []
        dead_pids = set()
        snakes = self.state["custom_data"].get("snakes", {})
        
        for pid, s_data in snakes.items():
            if not s_data.get("dead"):
                has_active = False
                for seg in s_data.get("segments", []):
                    if any(seg.get("active_chars", [True]*len(seg["verse"]))):
                        has_active = True
                        break
                if not has_active:
                    s_data["dead"] = True
                    dead_pids.add(pid)
                    
        if self.state["players"]:
            old_curr_idx = self.state["current_turn"]
            found_next = False
            
            for offset in range(1, len(self.state["players"]) + 1):
                idx = (old_curr_idx + offset) % len(self.state["players"])
                candidate_pid = self.state["players"][idx]["id"]
                if candidate_pid not in dead_pids:
                    for p in self.state["players"]:
                        if p["id"] in dead_pids:
                            dead_names.append(p["name"])
                            
                    new_players = [p for p in self.state["players"] if p["id"] not in dead_pids]
                    self.state["players"] = new_players
                    
                    for i, p in enumerate(self.state["players"]):
                        if p["id"] == candidate_pid:
                            self.state["current_turn"] = i
                            break
                    found_next = True
                    break
                    
            if not found_next:
                for p in self.state["players"]:
                    if p["id"] in dead_pids:
                        dead_names.append(p["name"])
                self.state["players"] = []
                self.state["current_turn"] = 0
                
        self.save_state()
        
        msg = f"[{user_name}] 落子成功！\n诗句：{verse}\n"
        if eaten_count > 0: 
            msg += f"吞噬奖励：吃掉 {eaten_count} 个独立字，寿命 +{eaten_count}！\n"
        if overlap_count > 0:
            msg += f"完美重合：精准覆盖了 {overlap_count} 个字！\n"
            
        if cut_details:
            for cut in cut_details:
                if cut["dead"]:
                    msg += f"致命打击！彻底消灭了 [{cut['name']}] 的残存蛇身，夺取 {cut['damage']} 寿命！\n"
                elif cut["damage"] > 0:
                    msg += f"刀光剑影！斩断并清除了 [{cut['name']}] 的碎片，夺取 {cut['damage']} 寿命！\n"
                else:
                    msg += f"擦肩而过！削掉了 [{cut['name']}] 的局部文字！\n"
                    
        if dead_names:
            msg += f"寿终正寝或无力回天！以下玩家的蛇身已完全消散，自动淘汰出局：{', '.join(dead_names)}\n"
            
        if not self.state["players"]:
            msg += f"\n游戏结束！当前已无存活玩家。"
            return {"status": "success", "msg": msg, "image": self.render_image()}
            
        curr_life = next((p['score'] for p in self.state['players'] if p['name'] == user_name), 0)
        if user_name not in dead_names:
            msg += f"当前寿命(存在轮次)：{curr_life}\n"
            
        msg += f"{'-' * 15}\n下一位：[{self.state['players'][self.state['current_turn']]['name']}]"
        return {"status": "success", "msg": msg, "image": self.render_image()}

    def bot_play(self):
        """蛇形飞花令 Bot：找含食物字符的诗句"""
        import sqlite3
        custom = self.state["custom_data"]
        foods = custom.get("foods", [])
        if not foods:
            self.next_turn(); self.save_state()
            return {"msg": "🤖 [诗词AI] 棋盘无食物，弃权。"}

        db_path = self.db_source if isinstance(self.db_source, str) else getattr(self.db_source, 'db_path', None)
        if not db_path:
            self.next_turn(); self.save_state()
            return {"msg": "🤖 [诗词AI] 数据库不可用，弃权。"}

        food_chars = list(set(f['char'] for f in foods))[:5]
        candidates = []
        try:
            with sqlite3.connect(db_path) as conn:
                for ch in food_chars:
                    cursor = conn.cursor()
                    cursor.execute("SELECT content FROM poems WHERE content LIKE ? LIMIT 15", (f'%{ch}%',))
                    for row in cursor.fetchall():
                        for sent in re.split(r'[，。！？\n\r\s、；：]+', row[0]):
                            pure = re.sub(r'[^\u4e00-\u9fa5]', '', sent)
                            if len(pure) < 3: continue
                            if ch not in pure: continue
                            if len(candidates) > 20: break
                            for f in foods:
                                if f['char'] in pure:
                                    for idx, c in enumerate(pure):
                                        if c == f['char']:
                                            for d in ['H', 'V']:
                                                sx = f['x'] - (idx if d == 'H' else 0)
                                                sy = f['y'] - (0 if d == 'H' else idx)
                                                try:
                                                    grid, ci = self._build_rich_grid()
                                                    if self._is_valid(sx, sy, d, pure, BOT_ID, grid, ci):
                                                        candidates.append(pure)
                                                        if len(candidates) > 20: break
                                                except:
                                                    pass
        except Exception as e:
            pass

        if candidates:
            best = random.choice(candidates)
            return self.step("play", BOT_ID, BOT_NAME, best)

        self.next_turn(); self.save_state()
        return {"msg": "🤖 [诗词AI] 找不到可用的诗句，弃权。"}

    def step(self, action_type, user_id, user_name, payload=""):
        self.update_activity()
        user_id = str(user_id)
        
        if action_type == "join": return self.process_join(user_id, user_name)
        if action_type == "quit": 
            custom = self.state.get("custom_data", {})
            if custom.get("pending_options") and custom.get("pending_player_id") == str(user_id):
                custom["pending_options"] = []
                custom["pending_verse"] = None
                custom["pending_player_id"] = None
                
            # 核心新增：玩家退出时，其蛇身瞬间死亡并完全消散
            if str(user_id) in custom.get("snakes", {}):
                custom["snakes"][str(user_id)]["dead"] = True
                custom["snakes"][str(user_id)]["segments"] = []
                custom["snakes"][str(user_id)]["life"] = 0
                
            return super().process_quit(user_id, user_name)
            
        if not self.state["players"]: return {"status": "ignore"}
        
        current_p = self.state["players"][self.state["current_turn"]]
        custom = self.state["custom_data"]

        if custom.get("pending_options"):
            if user_id != custom["pending_player_id"]: return {"status": "ignore"}
            if payload.strip().isdigit():
                idx = int(payload.strip()) - 1
                if 0 <= idx < len(custom["pending_options"]):
                    opt = custom["pending_options"][idx]
                    eats, overlaps, cuts = self._execute_placement(custom["pending_verse"], opt['start_x'], opt['start_y'], opt['dir'], user_id, user_name)
                    verse_cache = custom["pending_verse"]
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                    custom["pending_player_id"] = None
                    return self._finalize_success_turn(user_name, verse_cache, eats, overlaps, cuts)

            img = self.render_image(pending_options=custom["pending_options"], pending_verse=custom["pending_verse"])
            return {"status": "success", "msg": "选择无效。请直接发送图片中首尾的数字。", "image": img}

        if action_type == "skip":
            if len(self.state["players"]) <= 1:
                return {"status": "error", "msg": "当前只有你一个人在玩，没法跳过哦！"}
            import time
            timeout_limit = self.get_timeout()
            if time.time() - self.last_active_time < timeout_limit:
                rem = int(timeout_limit - (time.time() - self.last_active_time))
                return {"status": "error", "msg": f"还没到超时时间，请再给 TA {rem} 秒吧！"}
            curr_p = self.state["players"][self.state["current_turn"]]
            if custom.get("pending_options") and custom["pending_player_id"] == curr_p["id"]:
                custom["pending_options"] = []
                custom["pending_verse"] = None
                custom["pending_player_id"] = None
            self.next_turn()
            self.update_activity()
            self.save_state()
            next_p = self.state["players"][self.state["current_turn"]]
            return {"status": "success", "msg": f"已强制跳过超时的 [{curr_p['name']}]！\n下一位：[{next_p['name']}]"}

        if user_id != current_p['id']: return {"status": "ignore"}

        verse = re.sub(r'[^\u4e00-\u9fa5]', '', payload.strip())
        if not verse: return {"status": "ignore"}
        if len(verse) < 3: return {"status": "error", "msg": "诗句太短！"}
        
        snakes = custom.get("snakes", {})
        is_new = (user_id not in snakes or snakes[user_id].get("dead") or len(snakes[user_id]["segments"]) == 0)
        
        if not is_new:
            last_verse = snakes[user_id]["segments"][-1]["verse"]
            if verse == last_verse:
                return {"status": "error", "msg": "移动失败！不能使用和上一句完全一样的诗句原地踏步。"}
                
        if not self._check_db(verse): return {"status": "error", "msg": "库中未查到该诗句！"}

        valid_placements = []
        seen = set()
        grid, cell_info = self._build_rich_grid()

        if is_new:
            for f in custom["foods"]:
                if f['char'] in verse:
                    for idx, c in enumerate(verse):
                        if c == f['char']:
                            for d in ['H', 'V']:
                                sx = f['x'] - (idx if d == 'H' else 0)
                                sy = f['y'] - (0 if d == 'H' else idx)
                                if (sx, sy, d) not in seen and self._is_valid(sx, sy, d, verse, user_id, grid, cell_info):
                                    seen.add((sx, sy, d))
                                    valid_placements.append({'start_x': sx, 'start_y': sy, 'dir': d})
        else:
            head_s_idx = -1
            for s_idx in range(len(snakes[user_id]["segments"])-1, -1, -1):
                if any(snakes[user_id]["segments"][s_idx].get("active_chars", [True]*len(snakes[user_id]["segments"][s_idx]["verse"]))):
                    head_s_idx = s_idx
                    break
                    
            if head_s_idx != -1:
                head_seg = snakes[user_id]["segments"][head_s_idx]
                for i, sc in enumerate(head_seg['verse']):
                    if head_seg.get("active_chars", [True]*len(head_seg['verse']))[i] and sc in verse:
                        cx = head_seg['x'] + (i if head_seg['dir'] == 'H' else 0)
                        cy = head_seg['y'] + (0 if head_seg['dir'] == 'H' else i)
                        for idx, c in enumerate(verse):
                            if c == sc:
                                for d in ['H', 'V']:
                                    sx = cx - (idx if d == 'H' else 0)
                                    sy = cy - (0 if d == 'H' else idx)
                                    if (sx, sy, d) not in seen and self._is_valid(sx, sy, d, verse, user_id, grid, cell_info):
                                        seen.add((sx, sy, d))
                                        valid_placements.append({'start_x': sx, 'start_y': sy, 'dir': d})

        if not valid_placements:
            err = "出生失败！必须从绿色的独立字开始接龙。" if is_new else "移动失败！必须与自己存活的最后一句诗产生交叉，且路线上不能有其他字。"
            return {"status": "error", "msg": err}
            
        elif len(valid_placements) == 1:
            opt = valid_placements[0]
            eats, overlaps, cuts = self._execute_placement(verse, opt['start_x'], opt['start_y'], opt['dir'], user_id, user_name)
            return self._finalize_success_turn(user_name, verse, eats, overlaps, cuts)
        else:
            custom["pending_verse"] = verse
            custom["pending_player_id"] = user_id
            custom["pending_options"] = valid_placements
            img = self.render_image(pending_options=valid_placements, pending_verse=verse)
            return {"status": "pending", "msg": f"发现 {len(valid_placements)} 条可行路线！\n请发送首尾对应的【数字】进行落子。", "image": img}

# ================= 本地运行测试 =================
if __name__ == "__main__":
    import shutil
    
    db_path = r"D:\ALin-Data\AstrBot-plugins\poetry_data.db" 
    save_file = "./saves/game_local_test_snake.json"
    if os.path.exists(save_file): os.remove(save_file)
        
    engine = PoetrySnakeEngine(session_id="local_test_snake", db_source=db_path, save_dir="./saves")
    print("诗词贪吃蛇 本地多玩家测试启动！")
    print("提示：输入【/add 玩家名】可以随时让新玩家加入游戏（例如：/add 小黑）")
    print("提示：输入【跳过】可以测试回合跳过，输入【q】退出测试。\n" + "="*40)
    
    print(engine.step("join", "u1", "阿麟")["msg"])
    
    output_image_name = "test_board_current.png"
    shutil.copy(engine.render_image(), output_image_name)
    print(f"初始棋盘已保存至当前目录: {output_image_name}")
    print(f"请用看图软件打开该图片并放在一旁，每次落子后它会自动刷新！")
    
    mock_uid_counter = 2
    
    while True:
        if not engine.state["players"]:
            print("当前游戏已无玩家。")
            break
            
        curr_player = engine.state["players"][engine.state["current_turn"]]
        active_id = engine.state["custom_data"].get("pending_player_id") or curr_player["id"]
        active_name = next(p["name"] for p in engine.state["players"] if p["id"] == active_id)
        
        user_text = input(f"\n[{active_name}] > ").strip()
        
        if user_text.lower() == 'q': break
            
        if user_text.startswith("/add "):
            new_name = user_text[5:].strip()
            if new_name:
                new_uid = f"u{mock_uid_counter}"
                mock_uid_counter += 1
                res = engine.step("join", new_uid, new_name)
                if res.get("msg"): print(f"\n[系统播报]\n{res['msg']}")
            continue
            
        if user_text == "跳过":
            response = engine.step("skip", active_id, active_name)
        else:
            response = engine.step("play", active_id, active_name, user_text)
            
        if response.get("msg"): print(f"\n[系统]\n{response['msg']}")
        
        if "image" in response: 
            try:
                shutil.copy(response["image"], output_image_name)
                print(f"(图片已在后台更新，请查看看图软件)")
            except Exception as e:
                print(f"(图片保存失败: {e})")