"""
Dispatcher: gates frames between Capture and inference.

Two filters before a frame becomes work for the model:
    1. Perceptual hash — drop frames that are essentially identical to the
       last *dispatched* frame (comparing against last-dispatched, not
       last-seen, is drift-resistant).
    2. Single-slot queue — only the most recent eligible frame is held; new
       frames overwrite older ones, so inference always operates on the
       freshest image when it asks for one.

JPEG-encode happens after the change gate so we don't pay the encode cost on
frames the model would never see.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

from capture import Frame


@dataclass
class DispatchStats:
    dispatched: int
    dropped_unchanged: int
    dropped_saturation: int
    consumed: int


def _phash(bgrx: np.ndarray, side: int = 8) -> int:
    """64-bit (default) mean-hash of a BGRx frame.

    Picks `side*side` evenly-spaced samples from the grayscale-converted
    image, sets each bit to 1 iff its sample is >= the sample mean. Cheap,
    aliased, but good enough for "is this essentially the same screen?".
    """
    gray = bgrx[..., :3].mean(axis=2)
    h, w = gray.shape
    yi = np.linspace(0, h - 1, side).astype(np.int32)
    xi = np.linspace(0, w - 1, side).astype(np.int32)
    sample = gray[np.ix_(yi, xi)]
    bits = (sample >= sample.mean()).astype(np.uint8).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _to_jpeg(bgrx: np.ndarray, quality: int = 80) -> bytes:
    # BGRx -> RGB; PIL doesn't speak BGR.
    rgb = bgrx[..., [2, 1, 0]]
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class Dispatcher:
    def __init__(
        self,
        *,
        hamming_threshold: int = 5,
        jpeg_quality: int = 80,
        verbose: bool = False,
    ):
        self._threshold = hamming_threshold
        self._jpeg_quality = jpeg_quality
        self._verbose = verbose
        self._lock = threading.Lock()
        self._slot: tuple[bytes, int] | None = None  # (jpeg_bytes, seq)
        self._last_dispatched_hash: int | None = None
        self._stats = DispatchStats(0, 0, 0, 0)

    def submit(self, frame: Frame) -> None:
        h = _phash(frame.array)
        if (
            self._last_dispatched_hash is not None
            and _hamming(h, self._last_dispatched_hash) <= self._threshold
        ):
            self._stats.dropped_unchanged += 1
            return
        dist = (
            _hamming(h, self._last_dispatched_hash)
            if self._last_dispatched_hash is not None
            else "init"
        )
        jpeg = _to_jpeg(frame.array, quality=self._jpeg_quality)
        with self._lock:
            if self._slot is not None:
                self._stats.dropped_saturation += 1
            self._slot = (jpeg, frame.seq)
        self._last_dispatched_hash = h
        self._stats.dispatched += 1
        if self._verbose:
            print(f"[disp] seq={frame.seq} dispatched  Δhash={dist}")

    def take(self) -> tuple[bytes, int] | None:
        """Return the freshest dispatchable frame, clearing the slot.

        Resets the perceptual-hash baseline when a frame is consumed so the
        next frame submitted after a cycle always gets through — even if the
        screen looks identical to the last consumed frame.  Without this,
        once the screen stabilises (e.g. an Activities overview sitting open)
        every subsequent frame is silently dropped and the agent loops on
        "(no new frame)" forever.
        """
        with self._lock:
            slot = self._slot
            self._slot = None
        if slot is not None:
            self._stats.consumed += 1
            self._last_dispatched_hash = None  # force the next frame through
        return slot

    def stats(self) -> DispatchStats:
        return DispatchStats(**self._stats.__dict__)
