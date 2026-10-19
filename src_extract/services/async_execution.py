"""Bound blocking work without releasing capacity when its awaiter goes away.

Python threads cannot be killed. Timed-out running calls retain their slot until
they actually finish; cancelled queued calls never execute. No unbounded queue
of orphaned work is created by HTTP disconnects or asyncio timeouts.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class ExecutionBusy(RuntimeError):
    pass


class ExecutionTimeout(TimeoutError):
    pass


class BoundedExecution:
    def __init__(self, name: str, workers: int, queue: int, queue_timeout: float = 10):
        self.workers = max(1, workers)
        self.capacity = self.workers + max(0, queue)
        self.queue_timeout = queue_timeout
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._accepted = self._active = 0

    def snapshot(self):
        with self._lock:
            return {"limit": self.workers, "active": self._active,
                    "waiting": max(0, self._accepted - self._active),
                    "queue_limit": self.capacity - self.workers}

    async def run(self, callback, *args, deadline=None, discard=None, **kwargs):
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        context = contextvars.copy_context()
        abandoned = threading.Event()
        result_lock = threading.Lock()
        disposed = False

        def dispose(value):
            nonlocal disposed
            with result_lock:
                if disposed:
                    return
                disposed = True
            if discard is not None:
                discard(value)

        def mark_started():
            if not started.done():
                started.set_result(None)

        def invoke():
            with self._lock:
                self._active += 1
            try:
                if abandoned.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    raise ExecutionTimeout("execution expired before start")
                try:
                    loop.call_soon_threadsafe(mark_started)
                except RuntimeError:
                    raise ExecutionTimeout("request event loop closed") from None
                result = context.run(callback, *args, **kwargs)
                if abandoned.is_set():
                    dispose(result)
                return result
            finally:
                with self._lock:
                    self._active -= 1

        with self._lock:
            if self._accepted >= self.capacity:
                raise ExecutionBusy("execution queue is full")
            self._accepted += 1
        try:
            future = self._executor.submit(invoke)
        except BaseException:
            with self._lock:
                self._accepted -= 1
            raise

        def completed(done):
            try:
                if abandoned.is_set() and not done.cancelled() and done.exception() is None:
                    dispose(done.result())
            finally:
                with self._lock:
                    self._accepted -= 1

        future.add_done_callback(completed)
        wrapped = asyncio.wrap_future(future, loop=loop)
        # Retrieve abandoned exceptions so asyncio never emits an unhandled
        # future warning (nor dumps sensitive upstream errors).
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        try:
            queue_budget = self.queue_timeout
            if deadline is not None:
                queue_budget = min(queue_budget, max(0, deadline - time.monotonic()))
            done, _ = await asyncio.wait({started, wrapped}, timeout=queue_budget,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise ExecutionTimeout("execution queue wait expired")
            budget = None if deadline is None else max(0, deadline - time.monotonic())
            done, _ = await asyncio.wait({wrapped}, timeout=budget)
            if not done:
                raise ExecutionTimeout("execution deadline expired")
            return wrapped.result()
        except BaseException:
            abandoned.set()
            # Do not cancel Future here: ThreadPoolExecutor keeps cancelled
            # work items in its private queue. Releasing admission on cancel
            # would let repeated disconnects build an unbounded hidden queue.
            # invoke() skips abandoned callbacks; capacity frees on dequeue.
            if future.done() and not future.cancelled() and future.exception() is None:
                # A completed result can race the cancellation of its waiter.
                # Schedule potentially blocking iterator.close off the loop.
                if discard is not None and not disposed:
                    self._executor.submit(dispose, future.result())
            raise
        finally:
            started.cancel()

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
