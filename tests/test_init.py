"""Tests for the atorch_ble integration setup/unload lifecycle.

Covers ``async_setup_entry`` / ``async_unload_entry`` cycle, device
registry registration with the canonical Atorch / J7-C identifiers, and
the entry-removal path that cleans device-registry and entity-registry
state when a user deletes the integration.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

from bleak.backends.device import BLEDevice
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import (
    ACK_UNSUPPORTED_KEY,
    DOMAIN,
)

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE

# Patch target for the test-before-setup reachability probe in
# async_setup_entry. Returning a fake BLEDevice makes the meter look
# reachable; returning None drives the ConfigEntryNotReady retry path.
_BLE_LOOKUP = (
    "custom_components.atorch_ble.bluetooth.async_ble_device_from_address"
)


def _reachable_device() -> patch:
    """Patch the reachability probe so the meter appears reachable."""
    return patch(_BLE_LOOKUP, return_value=MagicMock(spec=BLEDevice))


def _make_entry(
    hass: HomeAssistant, *, data: dict | None = None
) -> MockConfigEntry:
    """Construct + register a MockConfigEntry suitable for setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED, **(data or {})},
        title=TEST_TITLE,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_unload_resetup_cycle(hass: HomeAssistant) -> None:
    """Setup -> unload -> re-setup completes without leaking state."""
    entry = _make_entry(hass)

    # Patch the runner so async_setup_entry doesn't actually fire BLE I/O,
    # and make the test-before-setup reachability probe see the meter.
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ), _reachable_device():
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

        # Coordinator instantiated, stashed under hass.data[DOMAIN][entry_id].
        coordinator_first = hass.data[DOMAIN][entry.entry_id]
        assert coordinator_first is not None

        # Unload — hass.data slot for this entry must be cleaned up.
        assert await hass.config_entries.async_unload(entry.entry_id) is True
        await hass.async_block_till_done()
        assert entry.entry_id not in hass.data.get(DOMAIN, {})

        # Re-setup against the same entry id — fresh coordinator instance.
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        coordinator_second = hass.data[DOMAIN][entry.entry_id]
        assert coordinator_second is not None
        assert coordinator_second is not coordinator_first


async def test_device_registry_entry_created(hass: HomeAssistant) -> None:
    """After setup the device registry carries the canonical J7-C entry."""
    entry = _make_entry(hass)

    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ), _reachable_device():
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    device = registry.async_get_device(
        identifiers={(DOMAIN, TEST_MAC_NORMALIZED)}
    )
    assert device is not None
    assert device.manufacturer == "Atorch"
    assert device.model == "J7-C USB Power Meter"
    assert device.hw_version == "UC96"
    assert (dr.CONNECTION_BLUETOOTH, TEST_MAC_NORMALIZED) in device.connections


async def test_entry_removal_cleans_up_registry(hass: HomeAssistant) -> None:
    """Removing the entry erases device + entity registry entries cleanly."""
    entry = _make_entry(hass, data={ACK_UNSUPPORTED_KEY: [2]})

    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ), _reachable_device():
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device = device_reg.async_get_device(
        identifiers={(DOMAIN, TEST_MAC_NORMALIZED)}
    )
    assert device is not None
    entries_for_entry = er.async_entries_for_config_entry(
        entity_reg, entry.entry_id
    )
    entity_ids = [e.entity_id for e in entries_for_entry]
    assert entity_ids  # sanity — sensor platform registered entities

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    # Device entry gone, entity entries gone, config entry gone.
    assert (
        device_reg.async_get_device(identifiers={(DOMAIN, TEST_MAC_NORMALIZED)})
        is None
    )
    for entity_id in entity_ids:
        assert entity_reg.async_get(entity_id) is None
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_setup_retries_when_device_unreachable(
    hass: HomeAssistant,
) -> None:
    """Setup raises ConfigEntryNotReady when the meter is not reachable.

    The test-before-setup reachability probe returns ``None`` (no
    connectable BLEDevice in HA's bluetooth registry), so async_setup_entry
    must raise ConfigEntryNotReady and the entry must land in the
    SETUP_RETRY state rather than setting up with unavailable entities.
    """
    entry = _make_entry(hass)

    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ), patch(_BLE_LOOKUP, return_value=None):
        with contextlib.suppress(Exception):
            await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Entry is in the retry state; no coordinator was stashed.
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
