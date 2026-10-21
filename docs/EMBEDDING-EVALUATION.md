# Retrieval backend and model choice

Use exact cosine retrieval over persistent SQLite vectors, fused with SQLite FTS5 keyword rankings (reciprocal rank fusion). A vector database service does not make an embedding model more accurate. At the current bounded scale (200 recordings / 10,000 chunks per project), another service adds deployment and memory overhead without a demonstrated need. Consider an indexed vector backend if measured query latency or corpus growth requires it. These limits fail explicitly; search never silently truncates a larger corpus.

The default is **multilingual E5-large, quantized ONNX**, with two inference threads, one serialized session, batches of four, and a 64-query vector cache. The model files total about 579 MB (decimal). No PyTorch, GPU, online inference, or automatic model downloads are required. Small, base, and base FP32 profiles remain explicit alternatives. ASR remains Turbo CPU INT8 with two threads.

## Local evidence

`scripts/evaluate_embeddings.py` uses 16 synthetic technical/product passages and 16 English, Spanish, and cross-language questions. Each question has one intended passage. This is a small engineering fixture, **not a representative relevance benchmark**. It contains ambiguous queries and does not establish general retrieval accuracy.

Measured on Vulcan in separate containers limited to two CPUs. These are **not Philemon timings**. RSS is process peak, not total container memory; cold time includes checksum verification, loading, and one warmup query.

| Profile | Intended passage first | In top 3 | Peak RSS | Cold seconds | Warm median query seconds |
| --- | --- | --- | --- | --- | --- |
| Small INT8 | 7/16 | 9/16 | 474 MiB | 1.07 | 0.006 |
| Base INT8 | 10/16 | 13/16 | 626 MiB | 1.68 | 0.015 |
| Base FP32 | 10/16 | 14/16 | 1823 MiB | 7.97 | 0.030 |
| Large INT8 | 11/16 | 15/16 | 901 MiB | 3.60 | 0.049 |

Large INT8 gives the strongest result in this fixture while remaining well below the supplied Philemon RAM capacity. It is the default; full-precision base did not justify its higher memory cost. Philemon runtime/UX measurements remain pending. The application can be installed with a different profile without changing the API or vector storage architecture; model identity is stored with each index so incompatible vectors are rebuilt.

Chunks contain 220 tokenizer tokens with 40 tokens of overlap and source timestamps. A long single-segment tail coverage check passes for all four profiles. New publications and accepted corrections trigger indexing after the response; a missing/interrupted index is rebuilt on search. Per-recording invalidation is durable. Query failures expose an actionable error and keyword mode remains available.

Reproduce with the documented Docker test image and `PYTHONPATH=backend python scripts/evaluate_embeddings.py models/e5-large --profile large`. Preparation downloads immutable revision URLs and verifies embedded SHA-256 values. Model documentation: [multilingual E5](https://huggingface.co/intfloat/multilingual-e5-large), [ONNX artifacts](https://huggingface.co/Xenova/multilingual-e5-large/tree/00fc3aeb3dbb95842de2ac1961d33c6319acf57b).
