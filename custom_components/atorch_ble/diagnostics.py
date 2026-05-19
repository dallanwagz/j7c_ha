"""Diagnostics support for the atorch_ble integration.

Implements ``async_get_config_entry_diagnostics`` per ticket #15. The
payload is a JSON-serializable dict surfacing:

* a hard-coded English ``_legend`` explaining ``connection_state`` values
  (UX-locked text from ``PROJECT_CONTEXT.md`` → "Diagnostics legend");
* a partial-MAC-masked ``device`` block (form ``AA:BB:**:**:**:FF``);
* a redacted ``config_entry`` block (data + options);
* a ``coordinator_state`` snapshot (mode, last_seen, connection_state,
  consecutive_connect_failures, failures_since_last_raise,
  parser_error_count, parser_error_rate_5m, the persisted
  ``acknowledged_unsupported_packet_types`` set);
* a ``last_reading`` block (parsed ``UsbMeterReading`` fields) or
  ``None``;
* a ``bluetooth_sources`` view derived from
  ``bluetooth.async_scanner_devices_by_address`` (per-source RSSI), or
  an empty list if the API is unavailable.

Diagnostics is a developer-facing artifact — the legend text is hard-
coded English literals (not routed through ``strings.json``). No raw
notification bytes are ever included in the payload.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import ACK_UNSUPPORTED_KEY, DOMAIN

# UX-locked diagnostics-legend text. Keys must exactly match the closed
# set of connection_state strings used by the coordinator (see
# const.CONNECTION_STATES); values are verbatim per PROJECT_CONTEXT.md
# and kept as literal keys here because each value is a distinct
# human-readable description.
_CONNECTION_STATE_LEGEND: dict[str, str] = {
    "connected": (
        "Persistent connection mode: actively subscribed to notifications."
    ),
    "polling": (
        "Polled connection mode: currently connected mid-poll-cycle "
        "(receiving one frame, then disconnecting)."
    ),
    "disconnected": (
        "Polled connection mode: between polls; will reconnect at next interval."
    ),
    "reconnecting": (
        "Connection dropped unexpectedly; backoff retry in progress."
    ),
    "failed_after_setup": (
        "Repeated connection failures since setup; see the "
        "cannot_connect_after_setup repair issue."
    ),
}

# Fields redacted by ``async_redact_data`` in the config_entry block.
# The MAC under CONF_ADDRESS is handled separately via partial masking
# at projection time, so it is intentionally not in this set.
_REDACT_CONFIG_ENTRY: set[str] = {"unique_id"}


def _partial_mask_mac(mac: str | None) -> str | None:
    """Mask a MAC string to ``AA:BB:**:**:**:FF`` form.

    First 2 octets and the last 1 octet are kept visible; middle 3
    octets are replaced with literal ``**``. Returns ``None`` when the
    input is falsy and falls back to a single ``**`` redaction when the
    input doesn't parse as a 6-octet MAC.
    """
    if not mac:
        return mac
    parts = mac.split(":")
    if len(parts) != 6:
        return "**"
    return f"{parts[0]}:{parts[1]}:**:**:**:{parts[5]}"


def _project_bluetooth_sources(hass: HomeAssistant, address: str) -> list[dict[str, Any]]:
    """Return a per-source advertisement view, or [] if unavailable.

    Each entry is ``{"source", "rssi", "time_since_last_seen"}``. The
    raw ``BluetoothServiceInfoBleak`` is intentionally not included.
    Falls back gracefully on AttributeError if the HA Bluetooth public
    API ever renames or removes ``async_scanner_devices_by_address``.
    """
    fn = getattr(bluetooth, "async_scanner_devices_by_address", None)
    if fn is None:
        return []
    try:
        scanner_devices = fn(hass, address, connectable=True)
    except Exception:  # noqa: BLE001 — defensive against API drift
        return []

    out: list[dict[str, Any]] = []
    for sd in scanner_devices or []:
        source = getattr(getattr(sd, "scanner", None), "source", None)
        adv = getattr(sd, "advertisement", None)
        rssi = getattr(adv, "rssi", None) if adv is not None else None
        time_since = getattr(sd, "time_since_last_seen", None)
        out.append(
            {
                "source": source,
                "rssi": rssi,
                "time_since_last_seen": time_since,
            }
        )
    return out


def _project_last_reading(reading: Any) -> dict[str, Any] | None:
    """Project a ``UsbMeterReading`` dataclass into a JSON-safe dict.

    Returns ``None`` when no reading has been observed yet. Uses
    ``dataclasses.asdict`` when possible and otherwise reflects over
    public attributes — keeping the diagnostics surface resilient to
    minor parser-library shape changes.
    """
    if reading is None:
        return None
    if dataclasses.is_dataclass(reading):
        return dataclasses.asdict(reading)
    return {
        name: getattr(reading, name)
        for name in dir(reading)
        if not name.startswith("_") and not callable(getattr(reading, name))
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Public symbol consumed by Home Assistant's diagnostics platform when
    the user clicks "Download diagnostics" on the integration card.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    raw_address: str = entry.data.get(CONF_ADDRESS, "")
    masked_address = _partial_mask_mac(raw_address)

    # Redact + partial-mask the config_entry block. We feed a shallow
    # copy through async_redact_data first, then overwrite CONF_ADDRESS
    # with the partial-masked form so MACs use the project-wide mask
    # rather than full redaction.
    entry_data = async_redact_data(dict(entry.data), _REDACT_CONFIG_ENTRY)
    if CONF_ADDRESS in entry_data:
        entry_data[CONF_ADDRESS] = masked_address
    entry_options = dict(entry.options)

    # Device block: pull the canonical fields from the device registry
    # entry created in async_setup_entry. MAC fields in both
    # ``identifiers`` and ``connections`` are partial-masked.
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, raw_address)})
    if device is not None:
        identifiers = [
            [domain, _partial_mask_mac(value) if domain == DOMAIN else value]
            for (domain, value) in device.identifiers
        ]
        connections = [
            [conn_type, _partial_mask_mac(value)]
            for (conn_type, value) in device.connections
        ]
        device_block: dict[str, Any] = {
            "manufacturer": device.manufacturer,
            "model": device.model,
            "name": device.name,
            "hw_version": device.hw_version,
            "sw_version": device.sw_version,
            "identifiers": identifiers,
            "connections": connections,
        }
    else:
        device_block = {
            "manufacturer": "Atorch",
            "model": None,
            "name": None,
            "hw_version": "UC96",
            "sw_version": None,
            "identifiers": [[DOMAIN, masked_address]],
            "connections": [],
        }

    # Coordinator state block — defensive ``getattr`` keeps diagnostics
    # useful even if coordinator setup is mid-flight or partial.
    if coordinator is not None:
        last_seen = getattr(coordinator, "last_seen", None)
        coordinator_state: dict[str, Any] = {
            "mode": getattr(coordinator, "_connection_mode", None),
            "connection_state": getattr(coordinator, "connection_state", None),
            "last_seen": last_seen.isoformat() if last_seen is not None else None,
            "consecutive_connect_failures": getattr(
                coordinator, "_consecutive_connect_failures", None
            ),
            "failures_since_last_raise": getattr(
                coordinator, "_failures_since_last_raise", None
            ),
            "parser_error_count": getattr(coordinator, "parser_error_count", None),
            "parser_error_rate_5m": getattr(
                coordinator, "parser_error_rate_5m", None
            ),
            "acknowledged_unsupported_packet_types": sorted(
                entry.data.get(ACK_UNSUPPORTED_KEY, [])
            ),
        }
        last_reading = _project_last_reading(
            getattr(coordinator, "last_reading", None)
        )
    else:
        coordinator_state = {
            "mode": None,
            "connection_state": None,
            "last_seen": None,
            "consecutive_connect_failures": None,
            "failures_since_last_raise": None,
            "parser_error_count": None,
            "parser_error_rate_5m": None,
            "acknowledged_unsupported_packet_types": sorted(
                entry.data.get(ACK_UNSUPPORTED_KEY, [])
            ),
        }
        last_reading = None

    bluetooth_sources = _project_bluetooth_sources(hass, raw_address)

    return {
        "_legend": {"connection_state": _CONNECTION_STATE_LEGEND},
        "device": device_block,
        "config_entry": {
            "title": entry.title,
            "version": entry.version,
            "domain": entry.domain,
            "data": entry_data,
            "options": entry_options,
        },
        "coordinator_state": coordinator_state,
        "last_reading": last_reading,
        "bluetooth_sources": bluetooth_sources,
    }
