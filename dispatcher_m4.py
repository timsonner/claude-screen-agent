"""
M4 demo: capture frames, push through Dispatcher, print stats.

Run for 15 seconds. While it's running, alternate between sitting still
(should rack up `dropped_unchanged`) and moving a window / scrolling
(should rack up `dispatched`). A "consumer" coroutine pulls from the
dispatcher's slot every 3 seconds, simulating slow inference.
"""

from __future__ import annotations

import asyncio
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from capture import Capture
from dispatcher import Dispatcher
from portal_remotedesktop import open_screencast_session

CAPTURE_SECONDS = 15.0
CONSUMER_PERIOD = 3.0


async def _producer(cap: Capture, disp: Dispatcher, stop_evt: asyncio.Event):
    """Forward each new frame from Capture to Dispatcher.

    The capture's appsink callback runs on a streaming thread; we just
    poll its `latest()` slot from asyncio because we know fps is low (4).
    """
    last_seq = 0
    while not stop_evt.is_set():
        f = cap.latest()
        if f is not None and f.seq != last_seq:
            disp.submit(f)
            last_seq = f.seq
        await asyncio.sleep(0.05)


async def _consumer(disp: Dispatcher, stop_evt: asyncio.Event):
    while not stop_evt.is_set():
        slot = disp.take()
        if slot is not None:
            jpeg, seq = slot
            print(f"[consumer] took frame seq={seq}  jpeg={len(jpeg)} bytes")
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=CONSUMER_PERIOD)
        except asyncio.TimeoutError:
            pass


async def _glib_pump(stop_evt: asyncio.Event):
    main_ctx = GLib.MainContext.default()
    while not stop_evt.is_set():
        while main_ctx.iteration(False):
            pass
        await asyncio.sleep(0.05)


async def _stat_logger(disp: Dispatcher, stop_evt: asyncio.Event):
    while not stop_evt.is_set():
        s = disp.stats()
        print(
            f"[stats] dispatched={s.dispatched}  consumed={s.consumed}  "
            f"dropped_unchanged={s.dropped_unchanged}  "
            f"dropped_saturation={s.dropped_saturation}"
        )
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def _main() -> int:
    handle = await open_screencast_session()
    print(f"[m4] session up — node_id={handle.node_id}")

    cap = Capture(handle, fps=4, width=1280)
    cap.start()
    disp = Dispatcher(hamming_threshold=5, jpeg_quality=80)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_glib_pump(stop)),
        asyncio.create_task(_producer(cap, disp, stop)),
        asyncio.create_task(_consumer(disp, stop)),
        asyncio.create_task(_stat_logger(disp, stop)),
    ]

    print(f"[m4] running {CAPTURE_SECONDS}s — try moving a window or scrolling!")
    deadline = time.monotonic() + CAPTURE_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    cap.stop()

    final = disp.stats()
    print()
    print(
        f"=== M4 RESULT === captured={cap.frame_count}  "
        f"dispatched={final.dispatched}  consumed={final.consumed}  "
        f"dropped_unchanged={final.dropped_unchanged}  "
        f"dropped_saturation={final.dropped_saturation}"
    )
    if cap.errors:
        print("ERRORS:", cap.errors, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
