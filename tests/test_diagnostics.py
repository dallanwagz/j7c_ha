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
