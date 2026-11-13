# screen-agent

screen-agent lets a Claude model see a live Wayland desktop and either describe it or choose the next pointer and keyboard action.

Capture stays a normal user-space client of xdg-desktop-portal. The portal returns a PipeWire file descriptor and node id. GStreamer reads that stream, a small dispatcher keeps only frames that changed, and those JPEGs go to the Anthropic API. In the default mode the program prints the actions Claude asks for. An optional ydotool actuator can inject them.

## Prerequisites

- A Linux desktop session with PipeWire and the session bus name `org.freedesktop.portal.Desktop` (xdg-desktop-portal).
- Python 3.10 or newer. `dispatcher.py` calls `int.bit_count()`.
- PyGObject (`gi`) with the GStreamer 1.0 typelib. `agent.py` imports `Gst` and `GLib` at startup, including for `--help`.
- GStreamer elements used by the pipelines: `pipewiresrc`, `videoconvert`, `videoscale`, `videorate`, `appsink`, and, for `capture_m1.py` only, `jpegenc` and `multifilesink`.
- `ANTHROPIC_API_KEY` for `agent.py` and `inference_m5.py`.
- For real input (`--actuator ydotool`): the `ydotool` binary, the user unit `ydotool.service`, the socket `$XDG_RUNTIME_DIR/.ydotool_socket`, and membership in the `input` group. `YdotoolActuator` checks the binary and the socket when it is constructed.

On Debian or Ubuntu, the matching packages are:

```sh
sudo apt install -y \
  python3-venv \
  python3-pip \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-pipewire \
  pipewire \
  xdg-desktop-portal
```

Confirm the PipeWire source element is visible to GStreamer:

```sh
gst-inspect-1.0 pipewiresrc
```

## Install

There is no `requirements.txt` or `pyproject.toml`. PyGObject comes from the system packages above, so the virtual environment must see system site-packages. The pip packages are the ones imported by the scripts: `dbus-next`, `numpy`, `anthropic`, and `pillow` (`from PIL import Image` in `dispatcher.py`).

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -U pip
.venv/bin/pip install dbus-next numpy anthropic pillow
```

## Build

There is no compile or package step. Run the scripts from this directory with the virtual environment's Python.

## Run

Export an API key in the same shell, then start the agent. The first screencast start raises the desktop consent dialog. Later runs reuse a restore token from `SCREEN_AGENT_TOKEN_PATH`, or from `~/.cache/screen-agent/restore_token` when that variable is unset (`portal_remotedesktop.py`).

```sh
export ANTHROPIC_API_KEY=your-key
export PYTHONUNBUFFERED=1
.venv/bin/python agent.py --help
.venv/bin/python agent.py --mode observe --duration 30
.venv/bin/python agent.py --mode act --duration 45
```

`agent.py` exits 2 when `ANTHROPIC_API_KEY` is unset, and exits 3 when `--actuator ydotool` is selected but `ydotool` or its socket is missing. A finished run exits 0.

These commands need a logged-in desktop session. They open a portal screencast and, except `--help`, call the Anthropic API.

## Test

This repository has no test runner, pytest config, or tests directory.

Check that every module parses:

```sh
python3 -m compileall -q .
```

The milestone scripts are manual checks. Each one that captures the screen needs the portal session from Prerequisites. `inference_m5.py` also needs `ANTHROPIC_API_KEY`.

```sh
.venv/bin/python portal_remotedesktop.py
.venv/bin/python capture_m1.py
.venv/bin/python capture_m2.py
.venv/bin/python dispatcher_m4.py
.venv/bin/python inference_m5.py
.venv/bin/python m6_diagnose.py
.venv/bin/python m6_eis_probe.py
```

What each script does:

| Script | Behavior |
| --- | --- |
| `portal_remotedesktop.py` | Opens a combined RemoteDesktop and ScreenCast session and prints node id, PipeWire fd, session handle, restore token, and the devices bitmask. |
| `capture_m1.py` | Records about 2 seconds and writes `/tmp/frame_*.jpg`. Exit 0 when at least one JPEG was written and the pipeline reported no error. |
| `capture_m2.py` | Runs `Capture` for about 5 seconds at 4 fps and 1280 px wide, and prints shape, dtype, and mean. Exit 0 when the frame count is within 2 of the expected count and there are no pipeline errors. |
| `dispatcher_m4.py` | Runs capture through `Dispatcher` for 15 seconds and prints dispatched, consumed, and dropped counts. |
| `inference_m5.py` | For 30 seconds, sends a changed frame to Claude about every 4 seconds and prints the text reply. Exit 2 when the API key is unset. |
| `m6_diagnose.py` | Opens the combined session and prints whether keyboard, pointer, and touchscreen bits were granted. |
| `m6_eis_probe.py` | Calls `RemoteDesktop.ConnectToEIS` and prints the fd or the D-Bus error. Exit 1 on `DBusError`. |

`portal_remotedesktop.py` uses the combined RemoteDesktop session, which does not request `persist_mode` (the portal rejects persist on that session). `agent.py` and the capture demos from M2 onward call `open_screencast_session()`, which sets `persist_mode` 2 and stores the restore token.

## Usage

`agent.py` is the end-to-end loop: screencast, capture, dispatcher, Claude, then an actuator in `act` mode.

```sh
.venv/bin/python agent.py --mode observe --duration 30 --period 3
.venv/bin/python agent.py --mode act --actuator dry-run --duration 45 --max-actions 10
.venv/bin/python agent.py --mode observe --prompt "Watch for an error dialog. If you see one, describe it. Otherwise say no alerts."
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mode` | `act` | `observe` or `act`. `observe` asks Claude for text only. `act` asks for tool calls and runs the actuator. |
| `--duration` | `45` | Seconds to run. |
| `--period` | `3` | Seconds between Claude calls. |
| `--prompt` | mode default | Instruction sent each cycle. See below. |
| `--actuator` | `dry-run` | `dry-run` or `ydotool`. Used only in `act` mode. |
| `--max-actions` | `10` | Stop after this many tool calls in `act` mode. |
| `--settle` | `0.3` | Seconds `YdotoolActuator` sleeps after each `ydotool` call. Ignored by `DryRunActuator`. |
| `--save-frames DIR` | off | Write each JPEG Claude consumes to `DIR/frame_NNNNNN.jpg`, and turn on dispatcher hash logging. |
| `--model` | `claude-opus-4-7` | Model id passed to the Anthropic API. `--help` also names `claude-haiku-4-5` and `claude-sonnet-4-5`. |
| `--effort` | `medium` for `act`, `low` for `observe` | One of `low`, `medium`, `high`, `xhigh`. Sent only when the model id contains `opus`. Other model ids omit it. |
| `--width` | `1280` | Frame width in pixels. Height follows the source aspect ratio. Capture fps is fixed at 4. |

Default `act` prompt: click the single most prominent actionable element, or call `wait` when nothing actionable is visible.

Default `observe` prompt: a short description of the active windows, prominent UI, and important text.

In `act` mode the tools are `click` (`x`, `y`, optional `button` `left`/`right`/`middle`), `move` (`x`, `y`), `type_text` (`text`), `key` (`combo`), and `wait`. Each tool requires `reason`. Coordinates are in the JPEG width and height, and both actuators scale them to the source size from the portal stream. Only `streams[0]` is used.

`DryRunActuator` prints the action and the mapped coordinates. It does not move the pointer or type.

### ydotool actuator

Install and start the daemon, then log out and back in after the group change so the `input` group applies:

```sh
sudo apt install ydotool
sudo usermod -aG input "$USER"
systemctl --user enable --now ydotool.service
```

Tap Super (evdev code 125 in `actuator.py`) to check the daemon before involving Claude:

```sh
ydotool key 125:1 125:0
```

Run the agent with real injection:

```sh
export ANTHROPIC_API_KEY=your-key
.venv/bin/python agent.py \
  --mode act \
  --actuator ydotool \
  --duration 45 \
  --max-actions 5 \
  --settle 0.3 \
  --prompt "Press Super to open Activities, then type edge and press Return."
