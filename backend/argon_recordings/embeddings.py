"""Small local multilingual retrieval runtime; no network access during inference."""
from collections import OrderedDict
import hashlib
import logging
import json
from pathlib import Path
import threading

PROFILES = {
 'small': ('Xenova/multilingual-e5-small', '761b726dd34fb83930e26aab4e9ac3899aa1fa78', {
  'onnx/model_quantized.onnx': 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193',
  'tokenizer.json': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'}),
 'base': ('Xenova/multilingual-e5-base', '1ec9243030a27d1a115d5c340572074c125b58b2', {
  'onnx/model_quantized.onnx': 'df7a9a29309e3ad491e1783adf8baee710262cc06079c7cbab63c630277fac94',
  'tokenizer.json': '62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626'})}

# Keep quantized comparison profiles available; verify accuracy before choosing a default.
PROFILES['base-fp32'] = ('Xenova/multilingual-e5-base', '1ec9243030a27d1a115d5c340572074c125b58b2', {
 'onnx/model.onnx': '84a4d426f7e87a6bf5bf195f0bae2c4a7d15f675b23ca96f42fab8326d7a77aa',
 'tokenizer.json': '62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626'})

PROFILES['large'] = ('Xenova/multilingual-e5-large', '00fc3aeb3dbb95842de2ac1961d33c6319acf57b', {
 'onnx/model_quantized.onnx': '0a8d65db9a36f810ba5da15249f13145fcdc7890e6656f1fd38cd8b7c4db1fca',
 'tokenizer.json': '62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626'})


def fingerprint(rows):
    return hashlib.sha256(json.dumps([(r['id'], r['text']) for r in rows], ensure_ascii=False).encode()).hexdigest()


class Embeddings:
    def __init__(self, directory=None, profile="large"):
        self.profile = profile
        self.repo, self.revision, self.files = PROFILES[profile]
        self.model = f"{self.repo}@{self.revision}:{profile}:256:mean:v1"
        self.directory = Path(directory) if directory else None
        self.lock = threading.RLock()
        self.session = self.tokenizer = None
        self.query_cache = OrderedDict()

    @property
    def configured(self):
        return self.directory is not None

    def _load(self):
        if self.session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer
        for name, expected in self.files.items():
            digest = hashlib.sha256()
            with (self.directory / name).open('rb') as stream:
                for block in iter(lambda: stream.read(1024**2), b''):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError('Embedding model checksum mismatch; run prepare_embeddings.py')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(self.directory / next(n for n in self.files if n.endswith('.onnx'))), options,
                                           providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(self.directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')

    def encode(self, texts, query=False):
        import numpy as np
        with self.lock:
            if query and len(texts) == 1 and texts[0] in self.query_cache:
                self.query_cache.move_to_end(texts[0])
                return [self.query_cache[texts[0]]]
            self._load()
            vectors = []
            for start in range(0, len(texts), 4):
                batch = self.tokenizer.encode_batch([('query: ' if query else 'passage: ') + t for t in texts[start:start + 4]])
                ids = np.array([b.ids for b in batch], dtype=np.int64)
                mask = np.array([b.attention_mask for b in batch], dtype=np.int64)
                inputs = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
                hidden = self.session.run(None, {i.name: inputs[i.name] for i in self.session.get_inputs()})[0]
                pooled = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
                pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
                vectors.extend(pooled.astype('<f4'))
            if query and len(texts) == 1:
                self.query_cache[texts[0]] = vectors[0]
                if len(self.query_cache) > 64:
                    self.query_cache.popitem(last=False)
            return vectors

    def chunks(self, rows):
        self._load()
        text, spans = '', []
        for row in rows:
            start = len(text)
            text += row['text'] + ' '
            spans.append((start, len(text), row))
        if len(text) > 250000:
            raise RuntimeError('Transcript exceeds the local indexing budget')
        # Token windows prevent silent truncation, including long single ASR segments.
        self.tokenizer.no_truncation()
        try:
            offsets = self.tokenizer.encode(text, add_special_tokens=False).offsets
        finally:
            self.tokenizer.enable_truncation(max_length=256)
        windows = []
        for i in range(0, len(offsets), 180):
            window = offsets[i:i + 220]
            start, end = window[0][0], window[-1][1]
            sources = [r for a, b, r in spans if a < end and b > start]
            if sources:
                windows.append(dict(start=sources[0]['start'], end=max(r['end'] for r in sources), text=text[start:end]))
        return windows

    def index(self, store, rid):
        if not self.configured:
            return
        # Serialize inference and duplicate indexing; don't hold a SQLite transaction during inference.
        with self.lock:
            with store.db() as db:
                previous = db.execute('SELECT sha FROM embedding_versions WHERE recording_id=? AND model=?', (rid, self.model)).fetchone()
                if previous:
                    return  # Accepted edits atomically invalidate this recording's index.
                rows = db.execute('SELECT * FROM segments WHERE recording_id=? ORDER BY start,id', (rid,)).fetchall()
                sha = fingerprint(rows)
            windows = self.chunks(rows) if rows else []
            vectors = self.encode([w['text'] for w in windows]) if windows else []
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM segments WHERE recording_id=? ORDER BY start,id', (rid,)).fetchall()
                if fingerprint(current) != sha:
                    return  # A concurrent accepted correction won; next search rebuilds.
                db.execute('DELETE FROM embeddings WHERE recording_id=?', (rid,))
                for window, vector in zip(windows, vectors):
                    db.execute('INSERT INTO embeddings(recording_id,start,end,text,vector,model) VALUES(?,?,?,?,?,?)',
                               (rid, window['start'], window['end'], window['text'], vector.tobytes(), self.model))
                db.execute('INSERT OR REPLACE INTO embedding_versions VALUES(?,?,?)', (rid, self.model, sha))

    def refresh(self, store, rid):
        try:
            self.index(store, rid)
        except Exception:
            logging.getLogger(__name__).exception('Embedding refresh failed for recording %s; search can retry', rid)

    def search(self, store, pid, q):
        import numpy as np
        with self.lock:
            with store.db() as db:
                ids = [r[0] for r in db.execute('SELECT DISTINCT r.id FROM recordings r JOIN segments s ON s.recording_id=r.id WHERE r.project_id=?', (pid,))]
                if len(ids) > 200:
                    raise RuntimeError('Local semantic search limit is 200 recordings per project')
            if not ids:
                return []
            for rid in ids:
                self.index(store, rid)
            vector = self.encode([q], query=True)[0]
            with store.db() as db:
                rows = db.execute('SELECT e.*,r.title FROM embeddings e JOIN recordings r ON r.id=e.recording_id JOIN embedding_versions v ON v.recording_id=r.id AND v.model=e.model WHERE r.project_id=? AND e.model=? LIMIT 10001', (pid, self.model)).fetchall()
            if len(rows) > 10000:
                raise RuntimeError('Local semantic search limit is 10000 chunks per project')
            scored = [(float(np.dot(vector, np.frombuffer(r['vector'], dtype='<f4'))), r) for r in rows]
            result, seen = [], set()
            for score, row in sorted(scored, key=lambda x: x[0], reverse=True):
                key = (row['recording_id'], row['start'])
                if key in seen:
                    continue
                seen.add(key)
                result.append(dict(recording_id=row['recording_id'], title=row['title'], start=row['start'], end=row['end'], text=row['text']))
                if len(result) == 30:
                    break
            return result
