"""Constants for the atorch_ble integration."""

from __future__ import annotations

DOMAIN = "atorch_ble"
MANUFACTURER = "Atorch"

# Canonical lookup pattern for downstream callers:
#   PACKET_TYPE_TO_MODEL.get(packet_type, f"Unknown Atorch device (type 0x{packet_type:02X})")
PACKET_TYPE_TO_MODEL: dict[int, str] = {
    0x03: "J7-C USB Power Meter",
}

# UX-locked packet-type family mapping (per PROJECT_CONTEXT.md).
PACKET_TYPE_TO_FAMILY: dict[int, str] = {
    0x01: "AC meter family",
    0x02: "DC meter family — e.g. DL24, UD18",
    0x03: "USB meter — J7-C, UC96",
}


def packet_type_family(packet_type: int) -> str:
    """Return the user-facing family label for a packet-type byte.

    Falls back to the canonical ``"unknown variant"`` string for bytes
    that are not in the UX-locked mapping table.
    """
    return PACKET_TYPE_TO_FAMILY.get(packet_type, "unknown variant")


# UX-locked options-flow constants.
CONF_CONNECTION_MODE = "connection_mode"
CONF_POLL_INTERVAL_SECONDS = "poll_interval_seconds"

MODE_PERSISTENT = "persistent"
MODE_POLLED = "polled"

DEFAULT_CONNECTION_MODE = MODE_PERSISTENT
DEFAULT_POLL_INTERVAL_SECONDS = 60
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 3600
