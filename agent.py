"""
End-to-end agent: capture -> dispatcher -> Claude -> (optional) actuator.

Two modes:

  --mode observe (text-only narration)
    Each cycle, Claude describes what's currently on screen. No tool calls,
    no actuator. Useful for live narration, activity logging, monitoring,
    accessibility, and audit / review of what the agent perceives.

  --mode act (default — tool-use loop)
    Claude decides actions via tool calls (click / move / type_text / key /
    wait). The configured actuator handles each action. Default actuator is
    DryRunActuator (prints intended actions); swap in a real one in code if
    you want hardware injection.

Run:
  ANTHROPIC_API_KEY=... .venv/bin/python agent.py [--mode observe|act] \
      [--duration 45] [--prompt "..."] [--period 5]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from actuator import Actuator, ActionLog, DryRunActuator
from capture import Capture
from dispatcher import Dispatcher
from inference import ClaudeInferer, IntendedAction
from portal_remotedesktop import open_screencast_session

DEFAULT_DURATION = 45.0
DEFAULT_PERIOD = 5.0

DEFAULT_ACT_PROMPT = (
    "Watch the screen. Identify the single most prominent actionable element "
    "(button, link, dock icon, menu item) currently visible and click it. "
    "If nothing actionable is visible, use the wait tool."
)
DEFAULT_OBSERVE_PROMPT = (
    "Briefly describe what is on the screen now — the active window(s), any "
    "prominent UI elements, and any text that looks important. 3–5 sentences."
)


def _execute(action: IntendedAction, actuator: Actuator) -> ActionLog:
    name = action.name
    args = action.args
    if name == "click":
        return actuator.click(int(args["x"]), int(args["y"]), args.get("button", "left"))
    if name == "move":
        return actuator.move(int(args["x"]), int(args["y"]))
    if name == "type_text":
        return actuator.type_text(str(args["text"]))
    if name == "key":
        return actuator.key(str(args["combo"]))
    if name == "wait":
        return ActionLog(kind="wait", detail=args.get("reason", ""))
    return ActionLog(kind="unknown", detail=f"{name} args={args}")


async def _main(mode: str, duration: float, prompt: str, period: float) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY before running.", file=sys.stderr)
        return 2

    handle = await open_screencast_session()
    print(f"[agent] mode={mode}  session up — node_id={handle.node_id}")
    print(f"[agent] prompt: {prompt}")

    cap = Capture(handle, fps=4, width=1280)
    cap.start()

    src_w, src_h = handle.stream_props["size"]
    jpeg_size = (cap._width, cap._height)
    actuator: Actuator | None = None
    if mode == "act":
        actuator = DryRunActuator(jpeg_size=jpeg_size, src_size=(src_w, src_h))

    disp = Dispatcher(hamming_threshold=5, jpeg_quality=80)
    inferer = ClaudeInferer(effort="low", max_tokens=768 if mode == "act" else 512)

    stop = asyncio.Event()
    cycles = 0

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
        nonlocal cycles
        await asyncio.sleep(1.5)  # let one frame land first
        while not stop.is_set():
            slot = disp.take()
            if slot is None:
                print("[agent] (no new frame)")
            else:
                jpeg, seq = slot
                t0 = time.monotonic()
                try:
                    if mode == "act":
                        result = await inferer.decide(
                            jpeg, goal=prompt, jpeg_size=jpeg_size
                        )
                    else:
                        result = await inferer.observe(jpeg, instruction=prompt)
                except Exception as e:
                    print(f"[agent] inference error: {e}", file=sys.stderr)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=period)
                    except asyncio.TimeoutError:
                        pass
                    continue
                dt = time.monotonic() - t0
                cycles += 1
                print()
                print(
                    f"[{mode} #{cycles} dt={dt:.2f}s seq={seq} "
                    f"in={result.input_tokens} out={result.output_tokens} "
                    f"cache_read={result.cache_read_tokens}]"
                )
                if mode == "observe":
                    for line in result.text.splitlines():
                        print(f"  {line}")
                else:
                    if result.rationale:
                        print(f"  rationale: {result.rationale}")
                    if not result.actions:
                        print("  (no tool calls this turn)")
                    else:
                        for a in result.actions:
                            log = _execute(a, actuator)  # type: ignore[arg-type]
                            reason = a.args.get("reason", "")
                            print(f"  → {log.kind}: {log.detail}")
                            if reason:
                                print(f"     reason: {reason}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass

    tasks = [
        asyncio.create_task(glib_pump()),
        asyncio.create_task(producer()),
        asyncio.create_task(consumer()),
    ]

    print(f"[agent] running {duration}s")
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    cap.stop()

    s = disp.stats()
    print()
    print(
        f"=== AGENT RESULT === mode={mode}  captured={cap.frame_count}  "
        f"dispatched={s.dispatched}  consumed={s.consumed}  cycles={cycles}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live screen-stream Claude vision agent (observe or act mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=["observe", "act"],
        default="act",
        help="observe: describe screen each cycle (no actions). "
        "act: use tools to decide actions (default).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help=f"run duration in seconds (default {DEFAULT_DURATION})",
    )
    p.add_argument(
        "--period",
        type=float,
        default=DEFAULT_PERIOD,
        help=f"seconds between Claude calls (default {DEFAULT_PERIOD})",
    )
    p.add_argument(
        "--prompt",
        help="Mode-appropriate instruction for Claude (uses a sensible default if omitted).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    prompt = args.prompt or (
        DEFAULT_ACT_PROMPT if args.mode == "act" else DEFAULT_OBSERVE_PROMPT
    )
    sys.exit(asyncio.run(_main(args.mode, args.duration, prompt, args.period)))
