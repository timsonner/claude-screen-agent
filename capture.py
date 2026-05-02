"""
Capture module: wraps the GStreamer pipewiresrc -> appsink pipeline and
delivers BGRx NumPy frames.

Caps are pinned to BGRx so we land on system memory (videoconvert before the
caps filter forces a CPU copy if pipewiresrc would otherwise emit DMA-BUF).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from portal_remotedesktop import StreamHandle


@dataclass
class Frame:
    array: np.ndarray         # shape (H, W, 4), dtype uint8, BGRx
    timestamp: float          # time.monotonic() at delivery
    seq: int                  # monotonic counter, useful for dedup logging


class Capture:
    def __init__(self, handle: StreamHandle, *, fps: int = 4, width: int = 1280):
        self._handle = handle
        self._fps = fps
        self._width = width
        Gst.init(None)

        # Compute height that preserves the source aspect ratio. Without an
        # explicit height in the caps, videoscale only scales width and the
        # source height passes through unchanged, geometrically distorting
        # the frame.
        src_w, src_h = handle.stream_props["size"]
        height = round(src_h * width / src_w)
        self._height = height

        # videoconvert before the caps filter pins us to system memory (would
        # otherwise possibly land on DMA-BUF from pipewiresrc).
        desc = (
            f"pipewiresrc fd={handle.pw_fd} path={handle.node_id} do-timestamp=true "
            "! videoconvert "
            "! videoscale "
            "! videorate "
            f"! video/x-raw,format=BGRx,width={width},height={height},framerate={fps}/1 "
            "! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._sink = self._pipeline.get_by_name("sink")
        self._sink.connect("new-sample", self._on_new_sample)

        self._lock = threading.Lock()
        self._latest: Optional[Frame] = None
        self._seq = 0
        self._errors: list[str] = []

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _on_bus_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self._errors.append(f"{err}: {dbg}")
        return True

    def _on_new_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps_struct = sample.get_caps().get_structure(0)
        ok_w, w = caps_struct.get_int("width")
        ok_h, h = caps_struct.get_int("height")
        if not (ok_w and ok_h):
            return Gst.FlowReturn.ERROR
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            # copy detaches the array from GStreamer-owned memory we're about to unmap
            arr = (
                np.frombuffer(mapinfo.data, dtype=np.uint8)
                .reshape((h, w, 4))
                .copy()
            )
        finally:
            buf.unmap(mapinfo)
        with self._lock:
            self._seq += 1
            self._latest = Frame(array=arr, timestamp=time.monotonic(), seq=self._seq)
        return Gst.FlowReturn.OK

    def start(self) -> None:
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to start")

    def stop(self) -> None:
        self._pipeline.set_state(Gst.State.NULL)

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._seq
