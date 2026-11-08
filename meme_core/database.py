"""SQLite 数据层：集合、图片、标签、审核状态、心情映射、元信息。"""

import random
import sqlite3
import threading
from datetime import datetime

from .constants import (
    REVIEW_CONFIRMED,
    STANDARD_TAGS,
)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            cur = self.conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS packs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    display_name TEXT,
                    type TEXT DEFAULT 'emotion',
                    enabled INTEGER DEFAULT 0,
                    source_url TEXT,
                    description TEXT,
                    has_native_tag INTEGER DEFAULT 0,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS images(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT,
                    review_status TEXT DEFAULT 'uncategorized',
                    created_at TEXT,
                    FOREIGN KEY(pack_id) REFERENCES packs(id) ON DELETE CASCADE,
                    UNIQUE(pack_id, file_path)
                );
                CREATE TABLE IF NOT EXISTS image_tags(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE,
                    UNIQUE(image_id, tag)
                );
                CREATE TABLE IF NOT EXISTS mood_mappings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mood_word TEXT UNIQUE NOT NULL,
                    tag TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS trash(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id INTEGER,
                    pack_name TEXT,
                    file_path TEXT NOT NULL,
                    file_name TEXT,
                    trash_path TEXT NOT NULL,
                    tags TEXT,
                    review_status TEXT,
                    deleted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS downloads(
                    id TEXT PRIMARY KEY,
                    url TEXT,
                    ref TEXT,
                    title TEXT,
                    pack_type TEXT,
                    created REAL,
                    finished REAL,
                    stage TEXT,
                    bytes INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    done_items INTEGER DEFAULT 0,
                    done INTEGER DEFAULT 0,
                    ok INTEGER,
                    cancel INTEGER DEFAULT 0,
                    pack_id INTEGER,
                    pack_name TEXT,
                    message TEXT
                );
                """
            )
            self.conn.commit()

    # ---------------- Packs ----------------
    def create_pack(self, name, display_name=None, pack_type="emotion",
                    source_url="", description="", has_native_tag=False):
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO packs
                   (name, display_name, type, enabled, source_url, description, has_native_tag, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, display_name or name, pack_type, 0, source_url,
                 description, 1 if has_native_tag else 0, _now()),
            )
            self.conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = self.conn.execute("SELECT id FROM packs WHERE name=?", (name,)).fetchone()
            return row["id"]

    def get_pack(self, name):
        row = self.conn.execute("SELECT * FROM packs WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def get_pack_by_id(self, pack_id):
        row = self.conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        return dict(row) if row else None

    def list_packs(self):
        rows = self.conn.execute("SELECT * FROM packs ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set_pack_enabled(self, name, enabled):
        with self._lock:
            self.conn.execute(
                "UPDATE packs SET enabled=? WHERE name=?",
                (1 if enabled else 0, name),
            )
            self.conn.commit()

    def set_enabled_packs(self, names):
        """多选：把 names 之内的启用，其余停用。"""
        names = set(names)
        with self._lock:
            rows = self.conn.execute("SELECT name FROM packs").fetchall()
            for r in rows:
                flag = 1 if r["name"] in names else 0
                self.conn.execute("UPDATE packs SET enabled=? WHERE name=?", (flag, r["name"]))
            self.conn.commit()

    def get_enabled_pack_names(self, pack_type=None):
        if pack_type:
            rows = self.conn.execute(
                "SELECT name FROM packs WHERE enabled=1 AND type=?", (pack_type,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT name FROM packs WHERE enabled=1").fetchall()
        return [r["name"] for r in rows]

    def update_pack_desc(self, name, description):
        with self._lock:
            self.conn.execute("UPDATE packs SET description=? WHERE name=?", (description, name))
            self.conn.commit()

    def set_pack_meta(self, name, display_name=None, description=None):
        """只改给人看的字段，name 是内部键（也是对目录名的引用）不动。"""
        with self._lock:
            sets, vals = [], []
            if display_name is not None:
                sets.append("display_name=?")
                vals.append(display_name)
            if description is not None:
                sets.append("description=?")
                vals.append(description)
            if not sets:
                return
            vals.append(name)
            self.conn.execute(f"UPDATE packs SET {', '.join(sets)} WHERE name=?", vals)
            self.conn.commit()

    def delete_pack(self, pack_id):
        with self._lock:
            self.conn.execute("DELETE FROM packs WHERE id=?", (pack_id,))
            self.conn.commit()

    # ---------------- Images ----------------
    def add_image(self, pack_id, file_path, file_name="", review_status="uncategorized"):
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO images
                   (pack_id, file_path, file_name, review_status, created_at)
                   VALUES (?,?,?,?,?)""",
                (pack_id, file_path, file_name, review_status, _now()),
            )
            self.conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = self.conn.execute(
                "SELECT id FROM images WHERE pack_id=? AND file_path=?",
                (pack_id, file_path),
            ).fetchone()
            return row["id"]

    def get_image(self, image_id):
        row = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        return dict(row) if row else None

    def list_images(self, pack_id=None, review_status=None, tag=None,
                    limit=50, offset=0):
        sql = "SELECT DISTINCT i.* FROM images i"
        joins = ""
        where = []
        params = []
        if tag:
            joins += " JOIN image_tags t ON t.image_id=i.id"
            where.append("t.tag=?")
            params.append(tag)
        if pack_id is not None:
            where.append("i.pack_id=?")
            params.append(pack_id)
        if review_status:
            where.append("i.review_status=?")
            params.append(review_status)
        sql += joins
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY i.id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_images(self, pack_id=None, review_status=None, tag=None):
        sql = "SELECT COUNT(DISTINCT i.id) AS c FROM images i"
        where = []
        params = []
        if tag:
            sql += " JOIN image_tags t ON t.image_id=i.id"
            where.append("t.tag=?")
            params.append(tag)
        if pack_id is not None:
            where.append("i.pack_id=?")
            params.append(pack_id)
        if review_status:
            where.append("i.review_status=?")
            params.append(review_status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.conn.execute(sql, params).fetchone()["c"]

    def set_review_status(self, image_id, status):
        with self._lock:
            self.conn.execute("UPDATE images SET review_status=? WHERE id=?", (status, image_id))
            self.conn.commit()

    def delete_image(self, image_id):
        with self._lock:
            self.conn.execute("DELETE FROM images WHERE id=?", (image_id,))
            self.conn.commit()

    # ---------------- Trash ----------------
    def add_trash(self, pack_id, pack_name, file_path, file_name,
                  trash_path, tags, review_status, deleted_at):
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO trash(pack_id, pack_name, file_path, file_name,
                   trash_path, tags, review_status, deleted_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pack_id, pack_name, file_path, file_name,
                 trash_path, tags, review_status, deleted_at),
            )
            self.conn.commit()
            return cur.lastrowid

    def list_trash(self):
        rows = self.conn.execute(
            "SELECT * FROM trash ORDER BY deleted_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trash(self, tid):
        row = self.conn.execute("SELECT * FROM trash WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def drop_trash(self, tid):
        with self._lock:
            self.conn.execute("DELETE FROM trash WHERE id=?", (tid,))
            self.conn.commit()

    # ---------------- Downloads ----------------
    def save_download(self, row: dict):
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO downloads
                   (id, url, ref, title, pack_type, created, finished, stage,
                    bytes, total, done_items, done, ok, cancel, pack_id,
                    pack_name, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row.get("id"), row.get("url"), row.get("ref"), row.get("title"),
                 row.get("pack_type"), row.get("created"), row.get("finished"),
                 row.get("stage"), row.get("bytes") or 0, row.get("total") or 0,
                 row.get("done_items") or 0, 1 if row.get("done") else 0,
                 None if row.get("ok") is None else (1 if row.get("ok") else 0),
                 1 if row.get("cancel") else 0, row.get("pack_id"),
                 row.get("pack_name"), row.get("message")),
            )
            self.conn.commit()

    def list_downloads(self, limit=60):
        rows = self.conn.execute(
            "SELECT * FROM downloads ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_download(self, jid):
        row = self.conn.execute(
            "SELECT * FROM downloads WHERE id=?", (jid,)
        ).fetchone()
        return dict(row) if row else None

    def drop_download(self, jid) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM downloads WHERE id=?", (jid,))
            self.conn.commit()
            return cur.rowcount or 0

    def clear_downloads(self) -> int:
        """只清已经结束的，正在跑的不动。"""
        with self._lock:
            cur = self.conn.execute("DELETE FROM downloads WHERE done=1")
            self.conn.commit()
            return cur.rowcount or 0

    def prune_downloads(self, keep=100):
        """只留最近的，历史不要越攒越多。"""
        with self._lock:
            self.conn.execute(
                """DELETE FROM downloads WHERE id NOT IN (
                       SELECT id FROM downloads ORDER BY created DESC LIMIT ?)""",
                (keep,),
            )
            self.conn.commit()

    # ---------------- Tags ----------------
    def add_tag(self, image_id, tag):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO image_tags(image_id, tag) VALUES (?,?)",
                (image_id, tag),
            )
            self.conn.commit()

    def remove_tag(self, image_id, tag):
        with self._lock:
            self.conn.execute(
                "DELETE FROM image_tags WHERE image_id=? AND tag=?",
                (image_id, tag),
            )
            self.conn.commit()

    def get_tags(self, image_id):
        rows = self.conn.execute(
            "SELECT tag FROM image_tags WHERE image_id=?", (image_id,)
        ).fetchall()
        return [r["tag"] for r in rows]

    def set_tags(self, image_id, tags):
        """覆盖一张图的标签。"""
        tags = list(dict.fromkeys(tags))
        with self._lock:
            self.conn.execute("DELETE FROM image_tags WHERE image_id=?", (image_id,))
            for t in tags:
                self.conn.execute(
                    "INSERT OR IGNORE INTO image_tags(image_id, tag) VALUES (?,?)",
                    (image_id, t),
                )
            self.conn.commit()

    # ---------------- 审核流程 ----------------
    def confirm_image(self, image_id):
        self.set_review_status(image_id, REVIEW_CONFIRMED)

    def batch_confirm(self, review_status):
        """整档一键入库，返回受影响行数。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE images SET review_status=? WHERE review_status=?",
                (REVIEW_CONFIRMED, review_status),
            )
            self.conn.commit()
            return cur.rowcount

    def revert_image(self, image_id, to_status):
        """一键撤回：把已入库的图退回指定待处理状态。"""
        self.set_review_status(image_id, to_status)

    def review_counts(self):
        result = {}
        for st in ("pending_high", "pending_low", "uncategorized", "confirmed"):
            result[st] = self.conn.execute(
                "SELECT COUNT(*) AS c FROM images WHERE review_status=?", (st,)
            ).fetchone()["c"]
        return result

    # ---------------- 取图 ----------------
    def get_random_image_by_tag(self, tag, pack_type="emotion"):
        rows = self.conn.execute(
            """SELECT i.file_path FROM images i
               JOIN packs p ON p.id=i.pack_id
               JOIN image_tags t ON t.image_id=i.id
               WHERE p.enabled=1 AND p.type=? AND i.review_status=? AND t.tag=?""",
            (pack_type, REVIEW_CONFIRMED, tag),
        ).fetchall()
        if not rows:
            return None
        return random.choice(rows)["file_path"]

    def get_random_image(self, pack_type="meme"):
        """不限标签，从指定类型的已启用、已确认图片中随机一张。"""
        rows = self.conn.execute(
            """SELECT i.file_path FROM images i JOIN packs p ON p.id=i.pack_id
               WHERE p.enabled=1 AND p.type=? AND i.review_status=?""",
            (pack_type, REVIEW_CONFIRMED),
        ).fetchall()
        return random.choice(rows)["file_path"] if rows else None

    def get_all_tag_stats(self):
        """各标准分类当前可用图片数量（已启用、已确认）。"""
        stats = {tag: 0 for tag in STANDARD_TAGS}
        rows = self.conn.execute(
            """SELECT t.tag AS tag, COUNT(*) AS c FROM images i
               JOIN packs p ON p.id=i.pack_id
               JOIN image_tags t ON t.image_id=i.id
               WHERE p.enabled=1 AND i.review_status=?
               GROUP BY t.tag""",
            (REVIEW_CONFIRMED,),
        ).fetchall()
        for r in rows:
            stats[r["tag"]] = r["c"]
        return stats

    # ---------------- 心情映射 ----------------
    def upsert_mood(self, mood_word, tag):
        with self._lock:
            self.conn.execute(
                """INSERT INTO mood_mappings(mood_word, tag) VALUES (?,?)
                   ON CONFLICT(mood_word) DO UPDATE SET tag=excluded.tag""",
                (mood_word, tag),
            )
            self.conn.commit()

    def delete_mood(self, mood_word):
        with self._lock:
            self.conn.execute("DELETE FROM mood_mappings WHERE mood_word=?", (mood_word,))
            self.conn.commit()

    def list_moods(self):
        rows = self.conn.execute(
            "SELECT mood_word, tag FROM mood_mappings ORDER BY mood_word"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_mood_tag(self, mood_word):
        row = self.conn.execute(
            "SELECT tag FROM mood_mappings WHERE mood_word=?", (mood_word,)
        ).fetchone()
        return row["tag"] if row else None

    def bulk_upsert_moods(self, mapping):
        with self._lock:
            for word, tag in mapping.items():
                self.conn.execute(
                    """INSERT INTO mood_mappings(mood_word, tag) VALUES (?,?)
                       ON CONFLICT(mood_word) DO UPDATE SET tag=excluded.tag""",
                    (word, tag),
                )
            self.conn.commit()

    # ---------------- meta ----------------
    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self.conn.execute(
                """INSERT INTO meta(key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, str(value)),
            )
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
