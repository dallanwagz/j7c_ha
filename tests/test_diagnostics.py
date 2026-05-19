"""Tests for the atorch_ble diagnostics platform.

Asserts the user-privacy + UX-locked contracts: MAC partial masking,
absence of raw notification bytes in the payload, the hard-coded English
``_legend`` for ``connection_state``, and the canonical
``coordinator_state`` block surface area.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import DOMAIN
from custom_components.atorch_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED},
        title=TEST_TITLE,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup_and_get_diagnostics(
    hass: HomeAssistant,
) -> tuple[MockConfigEntry, dict]:
    entry = _make_entry(hass)
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    payload = await async_get_config_entry_diagnostics(hass, entry)
    return entry, payload


async def test_diagnostics_legend_present(hass: HomeAssistant) -> None:
    """``_legend`` block carries all 5 connection_state values with text."""
    _, payload = await _setup_and_get_diagnostics(hass)

    legend = payload["_legend"]["connection_state"]
    expected_keys = {
        "connected",
        "polling",
        "disconnected",
        "reconnecting",
        "failed_after_setup",
    }
    assert set(legend.keys()) == expected_keys
    # Each entry is a non-empty human-readable string.
    for key in expected_keys:
        assert isinstance(legend[key], str)
        assert len(legend[key]) > 10


async def test_diagnostics_mac_partial_masked(hass: HomeAssistant) -> None:
    """The MAC in the diagnostics output is masked as AA:BB:**:**:**:FF."""
    _, payload = await _setup_and_get_diagnostics(hass)

    masked = "aa:bb:**:**:**:ff"
    # config_entry.data should carry the masked form, not the raw MAC.
    assert payload["config_entry"]["data"][CONF_ADDRESS] == masked
    # Device block masks both identifiers and connections.
    identifiers = payload["device"]["identifiers"]
    assert any(masked in [v for v in pair] for pair in identifiers)
    connections = payload["device"]["connections"]
    assert any(masked in [v for v in pair] for pair in connections)

    # No field anywhere in the payload contains the raw MAC.
    blob = json.dumps(payload, default=str).lower()
    assert TEST_MAC_NORMALIZED not in blob


async def test_diagnostics_no_raw_bytes(hass: HomeAssistant) -> None:
    """No field in diagnostics carries raw notification bytes."""
    _, payload = await _setup_and_get_diagnostics(hass)

    blob = json.dumps(payload, default=str)
    # No raw byte signatures from the BLE frame.
    assert "\\xff\\x55" not in blob.lower()
    # No keys named like raw-byte dump points.
    forbidden_keys = {"notif_bytes", "raw_bytes", "notification_bytes", "raw_frame"}

    def _walk(obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k not in forbidden_keys, f"raw-bytes key surfaced: {k}"
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)


async def test_diagnostics_coordinator_state_present(
    hass: HomeAssistant,
) -> None:
    """coordinator_state block carries the canonical fields."""
    _, payload = await _setup_and_get_diagnostics(hass)

    state = payload["coordinator_state"]
    assert "connection_state" in state
    assert "parser_error_count" in state
    assert "parser_error_rate_5m" in state
    assert "acknowledged_unsupported_packet_types" in state
    # parser_error_rate_5m is a float (0.0 for a fresh coordinator).
    assert isinstance(state["parser_error_rate_5m"], float)
    # acknowledged_unsupported_packet_types is a (possibly empty) list.
    assert isinstance(state["acknowledged_unsupported_packet_types"], list)


async def test_diagnostics_partial_mask_edge_cases() -> None:
    """``_partial_mask_mac`` handles None, empty, and malformed inputs."""
    from custom_components.atorch_ble.diagnostics import _partial_mask_mac

    assert _partial_mask_mac(None) is None
    assert _partial_mask_mac("") == ""
    assert _partial_mask_mac("not-a-mac") == "**"
    assert _partial_mask_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:**:**:**:ff"


async def test_diagnostics_project_last_reading_non_dataclass() -> None:
    """The reading-projection helper reflects over plain objects too."""
    from custom_components.atorch_ble.diagnostics import _project_last_reading

    assert _project_last_reading(None) is None

    class _Plain:
        """Non-dataclass reading-shaped object."""

        voltage_v = 5.0
        current_a = 1.0

    out = _project_last_reading(_Plain())
    assert out["voltage_v"] == 5.0
    assert out["current_a"] == 1.0


async def test_diagnostics_bluetooth_sources_api_unavailable(
    hass: HomeAssistant,
) -> None:
    """``_project_bluetooth_sources`` returns [] when the HA API is missing."""
    from custom_components.atorch_ble import diagnostics as diag

    with patch.object(diag.bluetooth, "async_scanner_devices_by_address", None):
        assert diag._project_bluetooth_sources(hass, "anything") == []


async def test_diagnostics_bluetooth_sources_with_entries(
    hass: HomeAssistant,
) -> None:
    """Per-source RSSI + time_since_last_seen projection populates the list."""
    from custom_components.atorch_ble import diagnostics as diag

    fake_scanner = type("Scanner", (), {"source": "local"})()
    fake_adv = type("Adv", (), {"rssi": -55})()
    fake_sd = type(
        "SD",
        (),
        {
            "scanner": fake_scanner,
            "advertisement": fake_adv,
            "time_since_last_seen": 1.25,
        },
    )()

    with patch.object(
        diag.bluetooth,
        "async_scanner_devices_by_address",
        return_value=[fake_sd],
    ):
        out = diag._project_bluetooth_sources(hass, "AA:BB:CC:DD:EE:FF")
    assert out == [
        {"source": "local", "rssi": -55, "time_since_last_seen": 1.25}
    ]


async def test_diagnostics_bluetooth_sources_api_raises(
    hass: HomeAssistant,
) -> None:
    """If the API raises, _project_bluetooth_sources returns []."""
    from custom_components.atorch_ble import diagnostics as diag

    with patch.object(
        diag.bluetooth,
        "async_scanner_devices_by_address",
        side_effect=RuntimeError("boom"),
    ):
        assert diag._project_bluetooth_sources(hass, "anything") == []


async def test_diagnostics_no_coordinator_or_device(hass: HomeAssistant) -> None:
    """Calling diagnostics for an entry not in hass.data still succeeds.

    Exercises the no-coordinator + no-device-registry-entry fallback
    branches in async_get_config_entry_diagnostics.
    """
    entry = _make_entry(hass)
    # Note: entry is added to hass but NOT set up — no coordinator
    # registered, no device created. The diagnostics call must still
    # produce a JSON-safe dict via the fallback paths.
    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert payload["coordinator_state"]["mode"] is None
    assert payload["coordinator_state"]["connection_state"] is None
    assert payload["last_reading"] is None
    # Fallback device block masks the address.
    assert payload["device"]["model"] is None
    assert payload["device"]["manufacturer"] == "Atorch"
