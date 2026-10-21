from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid

SCHEMA = '''
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE recordings(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,
 filename TEXT NOT NULL,language TEXT NOT NULL,status TEXT NOT NULL,bytes INTEGER DEFAULT 0,sha256 TEXT,
 duration REAL,error TEXT,created REAL NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY,recording_id TEXT NOT NULL UNIQUE REFERENCES recordings(id),state TEXT NOT NULL,
 priority INTEGER NOT NULL,lease_id TEXT,lease_until REAL,worker TEXT,attempts INTEGER DEFAULT 0,
 progress REAL DEFAULT 0,stage TEXT DEFAULT 'Waiting for worker',result_sha TEXT,created REAL NOT NULL);
CREATE TABLE segments(id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),start REAL NOT NULL,
 end REAL NOT NULL,text TEXT NOT NULL,words TEXT NOT NULL);
CREATE VIRTUAL TABLE segment_search USING fts5(text,segment_id UNINDEXED,recording_id UNINDEXED);
CREATE TABLE artifacts(recording_id TEXT NOT NULL,name TEXT NOT NULL,time REAL,kind TEXT NOT NULL,path TEXT NOT NULL,
 PRIMARY KEY(recording_id,name));
PRAGMA user_version=1;
'''


def identity():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root: Path):
        self.root = root
        for folder in ['sources', 'staging', 'results']:
            (root / folder).mkdir(parents=True, exist_ok=True)
        self.path = root / 'recordings.sqlite'
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                db.executescript(SCHEMA)
            elif version != 1:
                raise RuntimeError('Unsupported database version; automatic migration is disabled')
            db.executescript("""
CREATE TABLE IF NOT EXISTS embeddings(recording_id TEXT NOT NULL REFERENCES recordings(id),start REAL NOT NULL,
 end REAL NOT NULL,text TEXT NOT NULL,vector BLOB NOT NULL,model TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS embeddings_recording ON embeddings(recording_id);
CREATE TABLE IF NOT EXISTS embedding_versions(recording_id TEXT PRIMARY KEY REFERENCES recordings(id),model TEXT NOT NULL,sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 segment_id TEXT NOT NULL REFERENCES segments(id),original TEXT NOT NULL,hint TEXT NOT NULL,state TEXT NOT NULL,
 lease_id TEXT,lease_until REAL,attempts INTEGER DEFAULT 0,proposal TEXT,error TEXT,created REAL NOT NULL);
""")
            # An interrupted upload can be retried. Completed source files are never pruned here.
            db.execute("UPDATE recordings SET status='awaiting_upload' WHERE status='uploading'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def project(self, project_id):
        with self.db() as db:
            return db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()

    def recording(self, recording_id):
        with self.db() as db:
            return db.execute('SELECT * FROM recordings WHERE id=?', (recording_id,)).fetchone()

    def claim(self, worker):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE state='running' AND lease_until>? LIMIT 1", (now,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM corrections WHERE state='running' AND lease_until>?", (now,)).fetchone():
                return None
            exhausted = db.execute("SELECT recording_id FROM jobs WHERE state='running' AND lease_until<=? AND attempts>=3", (now,)).fetchall()
            for row in exhausted:
                db.execute("UPDATE jobs SET state='failed',stage='Lease retries exhausted' WHERE recording_id=?", (row[0],))
                db.execute("UPDATE recordings SET status='failed',error='Worker lease expired repeatedly' WHERE id=?", (row[0],))
            job = db.execute("SELECT * FROM jobs WHERE state='queued' OR (state='running' AND lease_until<=? AND attempts<3) ORDER BY priority*300-created DESC LIMIT 1", (now,)).fetchone()
            if not job:
                return None
            lease = identity()
            db.execute("UPDATE jobs SET state='running',lease_id=?,lease_until=?,worker=?,attempts=attempts+1,progress=0,stage='Starting' WHERE id=?", (lease, now + 120, worker, job['id']))
            db.execute("UPDATE recordings SET status='running',error=NULL WHERE id=?", (job['recording_id'],))
            recording = dict(db.execute('SELECT * FROM recordings WHERE id=?', (job['recording_id'],)).fetchone())
            return {'job_id': job['id'], 'lease_id': lease, 'lease_seconds': 120, 'recording': recording}


def lease_row(db, job_id, lease):
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row or row['state'] != 'running' or row['lease_id'] != lease or row['lease_until'] <= time.time():
        return None
    return row
