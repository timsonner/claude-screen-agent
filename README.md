# screen-agent

A Python prototype that gives Claude direct access to a live stream of the desktop, so it can observe what's on screen, reason about it, and decide on actions — closing the observe → infer → act loop on Wayland.

Built milestone-by-milestone (M0–M6). Verified on Ubuntu 26.04 LTS / GNOME 50 / Wayland.

## What it does

```
xdg-desktop-portal (RemoteDesktop / ScreenCast)
        │
        ▼  PipeWire fd + node id
   pipewiresrc ─► videoconvert ─► videoscale (1280×800) ─► appsink
        │                                                    │
        │                                                    ▼ NumPy frame (BGRx)
        │                                                    │
        ▼                                              perceptual-hash change gate
  GStreamer pipeline                                          │
                                                              ▼ JPEG bytes
                                                        Claude vision
                                                       (Opus 4.7, tools)
                                                              │
                                                              ▼ tool calls
                                                     DryRunActuator (prints)
                                                       — or real injection,
                                                          when enabled
```

Capture is portal-mediated, so there's no X11/screenshot fallback — it runs as a normal user-space client and respects GNOME's consent model. The screencast token persists across runs (no consent dialog after the first).

## Files

| File | Purpose |
|---|---|
| `portal_remotedesktop.py` | D-Bus dance for `org.freedesktop.portal.{RemoteDesktop,ScreenCast}` — opens a session, returns a PipeWire fd + node id. Two entry points: `open_session()` (combined RemoteDesktop+ScreenCast for input grants) and `open_screencast_session()` (ScreenCast-only, persistable). |
| `capture.py` | GStreamer `pipewiresrc → appsink` pipeline. Delivers BGRx NumPy frames at the requested fps and aspect-correct dimensions. |
| `dispatcher.py` | Single-slot latest-frame queue, perceptual-hash change gate, JPEG encode. Drops frames identical to the last one dispatched, so static desktops don't burn API calls. |
| `inference.py` | Claude vision wrapper. `observe()` returns text only (M5); `decide(goal=…)` returns tool calls (M6) using `click`/`move`/`type_text`/`key`/`wait`. |
| `actuator.py` | Abstract `Actuator` + `DryRunActuator` (prints intended actions, maps JPEG-space coords back to source-space). Real input-injection actuators slot in here. |
| `agent.py` | End-to-end loop: capture → dispatcher → Claude (with tools) → actuator. Configurable goal + duration via argv. |
| `capture_m1.py` `capture_m2.py` `dispatcher_m4.py` `inference_m5.py` | Per-milestone demos kept around for reference. |
| `m6_diagnose.py` `m6_eis_probe.py` | Diagnostics for the input-grant problem on GNOME 50 (kept for documentation). |

## Setup

System packages (Ubuntu / Debian):

```sh
sudo apt install -y \
    python3.14-venv python3-pip \
    pipewire wireplumber gstreamer1.0-pipewire \
    python3-gi gir1.2-gst-plugins-base-1.0
```

Python venv (must include system site-packages so we get `gi`/`Gst` from apt):

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -U pip
.venv/bin/pip install dbus-next numpy anthropic
```

Verify the GStreamer pipewire plugin is present:

```sh
gst-inspect-1.0 pipewiresrc | head -3   # should print "Factory Details: ..."
```

## Running

Set your API key on the same line as the Python invocation (each `!` shell call is a fresh subshell):

```sh
ANTHROPIC_API_KEY=sk-ant-... PYTHONUNBUFFERED=1 .venv/bin/python agent.py
```

Optional CLI args: `python agent.py [duration_seconds] [goal_string]`. Default is 45 s with a generic "click the most prominent actionable element" goal.

The first run prompts for screen-share consent. Subsequent runs reuse the saved token at `~/.cache/screen-agent/restore_token`.

### Per-milestone demos

| Demo | What to expect |
|---|---|
| `python capture_m1.py` | Writes ~8 JPEG frames to `/tmp/frame_*.jpg` over 2 s. Open one to verify the capture is real. |
| `python capture_m2.py` | Prints fps/shape/mean for ~5 s of frames. Frames land in NumPy at 1280×800 BGRx. |
| `python dispatcher_m4.py` | 15 s loop showing dispatched-vs-dropped counters. Move a window to see the change gate behave. |
| `python inference_m5.py` | 30 s loop calling Claude every ~4 s with text-only observation. |
| `python agent.py` | Full observe→decide→act loop with tool-using Claude (dry-run actuation). |

## Limitations and how to enable real actuation

**Capture and inference are fully working.** Input injection (M6) is dry-run on this system because GNOME 50 doesn't grant input devices to unprivileged portal clients:

- `RemoteDesktop.SelectDevices(types=keyboard|pointer)` returns `devices_bitmask=0` after `Start`.
- `RemoteDesktop.ConnectToEIS` returns `org.freedesktop.DBus.Error.AccessDenied: Invalid session`.
- The screen-share consent dialog has no input-grant toggle on this GNOME version.

To turn dry-run into real injection, pick one of:

### Option A — `ydotool` over `/dev/uinput` (simplest)

1. `sudo apt install ydotool ydotoold`
2. `sudo usermod -aG input $USER` and **log out / log back in** (group changes don't apply to existing sessions).
3. `systemctl --user enable --now ydotoold`
4. Implement a `YdotoolActuator(Actuator)` in `actuator.py` that shells out to `ydotool mousemove --absolute -- X Y` / `click 0xC0` / `type` / `key`. (`ydotool` uses uinput keycodes; map from the model's `combo` strings.)
5. In `agent.py`, swap `DryRunActuator(...)` for `YdotoolActuator(...)`.

This bypasses the portal entirely and works on any Wayland compositor. Tradeoff: gives the daemon (and anything that talks to it) full keyboard/mouse access — the portal's per-app consent model is gone.

### Option B — libei via `ConnectToEIS` (proper)

1. Enable Remote Desktop in **Settings → System → Remote Desktop**, set "Remote Control" on (not view-only), set a password.
2. `systemctl --user start gnome-remote-desktop.service`
3. The portal's input grant should now succeed (`devices_bitmask` non-zero, `ConnectToEIS` returns an fd).
4. Build a `EisActuator(Actuator)`. There are no Python bindings for libei in apt — use `ctypes` against `libei.so.1` (~half a day of work for the device-creation handshake plus pointer/keyboard frames).

This keeps the portal's consent model. Heavier setup, but cleaner long-term.

### Option C — gnome-remote-desktop as the broker

Run our agent as a client of `gnome-remote-desktop` (over RDP/VNC) instead of as a portal client. Different architecture; not pursued.

## Other known issues / future work

- **Prompt caching doesn't hit** (`cache_read_input_tokens=0`). Opus 4.7's minimum cacheable prefix is 4096 tokens; our system prompt is \~250. Pad with examples or accept full input cost (\~$0.018/decision at current sizes).
- **Coordinate precision.** The model sees a 1280×800 downscale; clicks land within \~30 px of the intended target. Bumping to 2400×1500 (within Opus 4.7's 2576 px max) and using its high-res mode would tighten this.
- **Single monitor.** `Start` may return multiple streams on multi-monitor setups; we use `streams[0]` only.
- **Headless/CI** is not set up. Would use `weston --backend=headless` + `xdg-desktop-portal-wlr` (different portal backend than GNOME's).

## Privacy

The capture pipeline can see anything visible on the desktop, including passwords and private content. JPEGs are sent to Anthropic's API; nothing is written to disk by default. GNOME's "screen is being shared" indicator is intentionally left visible — don't suppress it.
