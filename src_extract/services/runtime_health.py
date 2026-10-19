"""Observe event-loop stalls from a separate thread, without logging secrets.

No restart timer. Emit stack *locations*, never locals, source lines, request
bodies, URLs or credentials. Health endpoints remain on the real application
event loop so Docker does not report a blocked service as healthy.
"""
import asyncio
import logging
import os
import sys
import threading
import time
import traceback


class EventLoopWatchdog:
    def __init__(self, threshold=5.0, report_interval=30.0, exit_after=None, exit_func=os._exit):
        self.threshold = threshold
        self.report_interval = report_interval
        self._beat = time.monotonic()
        self._last_report = 0.0
        self._stop = threading.Event()
        self._task = None
        self._thread = None
        self._loop_thread = None
        try:
            self.exit_after = max(0, float(os.getenv("CHATGPT2API_EVENT_LOOP_STALL_EXIT_SECONDS", "0"))) if exit_after is None else exit_after
        except ValueError:
            self.exit_after = 0
        self._exit = exit_func

    async def _heartbeat(self):
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(0.5)

    def _observe(self):
        while not self._stop.wait(1):
            now = time.monotonic()
            lag = now - self._beat
            if lag < self.threshold or now - self._last_report < self.report_interval:
                continue
            self._last_report = now
            frame = sys._current_frames().get(self._loop_thread)
            # extract_stack does not include locals. Strip source code too.
            stack = [(f.filename.rsplit('/', 1)[-1], f.lineno, f.name)
                     for f in traceback.extract_stack(frame)[-16:]] if frame else []
            logging.getLogger(__name__).error(
                "event_loop_stalled lag_seconds=%.3f stack_locations=%s", lag, stack)
            if self.exit_after and lag >= max(30, self.exit_after):
                logging.getLogger(__name__).critical(
                    "event_loop_unresponsive_exit lag_seconds=%.3f exit_code=70", lag)
                # Opt-in, continuous event-loop failure only. Docker's existing
                # restart policy recovers this exited process. Never restart
                # merely because images are active or because a timer elapsed.
                self._exit(70)

    def start(self):
        self._loop_thread = threading.get_ident()
        self._stop.clear()
        self._beat = time.monotonic()
        self._task = asyncio.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._observe, name="event-loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
