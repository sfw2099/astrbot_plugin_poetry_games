import sqlite3
import re

class PoetryDB:
    def __init__(self, db_path):
        self.db_path = db_path

    def search_by_sentence(self, sentence):
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if not clean_text:
            return []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 使用 FTS5 加速全文搜索
            try:
                query = """
                    SELECT p.title, p.author, p.dynasty
                    FROM poems_fts
                    JOIN poems p ON poems_fts.rowid = p.id
                    WHERE poems_fts.content MATCH ?
                    LIMIT 10
                """
                cursor.execute(query, (clean_text,))
            except Exception:
                # FTS5 不可用时回退到 LIKE
                query = "SELECT title, author, dynasty FROM poems WHERE content LIKE ? LIMIT 10"
                cursor.execute(query, (f'%{clean_text}%',))
            results = cursor.fetchall()
        return results

    def get_poem_by_title(self, title):
        clean_title = title.strip()
        if not clean_title:
            return []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = """
                SELECT title, author, dynasty, content, version
                FROM poems
                WHERE title LIKE ?
                ORDER BY (CASE WHEN title = ? THEN 0 ELSE 1 END), title ASC, version ASC
                LIMIT 10
            """
            cursor.execute(query, (f'%{clean_title}%', clean_title))
            results = cursor.fetchall()
        return results

    def check_exact_poetry(self, sentence):
        clean_text = re.sub(r'[^\u4e00-\u9fa5]', '', sentence)
        if len(clean_text) < 3:
            return None

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = "SELECT title, author, dynasty FROM poems WHERE content LIKE ? LIMIT 1"
            cursor.execute(query, (f'%{clean_text}%',))
            res = cursor.fetchone()
        return res
