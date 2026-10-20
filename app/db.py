"""Small durable SQLite repository. Only the server can choose storage paths."""
import json
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(root / 'index.sqlite3', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS videos (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
          duration REAL DEFAULT 0, fps REAL DEFAULT 0, width INTEGER DEFAULT 0,
          height INTEGER DEFAULT 0, status TEXT DEFAULT 'uploaded', stage TEXT DEFAULT '',
          frames_total INTEGER DEFAULT 0, error TEXT, input_tokens INTEGER DEFAULT 0,
          model TEXT, taxonomy_version TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS frames (
          id INTEGER PRIMARY KEY, video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL, timestamp REAL NOT NULL, path TEXT NOT NULL,
          status TEXT DEFAULT 'pending', probabilities TEXT, error TEXT,
          UNIQUE(video_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS frame_video_status ON frames(video_id,status);
        CREATE TABLE IF NOT EXISTS match_cache (
          frame_id INTEGER REFERENCES frames(id) ON DELETE CASCADE,
          query TEXT, model TEXT, score REAL NOT NULL,
          PRIMARY KEY(frame_id,query,model)
        );
        CREATE TABLE IF NOT EXISTS usage (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, input_tokens INTEGER NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        self.conn.execute("UPDATE videos SET status='paused', stage='Paused after restart' WHERE status IN ('indexing','extracting','pausing','uploaded')")
        self.conn.execute("UPDATE videos SET status='failed', stage='Import interrupted', error='The download was interrupted. Import this URL again to restart the download.' WHERE status='downloading'")
        self.conn.execute("UPDATE frames SET status='pending' WHERE status='processing'")
        self.conn.commit()

    def all(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def run(self, sql, args=()):
        cursor = self.conn.execute(sql, args)
        self.conn.commit()
        return cursor.lastrowid

    def update_video(self, video_id, **fields):
        allowed = {'duration','fps','width','height','status','stage','frames_total','error','model','taxonomy_version'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid video fields')
        self.run('UPDATE videos SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', (*fields.values(), video_id))

    def save_frames(self, video_id, frames):
        with self.conn:
            self.conn.executemany('INSERT OR IGNORE INTO frames(video_id,ordinal,timestamp,path) VALUES(?,?,?,?)',
                                  [(video_id,i,f['timestamp'],str(f['path'])) for i,f in enumerate(frames)])

    def save_index(self, frame_id, probabilities):
        self.run("UPDATE frames SET status='ready', probabilities=?, error=NULL WHERE id=?", (json.dumps(probabilities),frame_id))

    def save_prepared_frames(self, video_id, frames):
        """Replace an extracted sequence while retaining existing frame IDs."""
        with self.conn:
            # Called only for a new or regenerated sequence. Neighbor evidence
            # may have changed, so invalidate matches across the whole video.
            self.conn.execute('DELETE FROM match_cache WHERE frame_id IN (SELECT id FROM frames WHERE video_id=?)',(video_id,))
            self.conn.executemany(
                """INSERT INTO frames(video_id,ordinal,timestamp,path,status)
                   VALUES(?,?,?,?,'ready')
                   ON CONFLICT(video_id,ordinal) DO UPDATE SET timestamp=excluded.timestamp,
                   path=excluded.path,status='ready',probabilities=NULL,error=NULL""",
                [(video_id,i,f['timestamp'],str(f['path'])) for i,f in enumerate(frames)])
            self.conn.execute('DELETE FROM frames WHERE video_id=? AND ordinal>=?',(video_id,len(frames)))

    def add_usage(self, kind, tokens, video_id=None):
        with self.conn:
            self.conn.execute('INSERT INTO usage(kind,input_tokens) VALUES(?,?)', (kind,tokens))
            if video_id:
                self.conn.execute('UPDATE videos SET input_tokens=input_tokens+? WHERE id=?',(tokens,video_id))

    def close(self):
        self.conn.close()
