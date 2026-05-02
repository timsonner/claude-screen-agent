"""
M6 diagnosis: open a combined RemoteDesktop+ScreenCast session and dump the
full Start results so we can see exactly what GNOME's portal granted.

Most-likely diagnosis: the consent dialog on this GNOME version is the
'Remote Control' dialog (combined screen + input). It usually has:
  - A 'Share screen' / 'Allow' button (always present)
  - An 'Allow remote control of pointer and keyboard' toggle/checkbox

If you only click 'Allow' without flipping the input toggle, devices=0.
"""

from __future__ import annotations

import asyncio
import sys

from portal_remotedesktop import open_session


async def _main() -> int:
    handle = await open_session()
    print()
    print("=== DIAGNOSIS ===")
    print(f"node_id        : {handle.node_id}")
    print(f"pw_fd          : {handle.pw_fd}")
    print(f"session        : {handle.session_handle}")
    print(f"restore_token  : {handle.restore_token}")
    print(f"devices        : {handle.devices_bitmask}")
    print()
    if handle.devices_bitmask & 0b001:
        print("  ✓ keyboard granted")
    else:
        print("  ✗ keyboard NOT granted")
    if handle.devices_bitmask & 0b010:
        print("  ✓ pointer granted")
    else:
        print("  ✗ pointer NOT granted")
    if handle.devices_bitmask & 0b100:
        print("  ✓ touchscreen granted")
    print()
    if handle.devices_bitmask == 0:
        print("ALL DEVICES DENIED. The dialog has an input-control toggle that")
        print("wasn't enabled. Re-run and look for 'Allow remote control of")
        print("pointer and keyboard' (or similar) in the dialog before clicking")
        print("Share/Allow.")
    else:
        print("Input devices granted — M6 actuator can proceed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
