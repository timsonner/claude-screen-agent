"""
Actuator: turns the model's tool calls into pointer / keyboard actions.

Two implementations ship:

  - DryRunActuator: prints intended actions; performs no real input injection.
    Used as the default for testing, audit, and observation-only workflows.

  - YdotoolActuator: shells out to `ydotool` over /dev/uinput. Bypasses the
    portal (which on GNOME 50 doesn't grant input devices to unprivileged
    clients — see README "Option B" for setup, including the input-group
    membership and `ydotool.service` user-systemd unit).

Both share the JPEG → source-coordinate mapping in the Actuator ABC: the
model sees scaled-down JPEGs (jpeg_w x jpeg_h) so its tool calls use that
coordinate space; the actuator scales up to the real monitor (src_size).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ActionLog:
    kind: str
    detail: str


class Actuator(ABC):
    def __init__(self, *, jpeg_size: tuple[int, int], src_size: tuple[int, int]):
        self._jpeg_w, self._jpeg_h = jpeg_size
        self._src_w, self._src_h = src_size
        self._sx = self._src_w / self._jpeg_w
        self._sy = self._src_h / self._jpeg_h

    def _src(self, x: int, y: int) -> tuple[int, int]:
        return (round(x * self._sx), round(y * self._sy))

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

    The printed log shows both JPEG-space coords (what the model emitted) and
    the source-space coords a real actuator would land — making it obvious
    where the model meant to click.
    """

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


# Linux evdev keycodes — selection sufficient for the GNOME overview test plus
# common combos. Values mirror /usr/include/linux/input-event-codes.h.
EVDEV_KEYS: dict[str, int] = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "-": 12,
    "equal": 13, "=": 13,
    "backspace": 14,
    "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20,
    "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "[": 26,
    "rightbrace": 27, "]": 27,
    "enter": 28, "return": 28,
    "leftctrl": 29, "ctrl": 29, "control": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34,
    "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, ";": 39,
    "apostrophe": 40, "'": 40,
    "grave": 41, "`": 41,
    "leftshift": 42, "shift": 42,
    "backslash": 43, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48,
    "n": 49, "m": 50,
    "comma": 51, ",": 51,
    "dot": 52, "period": 52, ".": 52,
    "slash": 53, "/": 53,
    "rightshift": 54,
    "leftalt": 56, "alt": 56,
    "space": 57,
    "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63,
    "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "f11": 87, "f12": 88,
    "rightctrl": 97,
    "rightalt": 100,
    "home": 102, "up": 103, "pageup": 104,
    "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109,
    "insert": 110, "delete": 111,
    "leftmeta": 125, "super": 125, "meta": 125, "win": 125,
    "rightmeta": 126,
}

# Maps printable ASCII characters to (evdev_keycode, needs_shift).
# Used by YdotoolActuator.type_text so all typing goes through the same
# ydotool-key path that works on Wayland / GNOME Activities.
_CHAR_KEY: dict[str, tuple[int, bool]] = {}
for _ch, _code in [
    ("a", 30), ("b", 48), ("c", 46), ("d", 32), ("e", 18), ("f", 33),
    ("g", 34), ("h", 35), ("i", 23), ("j", 36), ("k", 37), ("l", 38),
    ("m", 50), ("n", 49), ("o", 24), ("p", 25), ("q", 16), ("r", 19),
    ("s", 31), ("t", 20), ("u", 22), ("v", 47), ("w", 17), ("x", 45),
    ("y", 21), ("z", 44),
]:
    _CHAR_KEY[_ch] = (_code, False)
    _CHAR_KEY[_ch.upper()] = (_code, True)

for _ch, _code in [("1", 2), ("2", 3), ("3", 4), ("4", 5), ("5", 6),
                   ("6", 7), ("7", 8), ("8", 9), ("9", 10), ("0", 11)]:
    _CHAR_KEY[_ch] = (_code, False)

_CHAR_KEY.update({
    " ": (57, False), "\t": (15, False), "\n": (28, False),
    "-": (12, False), "_": (12, True),
    "=": (13, False), "+": (13, True),
    "[": (26, False), "{": (26, True),
    "]": (27, False), "}": (27, True),
    "\\": (43, False), "|": (43, True),
    ";": (39, False), ":": (39, True),
    "'": (40, False), '"': (40, True),
    "`": (41, False), "~": (41, True),
    ",": (51, False), "<": (51, True),
    ".": (52, False), ">": (52, True),
    "/": (53, False), "?": (53, True),
    "!": (2, True),  "@": (3, True),  "#": (4, True),
    "$": (5, True),  "%": (6, True),  "^": (7, True),
    "&": (8, True),  "*": (9, True),  "(": (10, True), ")": (11, True),
})


