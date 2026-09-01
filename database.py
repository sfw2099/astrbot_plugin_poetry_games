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

    def _split_clauses(self, content):
        """按标点切分诗句为分句列表，保留每个分句的纯汉字与其后的分隔符。"""
        # 用正则找分隔符位置
        clauses = []
        last = 0
        for m in re.finditer(r'[，。！？、；：]', content):
            part = content[last:m.start()]
            h = re.sub(r'[^\u4e00-\u9fa5]', '', part)
            if h:
                clauses.append((h, m.group()))
            last = m.end()
        tail = content[last:]
        h = re.sub(r'[^\u4e00-\u9fa5]', '', tail)
        if h:
            clauses.append((h, ""))
        return clauses

    def get_random_verse_by_combo(self, a_len, b_len, target_count=3, max_scan=800):
        """随机抽取「相邻 a字+b字 两句」的诗句。
        返回 [(combined_text, title, author, dynasty)]，combined_text 保留原分隔符（如「离离原上草，一岁一枯荣」）。
        """
        import random
        candidates = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM poems")
            total = cursor.fetchone()[0]
            if not total:
                return candidates
            tried = 0
            while len(candidates) < target_count and tried < max_scan:
                tried += 1
                r = random.randint(1, total)
                cursor.execute("SELECT title, author, dynasty, content FROM poems WHERE rowid >= ? LIMIT 1", (r,))
                row = cursor.fetchone()
                if not row:
                    continue
                title, author, dynasty, content = row
                clauses = self._split_clauses(content)
                for i in range(len(clauses) - 1):
                    ha, pa = clauses[i]
                    hb, pb = clauses[i + 1]
                    if len(ha) == a_len and len(hb) == b_len:
                        combined = f"{ha}{pa}{hb}"
                        candidates.append((combined, title, author, dynasty))
                        if len(candidates) >= target_count:
                            break
        return candidates

    def is_adjacent_pair(self, a_text, b_text):
        """判断 a_text、b_text 是否为库中某首诗的相邻两个分句。"""
        ca = re.sub(r'[^\u4e00-\u9fa5]', '', a_text)
        cb = re.sub(r'[^\u4e00-\u9fa5]', '', b_text)
        if not ca or not cb:
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT content FROM poems WHERE content LIKE ? LIMIT 100", (f'%{ca}%',))
                for (content,) in cursor.fetchall():
                    clauses = self._split_clauses(content)
                    for i in range(len(clauses) - 1):
                        ha, _ = clauses[i]
                        hb, _ = clauses[i + 1]
                        if ha == ca and hb == cb:
                            return True
        except Exception:
            return False
        return False

    def search_by_chars_and_len(self, include_chars, length, exclude_chars=None, limit=200):
        """按「包含字 + 单句字数」粗筛候选诗句。

        include_chars: 每字都必须在句中出现的字列表（非空）。
        length: 单句字数（拆句后精确匹配）。
        exclude_chars: 排除含这些字的候选（可选）。
        返回 [(句子, title, author, dynasty)]。
        """
        include = [re.sub(r'[^\u4e00-\u9fa5]', '', c) for c in (include_chars or [])]
        include = [c for c in include if c]
        if not include:
            return []
        excl = set(re.sub(r'[^\u4e00-\u9fa5]', '', c) for c in (exclude_chars or []))
        excl.discard('')
        where = []
        params = []
        for c in include:
            where.append("content LIKE ?")
            params.append(f'%{c}%')
        query = "SELECT title, author, dynasty, content FROM poems WHERE " + " AND ".join(where) + " LIMIT 500"
        candidates = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                for title, author, dynasty, content in cursor.fetchall():
                    sentences = re.split(r'[，。！？\n\r\s、；：]+', content)
                    for s in sentences:
                        pure = re.sub(r'[^\u4e00-\u9fa5]', '', s)
                        if length and len(pure) != length:
                            continue
                        if not include or not all(ch in pure for ch in include):
                            continue
                        if excl and any(ch in pure for ch in excl):
                            continue
                        candidates.append((pure, title, author, dynasty))
                        if len(candidates) >= limit:
                            return candidates
        except Exception:
            pass
        return candidates