"""
Claude-vision inference for desktop frames.

The system prompt is stable across calls and is cached with
`cache_control: ephemeral`; only the per-frame image + short instruction vary,
so cache reads should hit on every call after the first.

Opus 4.7 specifics:
  - No temperature / top_p / top_k (would 400).
  - No budget_tokens (use adaptive thinking + effort instead).
  - For per-frame perception, effort='low' is right — we want fast turnaround,
    not deep reasoning. Adaptive thinking is off by default on Opus 4.7, so we
    just don't pass `thinking` at all.
"""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import anthropic

SYSTEM_PROMPT = """You are a desktop observation assistant. The user will send you screenshots of their Linux desktop, captured live by a streaming agent. Your job:

1. Describe what is currently visible — the active window(s), notable UI elements, any text that looks important.
2. Identify actionable elements (buttons, links, form fields) and roughly where they are.
3. Be concise: 3-5 sentences. Quick observations, not essays.

Do not speculate about what the user should do unless asked. Just observe and report."""


_SYSTEM_PROMPT_AGENT_BASE = """You are the sole desktop-control agent. You watch a live stream of a Linux desktop and take actions to accomplish a goal — there is no other agent or executor; every decision is yours.

Each turn you receive a screenshot and a goal. Before acting, LOOK at the screenshot carefully and identify the CURRENT screen state. Then decide what the NEXT action should be to make progress — do not repeat steps that have already succeeded.

Rules:
- Observe first: look at the screenshot and identify what state the desktop is currently in (e.g., "GNOME Activities overview is open with a search bar visible", "desktop is idle", "terminal is open").
- Act on current state: if the screenshot shows a step has already been completed (e.g., GNOME Activities is already open — you see a search/Type-to-search bar at the top), skip that step and do the NEXT one.
- Act decisively: if the goal requires clicking, typing, or pressing a key, do it now — don't wait unless the UI genuinely hasn't loaded yet.
- Prefer one action per turn. A fresh frame will arrive after it executes.
- Use `wait` only when the screen is visibly mid-transition and acting would be premature.
- Keep your text rationale to one sentence."""

_DRY_RUN_NOTE = "\n- Tool calls run in DRY-RUN mode on this system (printed, not actually injected). Be specific anyway: real actuation can be enabled later by swapping the actuator implementation."
_LIVE_NOTE = "\n- Tool calls are LIVE: every action you emit will be injected into the real desktop immediately. Be precise and deliberate."

SYSTEM_PROMPT_AGENT = _SYSTEM_PROMPT_AGENT_BASE + _DRY_RUN_NOTE


AGENT_TOOLS = [
    {
        "name": "click",
        "description": "Click a location on screen. Coordinates are in the JPEG view dimensions stated in the user message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "reason": {"type": "string", "description": "One short phrase explaining why this click."},
            },
            "required": ["x", "y", "reason"],
        },
    },
    {
        "name": "move",
        "description": "Move the pointer without clicking. Useful for hovering to surface a tooltip or menu.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "reason": {"type": "string"},
            },
            "required": ["x", "y", "reason"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the focused field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["text", "reason"],
        },
    },
    {
        "name": "key",
        "description": "Press a key combo, e.g. 'Return', 'Escape', 'ctrl+s', 'super'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "combo": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["combo", "reason"],
        },
    },
    {
        "name": "wait",
        "description": "Take no action this turn. Use when nothing on screen advances the goal yet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


@dataclass
class IntendedAction:
    name: str
    args: dict[str, Any]
    tool_use_id: str


@dataclass
class Decision:
    rationale: str
    actions: list[IntendedAction]
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


@dataclass
class Observation:
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


class Inferer(ABC):
    @abstractmethod
    async def observe(
        self, jpeg_bytes: bytes, *, instruction: Optional[str] = None
    ) -> Observation: ...


def _supports_effort(model: str) -> bool:
    """output_config/effort is an extended-thinking feature — Opus models only."""
    return "opus" in model.lower()


class ClaudeInferer(Inferer):
    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_tokens: int = 512,
        effort: str = "low",
        live_mode: bool = False,
    ):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort if _supports_effort(model) else None
        self._system_prompt_agent = (
            _SYSTEM_PROMPT_AGENT_BASE + (_LIVE_NOTE if live_mode else _DRY_RUN_NOTE)
        )

    async def observe(
        self, jpeg_bytes: bytes, *, instruction: Optional[str] = None
    ) -> Observation:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")
        user_text = instruction or "What is on the screen right now?"

        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            **({"output_config": {"effort": self._effort}} if self._effort else {}),
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        return Observation(
            text=text,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cache_creation_tokens=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        )

    async def decide(
        self,
        jpeg_bytes: bytes,
        *,
        goal: str,
        jpeg_size: tuple[int, int],
    ) -> Decision:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")
        w, h = jpeg_size
        user_text = (
            f"Goal: {goal}\n"
            f"View dimensions: {w}x{h} (JPEG space — your tool-call coords use this)."
        )

        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            **({"output_config": {"effort": self._effort}} if self._effort else {}),
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt_agent,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=AGENT_TOOLS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )

        actions: list[IntendedAction] = []
        text_parts: list[str] = []
        for block in msg.content:
            if block.type == "tool_use":
                actions.append(
                    IntendedAction(
                        name=block.name,
                        args=dict(block.input),
                        tool_use_id=block.id,
                    )
                )
            elif block.type == "text":
                text_parts.append(block.text)

        return Decision(
            rationale=" ".join(text_parts).strip(),
            actions=actions,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cache_creation_tokens=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        )