def _parse_combo(combo: str) -> list[int]:
    """Map e.g. 'ctrl+shift+t' to the list of evdev keycodes [29, 42, 20]."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty key combo: {combo!r}")
    codes: list[int] = []
    for p in parts:
        if p not in EVDEV_KEYS:
            raise KeyError(p)
        codes.append(EVDEV_KEYS[p])
    return codes


class YdotoolActuator(Actuator):
    """Real input injection via the `ydotool` CLI over /dev/uinput.

    Setup prerequisites (verified in __init__):
      - `ydotool` on $PATH                   (sudo apt install ydotool)
      - $XDG_RUNTIME_DIR/.ydotool_socket exists (systemctl --user enable --now ydotool.service)
      - User in `input` group                (sudo usermod -aG input $USER, then logout/login)

    `settle` is a fixed sleep after each subprocess call so the UI has time
    to render before the next observation cycle. Without it, the model can
    re-issue the same key before the compositor has reacted to the previous
    one (e.g. Super pressed twice toggles the overview off).
    """

    BUTTON_CODES = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}

    def __init__(
        self,
        *,
        jpeg_size: tuple[int, int],
        src_size: tuple[int, int],
        settle: float = 0.5,
        timeout: float = 5.0,
    ):
        super().__init__(jpeg_size=jpeg_size, src_size=src_size)
        self._settle = settle
        self._timeout = timeout
        self._verify_setup()

    @staticmethod
    def _verify_setup() -> None:
        if shutil.which("ydotool") is None:
            raise RuntimeError(
                "ydotool not found on PATH — install with: sudo apt install ydotool"
            )
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise RuntimeError("XDG_RUNTIME_DIR not set; cannot locate ydotool socket")
        sock = Path(runtime) / ".ydotool_socket"
        if not sock.exists():
            raise RuntimeError(
                f"ydotool socket not found at {sock} — start the daemon with: "
                "systemctl --user enable --now ydotool.service"
            )

    def _run(self, argv: list[str]) -> str | None:
        """Run a ydotool subcommand. Returns None on success, error text on failure."""
        try:
            subprocess.run(
                argv, check=True, capture_output=True, text=True, timeout=self._timeout
            )
        except subprocess.CalledProcessError as e:
            return (e.stderr.strip() or str(e))[:200]
        except FileNotFoundError:
            return "ydotool binary disappeared mid-run"
        except subprocess.TimeoutExpired:
            return f"ydotool timed out after {self._timeout}s"
        if self._settle > 0:
            time.sleep(self._settle)
        return None

    def click(self, x: int, y: int, button: str = "left") -> ActionLog:
        sx, sy = self._src(x, y)
        button_code = self.BUTTON_CODES.get(button, self.BUTTON_CODES["left"])
        err = self._run(["ydotool", "mousemove", "--absolute", "--", str(sx), str(sy)])
        if err:
            return ActionLog(kind="error", detail=f"mousemove failed: {err}")
        err = self._run(["ydotool", "click", button_code])
        if err:
            return ActionLog(kind="error", detail=f"click failed: {err}")
        return ActionLog(
            kind="click", detail=f"{button} at jpeg=({x},{y}) -> src=({sx},{sy})"
        )

    def move(self, x: int, y: int) -> ActionLog:
        sx, sy = self._src(x, y)
        err = self._run(["ydotool", "mousemove", "--absolute", "--", str(sx), str(sy)])
        if err:
            return ActionLog(kind="error", detail=f"mousemove failed: {err}")
        return ActionLog(kind="move", detail=f"jpeg=({x},{y}) -> src=({sx},{sy})")

    def type_text(self, text: str) -> ActionLog:
        # ydotool injects via uinput. GNOME Shell processes uinput events
        # through libinput for its own compositor surfaces (e.g. the Activities
        # search box), so try this first.  wtype uses zwp_virtual_keyboard_v1,
        # which GNOME/Mutter does NOT expose — so it will always fail on GNOME.
        preview = text if len(text) <= 60 else text[:57] + "..."
        err = self._run(["ydotool", "type", "--key-delay", "50", "--", text])
        if not err:
            return ActionLog(kind="type", detail=repr(preview))

        # wtype works on wlroots-based compositors (Sway, Hyprland…) that
        # expose zwp_virtual_keyboard_v1.  Try it as a fallback.
        if shutil.which("wtype"):
            err2 = self._run(["wtype", text])
            if not err2:
                return ActionLog(kind="type", detail=repr(preview))

        # Last resort: send each character as individual ydotool key events
        # using evdev keycodes.  Works for standard ASCII in regular windows
        # but may not reach compositor overlay surfaces.
        SHIFT = 42
        events: list[str] = []
        for ch in text:
            mapping = _CHAR_KEY.get(ch)
            if mapping is None:
                return ActionLog(kind="error", detail=f"type_text: no keycode for {ch!r}")
            code, needs_shift = mapping
            if needs_shift:
                events += [f"{SHIFT}:1", f"{code}:1", f"{code}:0", f"{SHIFT}:0"]
            else:
                events += [f"{code}:1", f"{code}:0"]
        err3 = self._run(["ydotool", "key", "--key-delay", "50", *events])
        if err3:
            return ActionLog(kind="error", detail=f"type failed: {err3}")
        return ActionLog(kind="type", detail=repr(preview))

    def key(self, combo: str) -> ActionLog:
        try:
            codes = _parse_combo(combo)
        except (KeyError, ValueError) as e:
            return ActionLog(kind="error", detail=f"unknown key in combo {combo!r}: {e}")
        # Press in given order, release in reverse — matches normal modifier semantics.
        # --key-delay 80 gives the compositor 80 ms to process each event before the
        # next one arrives.  Without it, Super:1 and Super:0 land so close together
        # that GNOME toggles Activities open then immediately closed in one cycle.
        events = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        err = self._run(["ydotool", "key", "--key-delay", "80", *events])
        if err:
            return ActionLog(kind="error", detail=f"key failed: {err}")
        return ActionLog(kind="key", detail=combo)
