"""
End-to-end agent: capture -> dispatcher -> Claude (with tools) -> DryRunActuator.

Each cycle:
  1. The dispatcher hands us the freshest changed frame.
  2. Claude observes it and either calls a tool (click/move/type/key/wait)
     or returns text only.
  3. The actuator executes each tool call. DryRunActuator only prints; a
     real input-injection actuator can be slotted in once the platform
     permits it (see actuator.py header for the GNOME 50 limitations
     hit during M6).

Run with:
  ANTHROPIC_API_KEY=... .venv/bin/python agent.py [duration_seconds] [goal]
"""

from __future__ import annotations

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
DECISION_PERIOD = 5.0
DEFAULT_GOAL = (
    "Watch the screen. Identify the single most prominent actionable element "
    "(button, link, dock icon, menu item) currently visible and click it. "
    "If nothing actionable is visible, use the wait tool."
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


async def _main(duration: float, goal: str) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY before running.", file=sys.stderr)
        return 2

    handle = await open_screencast_session()
    print(f"[agent] session up — node_id={handle.node_id}")
    print(f"[agent] goal: {goal}")

    cap = Capture(handle, fps=4, width=1280)
    cap.start()

    src_w, src_h = handle.stream_props["size"]
    actuator: Actuator = DryRunActuator(
        jpeg_size=(cap._width, cap._height),
        src_size=(src_w, src_h),
    )

    disp = Dispatcher(hamming_threshold=5, jpeg_quality=80)
    inferer = ClaudeInferer(effort="low", max_tokens=768)

    stop = asyncio.Event()
    decisions = 0

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

    async def decision_loop():
        nonlocal decisions
        await asyncio.sleep(1.5)  # let one frame land first
        while not stop.is_set():
            slot = disp.take()
            if slot is None:
                print("[agent] (no new frame)")
            else:
                jpeg, seq = slot
                t0 = time.monotonic()
                try:
                    decision = await inferer.decide(
                        jpeg, goal=goal, jpeg_size=(cap._width, cap._height)
                    )
                except Exception as e:
                    print(f"[agent] decide error: {e}", file=sys.stderr)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=DECISION_PERIOD)
                    except asyncio.TimeoutError:
                        pass
                    continue
                dt = time.monotonic() - t0
                decisions += 1
                print()
                print(
                    f"[agent #{decisions} dt={dt:.2f}s seq={seq} "
                    f"in={decision.input_tokens} out={decision.output_tokens} "
                    f"cache_read={decision.cache_read_tokens}]"
                )
                if decision.rationale:
                    print(f"  rationale: {decision.rationale}")
                if not decision.actions:
                    print("  (no tool calls this turn)")
                else:
                    for a in decision.actions:
                        log = _execute(a, actuator)
                        reason = a.args.get("reason", "")
                        print(f"  → {log.kind}: {log.detail}")
                        if reason:
                            print(f"     reason: {reason}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=DECISION_PERIOD)
            except asyncio.TimeoutError:
                pass

    tasks = [
        asyncio.create_task(glib_pump()),
        asyncio.create_task(producer()),
        asyncio.create_task(decision_loop()),
    ]

    print(f"[agent] running {duration}s — keep useful UI visible (dock, browser, etc.)")
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    cap.stop()

    s = disp.stats()
    print()
    print(
        f"=== AGENT RESULT === captured={cap.frame_count}  "
        f"dispatched={s.dispatched}  consumed={s.consumed}  "
        f"decisions={decisions}"
    )
    return 0


if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION
    goal = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_GOAL
    sys.exit(asyncio.run(_main(duration, goal)))
