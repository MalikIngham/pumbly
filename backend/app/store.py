import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = '''
CREATE TABLE IF NOT EXISTS blogs (
    blog TEXT PRIMARY KEY,
    synced_until INTEGER,
    cursor INTEGER,
    walk_top INTEGER,
    scanned INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    new_originals INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    last_synced_at REAL
);
CREATE TABLE IF NOT EXISTS posts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    blog TEXT NOT NULL,
    post_id TEXT NOT NULL,
    timestamp INTEGER,
    raw TEXT NOT NULL,
    UNIQUE (blog, post_id)
);
CREATE INDEX IF NOT EXISTS posts_by_blog ON posts (blog, seq);
'''


@dataclass
class BlogState:
    '''How far a blog has been scanned.

    Everything at or before synced_until is known. A walk moves from newest to
    oldest, so while one is unfinished, cursor is where it picks up and
    walk_top is the newest timestamp it started from.'''

    blog: str
    synced_until: int | None = None
    cursor: int | None = None
    walk_top: int | None = None
    scanned: int = 0
    total: int | None = None
    new_originals: int = 0
    error: str | None = None
    last_synced_at: float | None = None


def post_id(post: dict[str, Any]) -> str:
    return post.get('id_string') or str(post['id'])


class Store:
    '''Originals are kept as Tumblr sent them, so a parsing change can be
    applied to saved posts without calling the API again.'''

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous = NORMAL')
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, blog: str) -> BlogState | None:
        row = self.db.execute('SELECT * FROM blogs WHERE blog = ?', (blog,)).fetchone()
        return BlogState(**dict(row)) if row else None

    def ensure(self, blog: str) -> BlogState:
        state = self.get(blog)
        if state is None:
            state = BlogState(blog=blog)
            self.save(state)
        return state

    def _write_state(self, state: BlogState) -> None:
        self.db.execute(
            '''INSERT OR REPLACE INTO blogs
               (blog, synced_until, cursor, walk_top, scanned, total, new_originals, error, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                state.blog, state.synced_until, state.cursor, state.walk_top, state.scanned,
                state.total, state.new_originals, state.error, state.last_synced_at,
            ),
        )

    def save(self, state: BlogState) -> None:
        self._write_state(state)
        self.db.commit()

    def save_page(self, state: BlogState, originals: list[dict[str, Any]]) -> int:
        '''Posts and position are committed together, so a crash can never
        record progress for posts that were not saved. Returns how many of
        the posts were not already stored.'''
        cursor = self.db.executemany(
            'INSERT OR IGNORE INTO posts (blog, post_id, timestamp, raw) VALUES (?, ?, ?, ?)',
            [(state.blog, post_id(p), p.get('timestamp'), json.dumps(p)) for p in originals],
        )
        added = max(cursor.rowcount, 0)
        self._write_state(state)
        self.db.commit()
        return added

    def count_posts(self, blog: str) -> int:
        return self.db.execute('SELECT COUNT(*) FROM posts WHERE blog = ?', (blog,)).fetchone()[0]

    def latest_seq(self, blog: str) -> int:
        row = self.db.execute('SELECT MAX(seq) FROM posts WHERE blog = ?', (blog,)).fetchone()
        return row[0] or 0

    def read_posts(
        self, blog: str, after: int, limit: int, up_to: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        '''Posts in the order they were saved, starting after seq `after`.'''
        rows = self.db.execute(
            'SELECT seq, raw FROM posts WHERE blog = ? AND seq > ? AND seq <= ? ORDER BY seq LIMIT ?',
            (blog, after, up_to if up_to is not None else 2**62, limit),
        ).fetchall()
        return [(row['seq'], json.loads(row['raw'])) for row in rows]