```

The screencast session is opened before `YdotoolActuator` checks its setup. A missing binary or socket still exits 3 and stops capture.

`type_text` tries `ydotool type` first. If that fails and `wtype` is on `PATH`, it tries `wtype`. Otherwise it sends per-character `ydotool key` events for the ASCII keys listed in `actuator.py`. `key` combos are split on `+` and looked up in `EVDEV_KEYS` (for example `super`, `Return`, `ctrl+shift+t`).

## Layout

| File | Role |
| --- | --- |
| `portal_remotedesktop.py` | D-Bus client for `org.freedesktop.portal.RemoteDesktop` and `org.freedesktop.portal.ScreenCast`. `open_session()` is the combined session. `open_screencast_session()` is ScreenCast only and can persist a restore token. |
| `capture.py` | `pipewiresrc` to `appsink`. Delivers BGRx NumPy frames. |
| `dispatcher.py` | Drops frames whose perceptual hash is within Hamming distance 5 of the last dispatched frame, keeps one pending JPEG, encodes with Pillow at quality 80. |
| `inference.py` | Claude vision calls. `observe()` returns text. `decide()` returns tool calls. |
| `actuator.py` | `Actuator`, `DryRunActuator`, and `YdotoolActuator`. |
| `agent.py` | CLI and the capture, infer, act loop. |
| `capture_m1.py`, `capture_m2.py`, `dispatcher_m4.py`, `inference_m5.py` | Manual milestone demos. |
| `m6_diagnose.py`, `m6_eis_probe.py` | Print portal input-grant and `ConnectToEIS` results. No libei actuator is implemented. |

Frames are sent to the Anthropic API. The agent writes JPEGs to disk only when `--save-frames` is set. `capture_m1.py` always writes `/tmp/frame_*.jpg`.

## License

This repository has no license file.
