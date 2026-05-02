"""
Probe whether ConnectToEIS returns a usable fd on this GNOME version even
when devices=0 was reported. This tells us whether we can drive input via
libei despite the portal not advertising any granted devices.
"""

from __future__ import annotations

import asyncio
import sys

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from portal_remotedesktop import (
    PORTAL_BUS, PORTAL_OBJ, PORTAL_INTROSPECT, RD_IFACE, open_session,
)


async def _main() -> int:
    handle = await open_session()
    print(f"\n[probe] devices_bitmask={handle.devices_bitmask}")
    print(f"[probe] attempting ConnectToEIS on session={handle.session_handle}...")

    bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
    proxy = bus.get_proxy_object(PORTAL_BUS, PORTAL_OBJ, PORTAL_INTROSPECT)
    rd = proxy.get_interface(RD_IFACE)

    try:
        fd = await rd.call_connect_to_eis(handle.session_handle, {})
        print(f"[probe] SUCCESS: got EIS fd={fd}")
        print("[probe] libei input would be possible from here")
        return 0
    except DBusError as e:
        print(f"[probe] FAILED: {e.type}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
