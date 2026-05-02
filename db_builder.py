#!/usr/bin/env python3
"""
诗词数据库构建器
从 data/ 目录下的 JSON 文件生成 SQLite 数据库。
"""

import json
import sqlite3
import os
import re
from pathlib import Path

def build_database(json_dir: str, db_path: str):
    """
    读取 json_dir 下所有 JSON 文件，构建 SQLite 数据库。

    JSON 格式: [{title, author, dynasty, content, source, version?}, ...]
    数据库表 poems: id, title, author, dynasty, content, version, source
    """
    json_dir = Path(json_dir)
    if not json_dir.exists():
        raise FileNotFoundError(f"数据目录不存在: {json_dir}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 创建表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS poems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '佚名',
            dynasty TEXT NOT NULL DEFAULT '唐',
            content TEXT NOT NULL,
            version INTEGER DEFAULT 0,
            source TEXT DEFAULT ''
        )
    """)

    # 创建 FTS5 全文搜索索引（加速诗句搜索）
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS poems_fts USING fts5(
            title, author, content, content=poems, content_rowid=id
        )
    """)

    # 创建立即填充 FTS 的触发器（增量同步）
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS poems_ai AFTER INSERT ON poems BEGIN
            INSERT INTO poems_fts(rowid, title, author, content)
            VALUES (new.id, new.title, new.author, new.content);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS poems_ad AFTER DELETE ON poems BEGIN
            INSERT INTO poems_fts(poems_fts, rowid, title, author, content)
            VALUES ('delete', old.id, old.title, old.author, old.content);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS poems_au AFTER UPDATE ON poems BEGIN
            INSERT INTO poems_fts(poems_fts, rowid, title, author, content)
            VALUES ('delete', old.id, old.title, old.author, old.content);
            INSERT INTO poems_fts(rowid, title, author, content)
            VALUES (new.id, new.title, new.author, new.content);
        END
    """)

    # 创建常规索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title ON poems(title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_author ON poems(author)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dynasty ON poems(dynasty)")

    conn.commit()

    # 读取所有 JSON 文件
    json_files = sorted(json_dir.glob("*.json"))
    total = 0

    for fpath in json_files:
        print(f"  读取: {fpath.name} ...")
        try:
            data = json.loads(fpath.read_text(encoding='utf-8'))
        except Exception as e:
            print(f"    [SKIP] 解析失败: {e}")
            continue

        batch = []
        for item in data:
            title = item.get("title", "")
            author = item.get("author", "佚名")
            dynasty = item.get("dynasty", "唐")
            content = item.get("content", "")
            version = item.get("version", 0)
            source = item.get("source", "")

            # 清理内容中的空行
            content = re.sub(r'\n{3,}', '\n\n', content).strip()

            if not content or not title:
                continue

            batch.append((title, author, dynasty, content, version, source))

        if batch:
            cursor.executemany(
                "INSERT INTO poems (title, author, dynasty, content, version, source) VALUES (?, ?, ?, ?, ?, ?)",
                batch
            )
            total += len(batch)
            print(f"    插入 {len(batch)} 条")

    # 填充 FTS 索引（建表后才需要）
    cursor.execute("INSERT INTO poems_fts(poems_fts) VALUES ('rebuild')")
    conn.commit()

    # 统计
    cursor.execute("SELECT COUNT(*) FROM poems")
    count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM poems WHERE version > 0")
    version_count = cursor.fetchone()[0]
    db_size = os.path.getsize(db_path) / (1024 * 1024)

    print(f"\n========================================")
    print(f"构建完成: {count} 首诗 ({version_count} 首含多版本)")
    print(f"数据库大小: {db_size:.1f} MB")
    print(f"路径: {db_path}")
    print(f"========================================")

    conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python db_builder.py <json_dir> <db_path>")
        print("示例: python db_builder.py ./data ./poetry_data.db")
        sys.exit(1)

    build_database(sys.argv[1], sys.argv[2])
