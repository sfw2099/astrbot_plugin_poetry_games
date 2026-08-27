import sqlite3
import re

class PoetryDB:
    def __init__(self, db_path):
        self.db_path = db_path

    def search_by_sentence(self, sentence):
        """同时返回精准匹配和模糊匹配的结果"""
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if not clean_text: return {"exact": [], "fuzzy": []}
        
        exact_matches = []
        fuzzy_matches = []
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 粗筛：从数据库中取出最多 50 条包含这串字符的诗，防止遍历太久
            query = "SELECT title, author, dynasty, content FROM poems WHERE content LIKE ? LIMIT 50"
            cursor.execute(query, (f'%{clean_text}%',))
            results = cursor.fetchall()
            
            for title, author, dynasty, content in results:
                # 按标点符号切分成单句数组
                sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                pure_sentences = [re.sub(r'[^\u4e00-\u9fa5]', '', s) for s in sentences if s]
                
                # 分流：如果正好是一句完整的诗，放入精准池；否则放入模糊池
                if clean_text in pure_sentences:
                    if len(exact_matches) < 5:  # 精确匹配最多展示 5 条
                        exact_matches.append((title, author, dynasty))
                else:
                    if len(fuzzy_matches) < 5:  # 模糊匹配最多展示 5 条
                        fuzzy_matches.append((title, author, dynasty))
                        
        return {"exact": exact_matches, "fuzzy": fuzzy_matches}

    def get_poem_by_title(self, title_kw, author_kw=""):
        """支持带作者的联合检索"""
        clean_title = title_kw.strip()
        clean_author = author_kw.strip()
        if not clean_title: return []
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if clean_author:
                query = """
                    SELECT title, author, dynasty, content 
                    FROM poems 
                    WHERE title LIKE ? AND author LIKE ?
                    LIMIT 10
                """
                cursor.execute(query, (f'%{clean_title}%', f'%{clean_author}%'))
            else:
                query = """
                    SELECT title, author, dynasty, content 
                    FROM poems 
                    WHERE title LIKE ? 
                    ORDER BY (CASE WHEN title = ? THEN 0 ELSE 1 END), title ASC
                    LIMIT 10
                """
                cursor.execute(query, (f'%{clean_title}%', clean_title))
            results = cursor.fetchall()
        return results

    def check_exact_poetry(self, sentence):
        """游戏引擎专用的精准查诗接口"""
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if len(clean_text) < 3: return None
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = "SELECT title, author, dynasty, content FROM poems WHERE content LIKE ?"
            cursor.execute(query, (f'%{clean_text}%',))
            rows = cursor.fetchall()
            
            for title, author, dynasty, content in rows:
                sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                pure_sentences = [re.sub(r'[^\u4e00-\u9fa5]', '', s) for s in sentences if s]
                if clean_text in pure_sentences:
                    return (title, author, dynasty)
        return None

    def is_complete_sentence(self, text):
        """判断输入是否为数据库中的完整诗句（合规校验用）。"""
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', text)
        if len(clean_text) < 2:
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT content FROM poems WHERE content LIKE ? LIMIT 300", (f'%{clean_text}%',))
                for (content,) in cursor.fetchall():
                    sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                    pure_sentences = [re.sub(r'[^\u4e00-\u9fa5]', '', s) for s in sentences if s]
                    if clean_text in pure_sentences:
                        return True
        except Exception:
            return False
        return False

    def get_random_verse(self, min_len=5, max_len=10, target_count=8, max_scan=200):
        """随机抽取若干条指定长度范围的完整诗句，返回 [(句子, 标题, 作者, 朝代)]"""
        import random
        candidates = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 用 rowid 随机抽样，避免 ORDER BY RANDOM() 全表排序
            cursor.execute("SELECT COUNT(*) FROM poems")
            total = cursor.fetchone()[0]
            if not total:
                return candidates
            tried = 0
            while len(candidates) < target_count and tried < max_scan:
                tried += 1
                r = random.randint(1, total)
                # rowid >= 随机值取下一行：避免 OFFSET 大表全扫，接近 O(1)
                cursor.execute("SELECT title, author, dynasty, content FROM poems WHERE rowid >= ? LIMIT 1", (r,))
                row = cursor.fetchone()
                if not row:
                    continue
                title, author, dynasty, content = row
                sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                for s in sentences:
                    pure = re.sub(r'[^\u4e00-\u9fa5]', '', s)
                    if min_len <= len(pure) <= max_len:
                        candidates.append((pure, title, author, dynasty))
                        if len(candidates) >= target_count:
                            break
        return candidates