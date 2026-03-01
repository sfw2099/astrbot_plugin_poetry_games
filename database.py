import sqlite3
import re

class PoetryDB:
    def __init__(self, db_path):
        self.db_path = db_path

    def search_by_sentence(self, sentence):
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if not clean_text: return []
        
        # 使用 with 语句自动管理连接
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = "SELECT title, author, dynasty FROM poems WHERE content LIKE ? LIMIT 10"
            cursor.execute(query, (f'%{clean_text}%',))
            results = cursor.fetchall()
        return results

    def get_poem_by_title(self, title):
        clean_title = title.strip()
        if not clean_title: return []
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
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
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if len(clean_text) < 3: return None
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = "SELECT title, author, dynasty FROM poems WHERE content LIKE ? LIMIT 1"
            cursor.execute(query, (f'%{clean_text}%',))
            res = cursor.fetchone()
        return res
