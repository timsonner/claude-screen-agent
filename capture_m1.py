"""
M1: One frame to disk.

Opens a portal session (via portal_remotedesktop.open_session) and runs a short
GStreamer pipeline that writes JPEG frames to /tmp/frame_NNNNN.jpg. Validates
that pipewiresrc can consume the negotiated PipeWire node and emit decodable
images.

The asyncio loop is held open during capture so the D-Bus session (and thus the
PipeWire screencast node) stays alive.
"""

from __future__ import annotations

import asyncio
import glob
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from portal_remotedesktop import open_session  # noqa: E402

CAPTURE_SECONDS = 2.0
OUTPUT_GLOB = "/tmp/frame_*.jpg"


def _build_pipeline(pw_fd: int, node_id: int) -> Gst.Pipeline:
    # pipewiresrc consumes the portal-issued fd + node id.
    # videorate caps to 4 fps so we don't write 60+ files in 2s.
    desc = (
        f"pipewiresrc fd={pw_fd} path={node_id} do-timestamp=true "
        "! videoconvert "
        "! videorate ! video/x-raw,framerate=4/1 "
        "! jpegenc quality=80 "
        "! multifilesink location=/tmp/frame_%05d.jpg"
    )
    print(f"[gst] pipeline: {desc}")
    pipeline = Gst.parse_launch(desc)
    return pipeline


async def _main() -> int:
    # Clean prior run's output so we know what came from this invocation.
    for f in glob.glob(OUTPUT_GLOB):
        os.unlink(f)

    Gst.init(None)
    handle = await open_session()
    print(f"[m1] session up — node_id={handle.node_id} pw_fd={handle.pw_fd}")

    pipeline = _build_pipeline(handle.pw_fd, handle.node_id)

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    errors: list[str] = []

    def on_message(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            errors.append(f"{err}: {dbg}")
            print(f"[gst] ERROR: {err}: {dbg}", file=sys.stderr)
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            print(f"[gst] WARN: {err}: {dbg}", file=sys.stderr)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == pipeline:
            old, new, _ = msg.parse_state_changed()
            print(f"[gst] state {old.value_nick} -> {new.value_nick}")
        return True

    bus.connect("message", on_message)

    ret = pipeline.set_state(Gst.State.PLAYING)
    print(f"[gst] set_state(PLAYING) -> {ret.value_nick}")
    if ret == Gst.StateChangeReturn.FAILURE:
        return 2

    # Drive the GLib main context briefly while pipeline runs.
    deadline = time.monotonic() + CAPTURE_SECONDS
    main_ctx = GLib.MainContext.default()
    while time.monotonic() < deadline:
        # iteration drains any pending bus messages
        while main_ctx.iteration(False):
            pass
        await asyncio.sleep(0.05)
        if errors:
            break

    pipeline.set_state(Gst.State.NULL)

    files = sorted(glob.glob(OUTPUT_GLOB))
    print()
    print(f"=== M1 RESULT === wrote {len(files)} JPEG file(s) in {CAPTURE_SECONDS}s")
    for f in files[:5]:
        print(f"  {f}  ({os.path.getsize(f)} bytes)")
    if len(files) > 5:
        print(f"  ... and {len(files) - 5} more")
    return 0 if files and not errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
