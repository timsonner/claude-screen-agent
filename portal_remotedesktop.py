"""
M0: portal handshake.

Walks the xdg-desktop-portal dance for a combined screen-capture + input session:
    RemoteDesktop.CreateSession
    -> RemoteDesktop.SelectDevices
    -> ScreenCast.SelectSources
    -> RemoteDesktop.Start          (single consent dialog)
    -> ScreenCast.OpenPipeWireRemote

Prints (node_id, fd, restore_token) and exits. Subsequent milestones import
`open_session` from this module to drive capture and input injection.

Note on introspection: dbus-next 0.2.3 cannot parse the live portal node
because the PowerProfileMonitor interface declares a property named
`power-saver-enabled` (a hyphen is illegal per the D-Bus spec but ships in
xdg-desktop-portal anyway). We provide a hand-rolled XML containing only the
interfaces we use.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

TOKEN_PATH = Path(
    os.environ.get(
        "SCREEN_AGENT_TOKEN_PATH",
        os.path.expanduser("~/.cache/screen-agent/restore_token"),
    )
)


def _load_token() -> str | None:
    try:
        return TOKEN_PATH.read_text().strip() or None
    except FileNotFoundError:
        return None


def _save_token(token: str | None) -> None:
    if not token:
        return
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_OBJ = "/org/freedesktop/portal/desktop"
RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
REQ_IFACE = "org.freedesktop.portal.Request"

PORTAL_INTROSPECT = """
<node>
  <interface name="org.freedesktop.portal.RemoteDesktop">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="SelectDevices">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="Start">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="NotifyPointerMotion">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="d" name="dx" direction="in"/>
      <arg type="d" name="dy" direction="in"/>
    </method>
    <method name="NotifyPointerMotionAbsolute">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="stream" direction="in"/>
      <arg type="d" name="x" direction="in"/>
      <arg type="d" name="y" direction="in"/>
    </method>
    <method name="NotifyPointerButton">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="i" name="button" direction="in"/>
      <arg type="u" name="state" direction="in"/>
    </method>
    <method name="NotifyKeyboardKeycode">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="i" name="keycode" direction="in"/>
      <arg type="u" name="state" direction="in"/>
    </method>
    <method name="NotifyKeyboardKeysym">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="i" name="keysym" direction="in"/>
      <arg type="u" name="state" direction="in"/>
    </method>
    <method name="ConnectToEIS">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="h" name="fd" direction="out"/>
    </method>
    <property name="AvailableDeviceTypes" type="u" access="read"/>
    <property name="version" type="u" access="read"/>
  </interface>
  <interface name="org.freedesktop.portal.ScreenCast">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="SelectSources">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="Start">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="OpenPipeWireRemote">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="h" name="fd" direction="out"/>
    </method>
    <property name="AvailableSourceTypes" type="u" access="read"/>
    <property name="AvailableCursorModes" type="u" access="read"/>
    <property name="version" type="u" access="read"/>
  </interface>
</node>
"""

REQUEST_INTROSPECT = """
<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg type="u" name="response"/>
      <arg type="a{sv}" name="results"/>
    </signal>
  </interface>
</node>
"""


@dataclass
class StreamHandle:
    pw_fd: int
    node_id: int
    session_handle: str
    restore_token: str | None
    devices_bitmask: int
    stream_props: dict[str, Any]


def _token() -> str:
    return "sa_" + secrets.token_hex(8)


def _request_path(bus: MessageBus, handle_token: str) -> str:
    sender = bus.unique_name[1:].replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"


def _unwrap(value: Any) -> Any:
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_unwrap(v) for v in value)
    return value


class _Pending:
    """Subscribes to the predicted Request path's Response signal before the call fires."""

    def __init__(self, bus: MessageBus, handle_token: str, label: str):
        self._bus = bus
        self._handle_token = handle_token
        self._label = label
        self._future: asyncio.Future | None = None
        self._iface = None
        self._handler = None

    async def __aenter__(self):
        path = _request_path(self._bus, self._handle_token)
        proxy = self._bus.get_proxy_object(PORTAL_BUS, path, REQUEST_INTROSPECT)
        self._iface = proxy.get_interface(REQ_IFACE)
        self._future = asyncio.get_running_loop().create_future()

        def on_response(response: int, results: dict):
            if not self._future.done():
                self._future.set_result((response, _unwrap(results)))

        self._handler = on_response
        self._iface.on_response(on_response)
        return self

    async def __aexit__(self, *exc):
        if self._iface and self._handler:
            try:
                self._iface.off_response(self._handler)
            except Exception:
                pass

    async def wait(self) -> dict:
        response, results = await self._future
        if response != 0:
            kind = {1: "user cancelled", 2: "other error"}.get(response, f"unknown ({response})")
            raise RuntimeError(f"{self._label} failed: {kind}; results={results}")
        return results


