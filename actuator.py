"""
Actuator: turns the model's tool calls into pointer / keyboard actions.

GNOME 50 on this system denies input grants to unprivileged portal clients
(devices_bitmask=0, ConnectToEIS returns AccessDenied). The realistic real-
actuation paths are:
  - libei via ConnectToEIS — would work if the portal granted devices, but it
    doesn't on this OS without gnome-remote-desktop running as the privileged
    client.
  - ydotool over /dev/uinput — works on Wayland but needs the user in the
    `input` group + a running ydotoold daemon.

DryRunActuator implements the Actuator interface and just prints actions, so
the full observe -> decide -> act loop can be exercised end-to-end without
the privilege setup. A real actuator can be slotted in by implementing the
same five methods.

Coordinate space: the model sees JPEGs scaled to (jpeg_w x jpeg_h) so its
tool calls use that coordinate space. The actuator maps to source-space
coordinates (the actual monitor) using `src_size / jpeg_size`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ActionLog:
    kind: str
    detail: str


class Actuator(ABC):
    @abstractmethod
    def click(self, x: int, y: int, button: str = "left") -> ActionLog: ...
    @abstractmethod
    def move(self, x: int, y: int) -> ActionLog: ...
    @abstractmethod
    def type_text(self, text: str) -> ActionLog: ...
    @abstractmethod
    def key(self, combo: str) -> ActionLog: ...


class DryRunActuator(Actuator):
    """Prints intended actions; performs no real input injection.

    Maps JPEG-space coordinates to source-space so the printed log shows
    both — making it obvious where the model meant to click, and where a
    real actuator would land.
    """

    def __init__(self, *, jpeg_size: tuple[int, int], src_size: tuple[int, int]):
        self._jpeg_w, self._jpeg_h = jpeg_size
        self._src_w, self._src_h = src_size
        self._sx = self._src_w / self._jpeg_w
        self._sy = self._src_h / self._jpeg_h

    def _src(self, x: int, y: int) -> tuple[int, int]:
        return (round(x * self._sx), round(y * self._sy))

    def click(self, x: int, y: int, button: str = "left") -> ActionLog:
        sx, sy = self._src(x, y)
        return ActionLog(
            kind="click",
            detail=f"{button} at jpeg=({x},{y}) -> src=({sx},{sy}) [DRY-RUN]",
        )

    def move(self, x: int, y: int) -> ActionLog:
        sx, sy = self._src(x, y)
        return ActionLog(
            kind="move",
            detail=f"jpeg=({x},{y}) -> src=({sx},{sy}) [DRY-RUN]",
        )

    def type_text(self, text: str) -> ActionLog:
        preview = text if len(text) <= 60 else text[:57] + "..."
        return ActionLog(kind="type", detail=f"{preview!r} [DRY-RUN]")

    def key(self, combo: str) -> ActionLog:
        return ActionLog(kind="key", detail=f"{combo} [DRY-RUN]")
