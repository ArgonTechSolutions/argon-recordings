"""Synthetic browser media fixture; real FFmpeg files, synthetic transcript."""
import hashlib
from pathlib import Path
import runpy
import shutil
import sys

runpy.run_path('scripts/seed_browser.py',run_name='__main__')
from argon_recordings.store import Store
store=Store(Path(sys.argv[1]))
fixture=Path(sys.argv[2])
with store.db() as db:
    rid=db.execute('SELECT id FROM recordings LIMIT 1').fetchone()[0]
    source=store.root/'sources'/rid
    shutil.copyfile(fixture/'source.mp4',source)
    db.execute('UPDATE recordings SET sha256=?,bytes=? WHERE id=?',
               (hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,rid))
    for i in range(26):
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(rid,f'frame-{i:04d}.jpg',float(i),'frame',str(fixture/'still/image.jpg')))
