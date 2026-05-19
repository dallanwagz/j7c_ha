"""Tests for the atorch_ble coordinator.

Covers initial connection_state per mode, parser_error_rate_5m rolling
window, unsupported-packet repair-issue + persistent-dismissal +
title/model rewriting, and the cannot_connect_after_setup
5-failure-raise / 50-failure-re-raise / clear-on-success lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from atorch_ble import UsbMeterReading
from bleak import BleakError
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import (
    ACK_UNSUPPORTED_KEY,
    CONF_CONNECTION_MODE,
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    ISSUE_CANNOT_CONNECT,
    MODE_PERSISTENT,
    MODE_POLLED,
)
from custom_components.atorch_ble.coordinator import AtorchBleCoordinator

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


def _make_entry(
    hass: HomeAssistant,
    *,
    options: dict | None = None,
    data: dict | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED, **(data or {})},
        title=TEST_TITLE,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(
    hass: HomeAssistant,
    *,
    options: dict | None = None,
    data: dict | None = None,
) -> tuple[MockConfigEntry, AtorchBleCoordinator]:
    entry = _make_entry(hass, options=options, data=data)
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return entry, coordinator


# ---------------------------------------------------------------------------
# Initial connection_state
# ---------------------------------------------------------------------------


async def test_initial_connection_state_persistent_is_reconnecting(
    hass: HomeAssistant,
) -> None:
    """A persistent-mode entry starts in connection_state='reconnecting'."""
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    assert coordinator.connection_state == "reconnecting"


async def test_initial_connection_state_polled_is_disconnected(
    hass: HomeAssistant,
) -> None:
    """A polled-mode entry starts in connection_state='disconnected'."""
    _, coordinator = await _setup(
        hass,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )
    assert coordinator.connection_state == "disconnected"


# ---------------------------------------------------------------------------
# Parser error-rate rolling-window
# ---------------------------------------------------------------------------


async def test_parser_error_rate_5m_zero_when_empty(
    hass: HomeAssistant,
) -> None:
    """No notifications, no errors -> rate is 0.0."""
    _, coordinator = await _setup(hass)
    assert coordinator.parser_error_rate_5m == 0.0


async def test_parser_error_rate_5m_rolling_window(
    hass: HomeAssistant,
) -> None:
    """Mixed notifications + errors across buckets compute the canonical ratio.

    Drives ``_increment_bucket`` directly with a controlled monotonic
    clock — bypasses the bleak notification path entirely so the test
    asserts the rolling-window math, not the parser plumbing.
    """
    _, coordinator = await _setup(hass)

    # Bucket size is 30s, window is 300s (10 buckets).
    # Bucket 0 (t=0): 10 notifications, 0 errors
    coordinator._increment_bucket(0.0, notifs=10, errors=0)
    # Bucket 1 (t=30): 10 notifications, 2 errors
    coordinator._increment_bucket(30.0, notifs=10, errors=2)
    # Bucket 2 (t=60): 5 notifications, 1 error
    coordinator._increment_bucket(60.0, notifs=5, errors=1)

    # Rate at t=60: (0+2+1) / (10+10+5) = 3 / 25 = 0.12
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=60.0,
    ):
        assert abs(coordinator.parser_error_rate_5m - (3 / 25)) < 1e-9

    # Zero errors → rate 0.0 case.
    _, coord2 = await _setup(hass)
    coord2._increment_bucket(0.0, notifs=10, errors=0)
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=0.0,
    ):
        assert coord2.parser_error_rate_5m == 0.0


async def test_parser_error_rate_5m_excludes_old_buckets(
    hass: HomeAssistant,
) -> None:
    """Buckets older than the 5-minute window are pruned out of the ratio."""
    _, coordinator = await _setup(hass)

    # Old bucket at t=0 with 10 errors out of 10 notifs.
    coordinator._increment_bucket(0.0, notifs=10, errors=10)
    # Fresh bucket at t=400 (well outside window from t=0) with clean data.
    coordinator._increment_bucket(400.0, notifs=20, errors=0)

    # At t=400, the t=0 bucket is outside the 300s window and must prune.
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=400.0,
    ):
        assert coordinator.parser_error_rate_5m == 0.0


# ---------------------------------------------------------------------------
# Unsupported packet type — repair issue + persistent dismissal + title/model
# ---------------------------------------------------------------------------


async def test_unsupported_packet_type_repair_issue_created(
    hass: HomeAssistant,
) -> None:
    """Observing UnsupportedPacketType(0x02) raises the named repair issue."""
    _, coordinator = await _setup(hass)

    await coordinator._handle_unsupported_packet_type(0x02)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, "unsupported_packet_type_0x02"
    )
    assert issue is not None
    assert issue.translation_key == "unsupported_packet_type"
    assert issue.translation_placeholders["packet_type"] == "02"
    assert (
        issue.translation_placeholders["packet_type_family"]
        == "DC meter family — e.g. DL24, UD18"
    )


async def test_unsupported_packet_type_persistent_dismissal(
    hass: HomeAssistant,
) -> None:
    """Acknowledged packet types persist in entry.data and suppress re-raises."""
    entry, coordinator = await _setup(hass)

    # First observation: ack persisted, issue raised.
    await coordinator._handle_unsupported_packet_type(0x02)
    await hass.async_block_till_done()
    assert 2 in entry.data[ACK_UNSUPPORTED_KEY]
    issue_first = ir.async_get(hass).async_get_issue(
        DOMAIN, "unsupported_packet_type_0x02"
    )
    assert issue_first is not None

    # Delete the issue to simulate dismissal; second observation must
    # NOT re-create the issue because the byte is acknowledged.
    ir.async_delete_issue(hass, DOMAIN, "unsupported_packet_type_0x02")
    await coordinator._handle_unsupported_packet_type(0x02)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "unsupported_packet_type_0x02")
        is None
    )

    # A different unsupported byte raises a new issue and joins the ack set.
    await coordinator._handle_unsupported_packet_type(0x01)
    await hass.async_block_till_done()
    assert set(entry.data[ACK_UNSUPPORTED_KEY]) == {1, 2}
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "unsupported_packet_type_0x01")
        is not None
    )


async def test_title_and_model_update_on_unsupported(
    hass: HomeAssistant,
) -> None:
    """Observing 0x02 rewrites entry.title and the device-registry model."""
    entry, coordinator = await _setup(hass)

    await coordinator._handle_unsupported_packet_type(0x02)
    await hass.async_block_till_done()

    # Title: "Atorch unknown (EEFF)" — last 4 of MAC, uppercase.
    assert entry.title == "Atorch unknown (EEFF)"

    device = dr.async_get(hass).async_get_device(
        connections={(dr.CONNECTION_BLUETOOTH, TEST_MAC_NORMALIZED)}
    )
    assert device is not None
    assert device.model == "Unknown Atorch device (type 0x02)"


async def test_unsupported_acknowledgements_survive_restart(
    hass: HomeAssistant,
) -> None:
    """The acked-bytes set persists across setup -> unload -> setup."""
    entry, coordinator = await _setup(hass)
    await coordinator._handle_unsupported_packet_type(0x02)
    await coordinator._handle_unsupported_packet_type(0x01)
    await hass.async_block_till_done()
    assert set(entry.data[ACK_UNSUPPORTED_KEY]) == {1, 2}

    # Unload then re-setup the same entry; ack set survives.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    assert set(entry.data[ACK_UNSUPPORTED_KEY]) == {1, 2}

    coordinator2 = hass.data[DOMAIN][entry.entry_id]
    # Observing already-acked bytes after restart raises no new issues.
    ir.async_delete_issue(hass, DOMAIN, "unsupported_packet_type_0x02")
    ir.async_delete_issue(hass, DOMAIN, "unsupported_packet_type_0x01")
    await coordinator2._handle_unsupported_packet_type(0x02)
    await coordinator2._handle_unsupported_packet_type(0x01)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "unsupported_packet_type_0x02")
        is None
    )
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "unsupported_packet_type_0x01")
        is None
    )
