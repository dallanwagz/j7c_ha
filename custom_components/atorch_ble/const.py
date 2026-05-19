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

# BLE GATT characteristic carrying Atorch USB-meter notification frames.
# Locked by the device firmware; do not change without verifying against
# a real capture (see batch-1/02 notes).
CHARACTERISTIC_UUID = "0000FFE1-0000-1000-8000-00805F9B34FB"

# Connection backoff parameters (persistent and polled both share these).
# Cap shortened from 60 s in v0.1.1: advertisement-wait now handles the
# "device unreachable" case implicitly, so the outer backoff only needs
# to cover transient GATT-layer failures.
RECONNECT_INITIAL_BACKOFF_SECONDS = 5.0
RECONNECT_MAX_BACKOFF_SECONDS = 30.0

# Upper bound on how long the coordinator will block waiting for a fresh
# BLE advertisement from the meter before treating it as a connect
# failure. Some J7-C firmware variants advertise every several minutes
# when idle, so this needs to be substantially larger than HA's
# connectable-registry freshness window (~90 s).
ADVERTISEMENT_WAIT_TIMEOUT_SECONDS = 600

# One-shot polled-cycle notification wait timeout.
POLLED_NOTIFICATION_TIMEOUT_SECONDS = 5.0

# Repair-issue thresholds.
CONNECT_FAILURE_RAISE_THRESHOLD = 5
CONNECT_FAILURE_RERAISE_INTERVAL = 50

# Persistent storage key on entry.data for acknowledged-unsupported bytes.
ACK_UNSUPPORTED_KEY = "acknowledged_unsupported_packet_types"

# Repair issue IDs / translation keys.
ISSUE_CANNOT_CONNECT = "cannot_connect_after_setup"
ISSUE_UNSUPPORTED_PACKET_TYPE_PREFIX = "unsupported_packet_type_0x"

# Closed set of coordinator connection_state values. Source of truth for
# the sensor enum options, diagnostics legend, and test expectations.
CONNECTION_STATES: tuple[str, ...] = (
    "connected",
    "polling",
    "disconnected",
    "reconnecting",
    "failed_after_setup",
)