async def open_session(restore_token: str | None = None) -> StreamHandle:
    bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
    proxy = bus.get_proxy_object(PORTAL_BUS, PORTAL_OBJ, PORTAL_INTROSPECT)
    rd = proxy.get_interface(RD_IFACE)
    sc = proxy.get_interface(SC_IFACE)

    rd_v = await rd.get_version()
    sc_v = await sc.get_version()
    print(f"[portal] RemoteDesktop v{rd_v}  ScreenCast v{sc_v}")
    if sc_v < 4:
        print("[portal] WARNING: ScreenCast<4 may not support restore_token", file=sys.stderr)

    # 1. RemoteDesktop.CreateSession
    ht = _token()
    async with _Pending(bus, ht, "CreateSession") as p:
        await rd.call_create_session(
            {
                "handle_token": Variant("s", ht),
                "session_handle_token": Variant("s", _token()),
            }
        )
        results = await p.wait()
    session = results["session_handle"]
    print(f"[portal] CreateSession ok  session={session}")

    # 2. RemoteDesktop.SelectDevices (1=keyboard | 2=pointer = 3)
    ht = _token()
    async with _Pending(bus, ht, "SelectDevices") as p:
        await rd.call_select_devices(
            session,
            {
                "handle_token": Variant("s", ht),
                "types": Variant("u", 3),
            },
        )
        await p.wait()
    print("[portal] SelectDevices ok (keyboard+pointer)")

    # 3. ScreenCast.SelectSources on the *same* session handle.
    # NB: persist_mode is rejected on RemoteDesktop sessions ("Remote desktop sessions cannot
    # persist"). For M3 we'll likely need a separate ScreenCast-only session for persistence.
    ht = _token()
    sources_opts: dict[str, Variant] = {
        "handle_token": Variant("s", ht),
        "types": Variant("u", 1),       # 1=monitor
        "multiple": Variant("b", False),
        "cursor_mode": Variant("u", 2), # 2=embedded
    }
    async with _Pending(bus, ht, "SelectSources") as p:
        await sc.call_select_sources(session, sources_opts)
        await p.wait()
    print("[portal] SelectSources ok (monitor)")

    # 4. RemoteDesktop.Start — unified consent dialog
    ht = _token()
    async with _Pending(bus, ht, "Start") as p:
        await rd.call_start(session, "", {"handle_token": Variant("s", ht)})
        if not restore_token:
            print("[portal] Start invoked — consent dialog should appear now...")
        results = await p.wait()

    streams = results.get("streams") or []
    new_token = results.get("restore_token")
    devices = results.get("devices", 0)
    if not streams:
        raise RuntimeError("Start returned no streams")
    node_id, props = streams[0]
    print(f"[portal] Start ok  devices_bitmask={devices}  streams={len(streams)}")
    print(f"[portal]   stream[0]: node_id={node_id}  props={props}")
    if new_token:
        print(f"[portal]   restore_token={new_token}")

    # 5. ScreenCast.OpenPipeWireRemote — direct method, returns the PW socket fd
    pw_fd = await sc.call_open_pipe_wire_remote(session, {})
    print(f"[portal] OpenPipeWireRemote ok  fd={pw_fd}")

    return StreamHandle(
        pw_fd=pw_fd,
        node_id=node_id,
        session_handle=session,
        restore_token=new_token,
        devices_bitmask=devices,
        stream_props=props,
    )


