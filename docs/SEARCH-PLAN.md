# Semantic search and guided corrections

Implement directly as one coupled workstream. Keep the local security profile and existing Turbo settings.

1. Add pinned, explicitly prepared multilingual E5-large (default; smaller profiles supported) quantized ONNX embeddings, a serialized two-thread runtime, small overlapping timestamped chunks, SQLite vector storage, and hybrid retrieval. No vector service or PyTorch. Preserve keyword fallback when weights are absent.
2. Add recording-scoped candidate discovery from a marked phrase and user hint. Queue only selected passages; the one-job worker crops retained audio, applies the hint, and returns proposals. Accept proposals explicitly, preserve prior text, reject stale revisions, and invalidate/rebuild affected embeddings.
3. Keep the interface compact; remove the explanatory sidebar footer. Add focused search and correction controls.
4. Verify backend behavior, the real quantized model on English/Spanish examples, worker crop/hint protocol, OpenAPI, frontend build/lint, and bounded browser interactions. Commit conventionally. No deployment or real-data migration is part of this work.

Implementation and local verification are recorded in STATUS.md; model selection evidence is in EMBEDDING-EVALUATION.md. No deployment is included.
