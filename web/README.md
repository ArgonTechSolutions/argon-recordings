# Argon Recordings frontend

React + TypeScript + Vite. Initialized with the owner-supplied shadcn preset `b5xcvlbv6` (base-sera, mauve, Inter, Lucide, pointer cursors).

Run `npm ci`, `npm run build`, and `npm run lint` for one-shot validation. The production app serves these static assets through the coordinator. See the repository README for local preview, API generation, and the current single-user boundary.

`?preview=1` shows labelled synthetic design content with uploads disabled. The ordinary application uses the generated API types in `src/lib/schema.d.ts` and live coordinator requests. Keyword search is implemented; semantic search is the next milestone.