async def open_screencast_session(
    *,
    restore_token: str | None = None,
    persist: bool = True,
    save_token: bool = True,
) -> StreamHandle:
    """ScreenCast-only session that supports persist_mode and restore_token.

    Combined RemoteDesktop sessions are blocked from persisting by GNOME's
    portal (security model). This function gives M2-M5 capture work a session
    that survives across runs without re-prompting the user.

    `restore_token=None` falls back to the cached token at TOKEN_PATH if one
    exists. Pass `save_token=False` for tests that should not mutate the cache.
    """
    bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
    proxy = bus.get_proxy_object(PORTAL_BUS, PORTAL_OBJ, PORTAL_INTROSPECT)
    sc = proxy.get_interface(SC_IFACE)

    sc_v = await sc.get_version()
    print(f"[portal-sc] ScreenCast v{sc_v}")
    if persist and sc_v < 4:
        print("[portal-sc] WARNING: ScreenCast<4 may not support restore_token", file=sys.stderr)

    if restore_token is None:
        restore_token = _load_token()
        if restore_token:
            print(f"[portal-sc] using cached restore_token ({restore_token[:8]}...)")

    # 1. ScreenCast.CreateSession
    ht = _token()
    async with _Pending(bus, ht, "ScreenCast.CreateSession") as p:
        await sc.call_create_session(
            {
                "handle_token": Variant("s", ht),
                "session_handle_token": Variant("s", _token()),
            }
        )
        results = await p.wait()
    session = results["session_handle"]
    print(f"[portal-sc] CreateSession ok  session={session}")

    # 2. ScreenCast.SelectSources (with persist + restore_token)
    ht = _token()
    sources_opts: dict[str, Variant] = {
        "handle_token": Variant("s", ht),
        "types": Variant("u", 1),       # 1=monitor
        "multiple": Variant("b", False),
        "cursor_mode": Variant("u", 2), # 2=embedded
    }
    if persist:
        sources_opts["persist_mode"] = Variant("u", 2)
    if restore_token:
        sources_opts["restore_token"] = Variant("s", restore_token)
    async with _Pending(bus, ht, "ScreenCast.SelectSources") as p:
        await sc.call_select_sources(session, sources_opts)
        await p.wait()
    print(
        f"[portal-sc] SelectSources ok"
        f"{' persist' if persist else ''}{', restore' if restore_token else ''}"
    )

    # 3. ScreenCast.Start
    ht = _token()
    async with _Pending(bus, ht, "ScreenCast.Start") as p:
        await sc.call_start(session, "", {"handle_token": Variant("s", ht)})
        if not restore_token:
            print("[portal-sc] Start invoked — consent dialog should appear now...")
        results = await p.wait()

    streams = results.get("streams") or []
    new_token = results.get("restore_token")
    if not streams:
        raise RuntimeError("ScreenCast.Start returned no streams")
    node_id, props = streams[0]
    print(f"[portal-sc] Start ok  streams={len(streams)}")
    print(f"[portal-sc]   stream[0]: node_id={node_id}  props={props}")
    if new_token:
        print(f"[portal-sc]   restore_token={new_token[:12]}... (len={len(new_token)})")
        if save_token:
            _save_token(new_token)
            print(f"[portal-sc]   saved to {TOKEN_PATH}")

    # 4. ScreenCast.OpenPipeWireRemote
    pw_fd = await sc.call_open_pipe_wire_remote(session, {})
    print(f"[portal-sc] OpenPipeWireRemote ok  fd={pw_fd}")

    return StreamHandle(
        pw_fd=pw_fd,
        node_id=node_id,
        session_handle=session,
        restore_token=new_token,
        devices_bitmask=0,
        stream_props=props,
    )


async def _main() -> int:
    handle = await open_session()
    print()
    print("=== M0 SUCCESS ===")
    print(f"node_id       : {handle.node_id}")
    print(f"pw_fd         : {handle.pw_fd}")
    print(f"session       : {handle.session_handle}")
    print(f"restore_token : {handle.restore_token}")
    print(f"devices       : {handle.devices_bitmask} (bit0=kbd, bit1=ptr, bit2=touch)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
