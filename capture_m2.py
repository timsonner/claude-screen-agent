"""
M2 demo: open portal session, run Capture for ~5 seconds, print frame stats.

Verifies that frames land in NumPy with the expected shape and that the mean
pixel value updates as desktop content changes.
"""

from __future__ import annotations

import asyncio
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from capture import Capture
from portal_remotedesktop import open_screencast_session

CAPTURE_SECONDS = 5.0


async def _main() -> int:
    handle = await open_screencast_session()
    print(f"[m2] session up — node_id={handle.node_id} pw_fd={handle.pw_fd}")

    cap = Capture(handle, fps=4, width=1280)
    cap.start()

    main_ctx = GLib.MainContext.default()
    last_seq = 0
    elapsed = 0.0
    tick = 0.5
    while elapsed < CAPTURE_SECONDS:
        # iterate GLib so the streaming thread's bus messages get drained
        while main_ctx.iteration(False):
            pass
        await asyncio.sleep(tick)
        elapsed += tick
        f = cap.latest()
        if f is None:
            print(f"[m2 t={elapsed:.1f}s] no frames yet")
            continue
        delta = f.seq - last_seq
        last_seq = f.seq
        mean = float(f.array[..., :3].mean())  # mean of BGR (drop the X channel)
        print(
            f"[m2 t={elapsed:.1f}s] frames={f.seq} (+{delta} this tick)  "
            f"shape={f.array.shape}  dtype={f.array.dtype}  mean_bgr={mean:.1f}"
        )

    cap.stop()
    if cap.errors:
        print("[m2] ERRORS:", cap.errors, file=sys.stderr)
        return 1

    total = cap.frame_count
    expected = int(CAPTURE_SECONDS * 4)  # fps=4
    print()
    print(f"=== M2 RESULT === frames={total} (expected ~{expected})")
    return 0 if total >= expected - 2 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
