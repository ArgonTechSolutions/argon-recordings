"""Commit-driven, single-process fan-out; one shared snapshot per watched project."""
import asyncio
from contextlib import suppress
import json
import logging

log = logging.getLogger(__name__)


class ProjectEvents:
    def __init__(self, snapshot, debounce=.1, keepalive=15):
        self.snapshot = snapshot
        self.debounce, self.keepalive = debounce, keepalive
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.subscribers = {}
        self.latest = {}
        self.task = asyncio.create_task(self.broadcast())

    def notify(self):
        # SQLite writes also run in FastAPI's thread pool. Notify only after commit.
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.changed.set)

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    async def broadcast(self):
        while True:
            await self.changed.wait()
            await asyncio.sleep(self.debounce)
            self.changed.clear()
            for pid in list(self.subscribers):
                try:
                    rows = await asyncio.to_thread(self.snapshot, pid)
                    payload = json.dumps(rows, separators=(',', ':'), ensure_ascii=False)
                except Exception:
                    log.exception('Could not refresh a project event snapshot')
                    self.changed.set()
                    await asyncio.sleep(1)
                    continue
                queues = self.subscribers.get(pid)
                if not queues:
                    continue
                if payload != self.latest.get(pid):
                    self.latest[pid] = payload
                    for queue in queues:
                        # Coalesce updates instead of accumulating work for slow clients.
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(payload)

    async def stream(self, pid):
        queue = asyncio.Queue(maxsize=1)
        queues = self.subscribers.setdefault(pid, set())
        queues.add(queue)
        # A new connection always gets an authoritative snapshot, not a partial replay.
        if pid in self.latest:
            queue.put_nowait(self.latest[pid])
        self.notify()
        try:
            yield 'retry: 3000\n\n'
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), self.keepalive)
                    yield f'event: recordings\ndata: {payload}\n\n'
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
        finally:
            queues.discard(queue)
            if not queues:
                self.subscribers.pop(pid, None)
                self.latest.pop(pid, None)
