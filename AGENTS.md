# Argon Recordings

- Argon owns this project. The project knowledge base belongs in YouTrack; its article is pending.
- Preserve the current `backend/`, `web/`, `docs/`, and `scripts/` layout.
- Use large-v3-turbo on CPU with INT8 and two threads. Preserve shadcn preset `b5xcvlbv6`.
- Validate backend tests, the OpenAPI contract, the frontend production build, and lint when changing the corresponding application code.
- Use conventional commits. Keep recordings, model weights, mutable data, and secrets out of Git.
- Do not start dev servers or persistent services without an explicit request.
