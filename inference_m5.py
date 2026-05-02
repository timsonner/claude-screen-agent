"""
M5 demo: capture -> dispatcher -> Claude inference loop.

Runs for 30 seconds. Every ~4 seconds the consumer pulls the freshest
dispatchable frame and asks Claude what's on screen. Watch:
  - First call: cache_creation_tokens > 0, cache_read_tokens == 0
  - Subsequent calls: cache_read_tokens > 0 (system prompt is cached)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from capture import Capture
from dispatcher import Dispatcher
from inference import ClaudeInferer
from portal_remotedesktop import open_screencast_session

DURATION = 30.0
INFERENCE_PERIOD = 4.0


async def _main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY before running.", file=sys.stderr)
        return 2

    handle = await open_screencast_session()
    print(f"[m5] session up — node_id={handle.node_id}")

    cap = Capture(handle, fps=4, width=1280)
    cap.start()
    disp = Dispatcher(hamming_threshold=5, jpeg_quality=80)
    inferer = ClaudeInferer(effort="low")

    stop = asyncio.Event()
    inferences_done = 0

    async def glib_pump():
        ctx = GLib.MainContext.default()
        while not stop.is_set():
            while ctx.iteration(False):
                pass
            await asyncio.sleep(0.05)

    async def producer():
        last_seq = 0
        while not stop.is_set():
            f = cap.latest()
            if f is not None and f.seq != last_seq:
                disp.submit(f)
                last_seq = f.seq
            await asyncio.sleep(0.05)

    async def consumer():
        nonlocal inferences_done
        # Wait one tick before first call so we have a frame to send
        await asyncio.sleep(1.0)
        while not stop.is_set():
            slot = disp.take()
            if slot is None:
                # nothing changed since last dispatch — skip
                print(f"[m5] (no new frame to send)")
            else:
                jpeg, seq = slot
                t0 = time.monotonic()
                try:
                    obs = await inferer.observe(jpeg)
                except Exception as e:
                    print(f"[m5] inference error: {e}", file=sys.stderr)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=INFERENCE_PERIOD)
                    except asyncio.TimeoutError:
                        pass
                    continue
                dt = time.monotonic() - t0
                inferences_done += 1
                print()
                print(
                    f"[m5 #{inferences_done} dt={dt:.2f}s seq={seq} "
                    f"jpeg={len(jpeg)}B  in={obs.input_tokens} "
                    f"out={obs.output_tokens} "
                    f"cache_create={obs.cache_creation_tokens} "
                    f"cache_read={obs.cache_read_tokens}]"
                )
                for line in obs.text.splitlines():
                    print(f"  {line}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=INFERENCE_PERIOD)
            except asyncio.TimeoutError:
                pass

    tasks = [
        asyncio.create_task(glib_pump()),
        asyncio.create_task(producer()),
        asyncio.create_task(consumer()),
    ]

    print(f"[m5] running {DURATION}s — open something on screen, switch windows, type text!")
    deadline = time.monotonic() + DURATION
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    cap.stop()

    s = disp.stats()
    print()
    print(
        f"=== M5 RESULT === captured={cap.frame_count}  "
        f"dispatched={s.dispatched}  consumed={s.consumed}  "
        f"inferences={inferences_done}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
