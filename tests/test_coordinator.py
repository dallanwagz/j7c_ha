"""Tests for the atorch_ble coordinator.

Covers initial connection_state per mode, parser_error_rate_5m rolling
window, unsupported-packet repair-issue + persistent-dismissal +
title/model rewriting, and the cannot_connect_after_setup
5-failure-raise / 50-failure-re-raise / clear-on-success lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from atorch_ble import UsbMeterReading
from bleak import BleakError
from bleak.backends.device import BLEDevice
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import (
    ACK_UNSUPPORTED_KEY,
    ATORCH_SERVICE_UUID,
    CONF_CONNECTION_MODE,
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    ISSUE_CANNOT_CONNECT,
    ISSUE_CANNOT_CONNECT_NO_SLOT,
    MODE_PERSISTENT,
    MODE_POLLED,
    PERSISTENT_DATA_TIMEOUT_SECONDS,
)
from custom_components.atorch_ble.coordinator import (
    AtorchBleCoordinator,
    _is_no_slot_error,
)

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
    ), patch(
        "custom_components.atorch_ble.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(spec=BLEDevice),
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
# Connection-state setter — notifies listeners on change, idempotent on no-op
# ---------------------------------------------------------------------------


async def test_set_connection_state_notifies_listeners_on_change(
    hass: HomeAssistant,
) -> None:
    """_set_connection_state fires listeners so the diagnostic sensor refreshes.

    Direct assignment to ``_connection_state`` previously left the
    ``connection_state`` sensor stuck on its first read because nothing
    triggered an HA-side state-write. Routing every transition through
    the setter must call back into the CoordinatorEntity listener
    registry.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    calls: list[None] = []
    coordinator.async_add_listener(lambda: calls.append(None))

    coordinator._set_connection_state("connected")
    assert coordinator.connection_state == "connected"
    assert len(calls) == 1

    coordinator._set_connection_state("disconnected")
    assert coordinator.connection_state == "disconnected"
    assert len(calls) == 2


async def test_set_connection_state_no_op_does_not_notify(
    hass: HomeAssistant,
) -> None:
    """Re-asserting the same state must not fire listeners.

    Steady-state operation (e.g., persistent runner heartbeat re-entry
    after a transient hiccup) should not produce spurious listener
    storms.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    calls: list[None] = []
    coordinator.async_add_listener(lambda: calls.append(None))

    # Initial state is already "reconnecting"; no-op set should be silent.
    coordinator._set_connection_state("reconnecting")
    assert len(calls) == 0


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
# Persistent-mode data-flow watchdog (_data_is_stale)
# ---------------------------------------------------------------------------


async def test_data_is_stale_false_before_any_notification(
    hass: HomeAssistant,
) -> None:
    """With no notification timestamp seeded yet, the link is never stale.

    Guards the defensive pre-connect path: the connect helper seeds the
    timestamp, but if the heartbeat ever ran first it must not tear down.
    """
    _, coordinator = await _setup(hass)
    assert coordinator._last_notification_monotonic is None
    assert coordinator._data_is_stale(10_000.0) is False


async def test_data_is_stale_false_within_timeout(
    hass: HomeAssistant,
) -> None:
    """A recent notification keeps the link fresh."""
    _, coordinator = await _setup(hass)
    coordinator._last_notification_monotonic = 100.0
    # Just under the timeout -> still fresh.
    assert (
        coordinator._data_is_stale(100.0 + PERSISTENT_DATA_TIMEOUT_SECONDS - 0.1)
        is False
    )


async def test_data_is_stale_true_past_timeout(
    hass: HomeAssistant,
) -> None:
    """No notification for longer than the timeout marks the link stale."""
    _, coordinator = await _setup(hass)
    coordinator._last_notification_monotonic = 100.0
    assert (
        coordinator._data_is_stale(100.0 + PERSISTENT_DATA_TIMEOUT_SECONDS + 0.1)
        is True
    )


async def test_notification_callback_refreshes_watchdog_timestamp(
    hass: HomeAssistant,
) -> None:
    """Each raw notification refreshes the watchdog clock.

    Asserts the wiring (callback -> timestamp) without the bleak path:
    feed a valid frame through the public notification callback and check
    the monotonic stamp advances to the patched 'now'.
    """
    _, coordinator = await _setup(hass)
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=4242.0,
    ):
        coordinator._notification_callback(None, b"\x00")  # non-frame is fine
    assert coordinator._last_notification_monotonic == 4242.0


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
    ), patch(
        "custom_components.atorch_ble.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(spec=BLEDevice),
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


# ---------------------------------------------------------------------------
# cannot_connect_after_setup lifecycle
# ---------------------------------------------------------------------------


def _drive_failure(
    coordinator: AtorchBleCoordinator, exc: Exception | None = None
) -> None:
    """Helper: invoke the coordinator's failure hook synchronously."""
    coordinator._on_connect_failure(exc or BleakError("test connect failure"))


async def test_cannot_connect_after_setup_raises_at_5_failures(
    hass: HomeAssistant,
) -> None:
    """Five consecutive failures raise the cannot_connect_after_setup issue."""
    _, coordinator = await _setup(hass)

    # First 4 failures must NOT raise the repair issue.
    for _ in range(4):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT) is None
    )

    # 5th failure: issue raised.
    _drive_failure(coordinator)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
    assert issue is not None
    assert "device_name" in issue.translation_placeholders
    # Generic failure -> generic translation key.
    assert issue.translation_key == ISSUE_CANNOT_CONNECT


def test_is_no_slot_error_detects_slot_exhaustion() -> None:
    """The slot-exhaustion signature is matched; unrelated errors are not."""
    no_slot = BleakError(
        "atorch_ble-c2:67:69:9f:77:f4 - C2:67:69:9F:77:F4: Failed to connect "
        "after 9 attempt(s): No backend with an available connection slot "
        "that can reach address C2:67:69:9F:77:F4 was found"
    )
    assert _is_no_slot_error(no_slot) is True
    assert _is_no_slot_error(BleakError("ESP_GATT_CONN_TIMEOUT")) is False
    assert _is_no_slot_error(TimeoutError()) is False


async def test_cannot_connect_no_slot_uses_actionable_message(
    hass: HomeAssistant,
) -> None:
    """A slot-exhaustion failure raises the issue with the no-slot message.

    Same issue id as the generic case (so clear/re-raise are unchanged),
    but the more actionable ``cannot_connect_no_slot`` translation key.
    """
    _, coordinator = await _setup(hass)
    no_slot = BleakError(
        "No backend with an available connection slot that can reach "
        "address C2:67:69:9F:77:F4 was found"
    )
    for _ in range(5):
        _drive_failure(coordinator, no_slot)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
    assert issue is not None
    assert issue.translation_key == ISSUE_CANNOT_CONNECT_NO_SLOT
    assert "device_name" in issue.translation_placeholders


async def test_cannot_connect_dismissed_then_reraised_at_50(
    hass: HomeAssistant,
) -> None:
    """After dismissal, +49 failures do nothing; the +50th re-raises."""
    _, coordinator = await _setup(hass)

    # Raise initially via 5 failures.
    for _ in range(5):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
        is not None
    )

    # Simulate user dismissal: ignore and delete the issue from the registry.
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_CANNOT_CONNECT, True)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_CANNOT_CONNECT)
    await hass.async_block_till_done()

    # 49 more failures — no fresh issue.
    for _ in range(49):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT) is None
    )

    # 50th additional failure — fresh issue resurfaces.
    _drive_failure(coordinator)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
        is not None
    )


async def test_cannot_connect_cleared_on_successful_connection(
    hass: HomeAssistant,
) -> None:
    """A successful connection after the issue is raised deletes it."""
    _, coordinator = await _setup(hass)
    for _ in range(5):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
        is not None
    )

    coordinator._on_connect_success()
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT) is None
    )


async def test_device_name_placeholder_default(hass: HomeAssistant) -> None:
    """Without name_by_user, device_name placeholder == device.name."""
    _, coordinator = await _setup(hass)
    for _ in range(5):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
    assert issue is not None
    # The default device name is "Atorch J7-C (EEFF)" from __init__.py.
    assert issue.translation_placeholders["device_name"] == "Atorch J7-C (EEFF)"


async def test_device_name_placeholder_uses_name_by_user(
    hass: HomeAssistant,
) -> None:
    """When name_by_user is set, the placeholder reflects it."""
    _, coordinator = await _setup(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_device(
        identifiers={(DOMAIN, TEST_MAC_NORMALIZED)}
    )
    assert device is not None
    registry.async_update_device(device.id, name_by_user="Workbench")

    for _ in range(5):
        _drive_failure(coordinator)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CANNOT_CONNECT)
    assert issue is not None
    assert issue.translation_placeholders["device_name"] == "Workbench"


# ---------------------------------------------------------------------------
# Smaller-grain coordinator hooks
# ---------------------------------------------------------------------------


async def test_snapshot_returns_none_until_first_reading(
    hass: HomeAssistant,
) -> None:
    """Fresh coordinator has no reading and snapshot() returns None."""
    _, coordinator = await _setup(hass)
    assert coordinator._snapshot() is None

    coordinator._last_reading = UsbMeterReading(
        voltage_v=5.0,
        current_a=2.0,
        capacity_mah=100,
        energy_wh=10.0,
        voltage_dplus_v=2.5,
        voltage_dminus_v=2.5,
        temperature_c=25,
        duration_s=60,
    )
    snap = coordinator._snapshot()
    assert snap is not None
    assert snap.power_w == 10.0


async def test_poll_method_returns_none_until_first_reading(
    hass: HomeAssistant,
) -> None:
    """The connected-guard fast path of _poll_method returns the snapshot.

    With a client already connected (the re-entrancy guard from
    decision #2), _poll_method short-circuits and returns whatever
    _snapshot() yields — None pre-first-reading.
    """
    _, coordinator = await _setup(hass)
    fake_client = MagicMock()
    fake_client.is_connected = True
    coordinator._client = fake_client
    assert await coordinator._poll_method(MagicMock()) is None


async def test_needs_poll_method_persistent(hass: HomeAssistant) -> None:
    """Persistent mode never polls — the streaming runner owns the connection."""
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    # Persistent mode returns False unconditionally, regardless of
    # last_poll value or client state.
    assert coordinator._needs_poll_method(MagicMock(), None) is False
    assert coordinator._needs_poll_method(MagicMock(), 0.0) is False
    assert coordinator._needs_poll_method(MagicMock(), 9999.0) is False

    fake_client = MagicMock()
    fake_client.is_connected = False
    coordinator._client = fake_client
    assert coordinator._needs_poll_method(MagicMock(), 0.0) is False


async def test_needs_poll_method_polled(hass: HomeAssistant) -> None:
    """Polled mode polls when seconds-since-last-poll meets the interval.

    ``last_poll`` is passed by the base as seconds since the last poll
    (not an absolute timestamp), so the comparison is direct: a poll is
    due when ``last_poll`` is None or >= the configured interval.
    """
    _, coordinator = await _setup(
        hass,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )
    # last_poll None -> always polls.
    assert coordinator._needs_poll_method(MagicMock(), None) is True
    # 30s since last poll against a 60s interval — no poll yet.
    assert coordinator._needs_poll_method(MagicMock(), 30.0) is False
    # 70s since last poll — poll due.
    assert coordinator._needs_poll_method(MagicMock(), 70.0) is True
    # Exactly at the interval — poll due.
    assert coordinator._needs_poll_method(MagicMock(), 60.0) is True


async def test_options_update_changes_mode(hass: HomeAssistant) -> None:
    """Updating options flips connection_mode in place without re-setup."""
    entry, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    assert coordinator._connection_mode == MODE_PERSISTENT

    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_CONNECTION_MODE: MODE_POLLED,
                CONF_POLL_INTERVAL_SECONDS: 30,
            },
        )
        await hass.async_block_till_done()

    assert coordinator._connection_mode == MODE_POLLED
    assert coordinator._poll_interval_seconds == 30
    assert coordinator.connection_state == "disconnected"
    assert coordinator.expected_cadence_seconds == 30


async def test_options_update_interval_only(hass: HomeAssistant) -> None:
    """Interval-only change is applied without flipping mode."""
    entry, coordinator = await _setup(
        hass,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 120,
        },
    )
    await hass.async_block_till_done()
    assert coordinator._poll_interval_seconds == 120
    assert coordinator._connection_mode == MODE_POLLED


async def test_expected_cadence_persistent_is_one_second(
    hass: HomeAssistant,
) -> None:
    """Persistent mode reports 1s cadence; polled reports the configured interval."""
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    assert coordinator.expected_cadence_seconds == 1


async def test_resolve_ble_device_missing_returns_none(
    hass: HomeAssistant,
) -> None:
    """When HA's bluetooth manager has no entry, the sync helper returns None.

    The synchronous ``_resolve_ble_device`` is the "right now" lookup —
    miss is signalled by ``None`` so callers that legitimately can't
    wait can branch on it. The async ``_wait_for_fresh_advertisement``
    is the primary entry point for the runner.
    """
    _, coordinator = await _setup(hass)
    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        assert coordinator._resolve_ble_device() is None


async def test_wait_for_fresh_advertisement_fast_path(
    hass: HomeAssistant,
) -> None:
    """Fast path: registry has a connectable BLEDevice -> return immediately.

    ``async_register_callback`` MUST NOT be invoked when the fast path
    succeeds, otherwise we'd leak a callback for every successful
    connect.
    """
    _, coordinator = await _setup(hass)
    fake_device = MagicMock(spec=BLEDevice)
    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=fake_device,
    ) as mock_lookup, patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback"
    ) as mock_register:
        result = await coordinator._wait_for_fresh_advertisement()
    assert result is fake_device
    mock_lookup.assert_called_once()
    mock_register.assert_not_called()


async def test_async_ble_device_lookup_uses_uppercase_address(
    hass: HomeAssistant,
) -> None:
    """``async_ble_device_from_address`` is always called with UPPERCASE.

    Regression test for v0.1.5: HA's bluetooth manager keys its internal
    device-history dictionaries by uppercase address and does a plain
    ``dict.get()`` with no normalization. The coordinator stored a
    lowercase ``format_mac()`` address, so every lookup missed and
    returned ``None`` even right after a connectable advertisement was
    received. The coordinator now keeps an uppercase ``_ble_address``
    for all HA bluetooth-API lookups.
    """
    _, coordinator = await _setup(hass)
    # The stored config-entry address is lowercase ...
    assert coordinator.entry.data[CONF_ADDRESS] == TEST_MAC_NORMALIZED
    # ... but the bluetooth-API lookup address is uppercase.
    assert coordinator._ble_address == TEST_MAC_NORMALIZED.upper()
    # The lowercase identifier form is untouched (device registry / unique_ids).
    assert coordinator.mac_normalized == TEST_MAC_NORMALIZED

    fake_device = MagicMock(spec=BLEDevice)
    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=fake_device,
    ) as mock_lookup, patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback"
    ):
        # Both the sync helper and the async wait must use the uppercase form.
        coordinator._resolve_ble_device()
        await coordinator._wait_for_fresh_advertisement()

    assert mock_lookup.call_count == 2
    for call in mock_lookup.call_args_list:
        address_arg = call.args[1]
        assert address_arg == TEST_MAC_NORMALIZED.upper()
        assert address_arg == address_arg.upper()


async def test_wait_for_fresh_advertisement_slow_path_arrival(
    hass: HomeAssistant,
) -> None:
    """Slow path: lookup misses, advertisement arrives, second lookup hits.

    Patches ``async_register_callback`` to capture the registered
    callback, then drives it from the test to simulate an
    advertisement arriving.
    """
    _, coordinator = await _setup(hass)
    fake_device = MagicMock(spec=BLEDevice)
    captured: dict[str, object] = {}

    service_info = MagicMock()
    service_info.address = TEST_MAC_NORMALIZED

    def _capture_register(_hass, cb, _matcher, _mode):
        captured["cb"] = cb
        # Fire the advertisement on the loop as soon as it spins.
        hass.loop.call_soon(cb, service_info, MagicMock())
        return MagicMock()

    # First lookup (fast path) returns None; second (post-event) returns device.
    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        side_effect=[None, fake_device],
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        side_effect=_capture_register,
    ):
        result = await coordinator._wait_for_fresh_advertisement()

    assert result is fake_device
    assert "cb" in captured


async def test_wait_for_fresh_advertisement_registers_service_uuid_matcher(
    hass: HomeAssistant,
) -> None:
    """The slow-path callback is registered with a service_uuid matcher.

    Regression test for v0.1.4: the callback used to be registered with
    ``BluetoothCallbackMatcher(address=...)`` keyed on a lowercase
    address. HA represents advertisement addresses uppercase and the
    matcher compares case-sensitively, so the callback never fired. The
    fix matches on the Atorch service UUID instead.
    """
    _, coordinator = await _setup(hass)
    fake_device = MagicMock(spec=BLEDevice)
    captured: dict[str, object] = {}

    service_info = MagicMock()
    service_info.address = TEST_MAC_NORMALIZED

    def _capture_register(_hass, cb, matcher, _mode):
        captured["matcher"] = matcher
        hass.loop.call_soon(cb, service_info, MagicMock())
        return MagicMock()

    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        side_effect=[None, fake_device],
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        side_effect=_capture_register,
    ):
        await coordinator._wait_for_fresh_advertisement()

    matcher = captured["matcher"]
    assert matcher.get("service_uuid") == ATORCH_SERVICE_UUID
    # The matcher must NOT be keyed on address (the v0.1.3-and-earlier bug).
    assert "address" not in matcher


async def test_wait_for_fresh_advertisement_address_case_mismatch_resolves(
    hass: HomeAssistant,
) -> None:
    """An advertisement whose address differs only in CASE still resolves.

    Regression test for the exact v0.1.4 bug: HA delivers advertisement
    addresses in UPPERCASE while the coordinator's stored address is
    lowercase. The in-callback filter must compare case-insensitively,
    so an UPPERCASE ``service_info.address`` resolves the wait.
    """
    _, coordinator = await _setup(hass)
    assert coordinator.entry.data[CONF_ADDRESS] == TEST_MAC_NORMALIZED
    fake_device = MagicMock(spec=BLEDevice)

    service_info = MagicMock()
    # HA delivers the address uppercase; stored address is lowercase.
    service_info.address = TEST_MAC_NORMALIZED.upper()

    def _capture_register(_hass, cb, _matcher, _mode):
        hass.loop.call_soon(cb, service_info, MagicMock())
        return MagicMock()

    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        side_effect=[None, fake_device],
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        side_effect=_capture_register,
    ):
        result = await coordinator._wait_for_fresh_advertisement()

    assert result is fake_device


async def test_wait_for_fresh_advertisement_ignores_other_address(
    hass: HomeAssistant,
) -> None:
    """An advertisement from a DIFFERENT address does NOT resolve the wait.

    The callback fires for any Atorch meter in range (it is registered
    on the shared service UUID); the in-callback address filter must
    drop advertisements that are not from our meter, so the wait times
    out rather than connecting to the wrong device.
    """
    _, coordinator = await _setup(hass)

    other_info = MagicMock()
    other_info.address = "11:22:33:44:55:66"  # a different Atorch meter

    def _capture_register(_hass, cb, _matcher, _mode):
        hass.loop.call_soon(cb, other_info, MagicMock())
        return MagicMock()

    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        side_effect=_capture_register,
    ), patch(
        "custom_components.atorch_ble.coordinator.ADVERTISEMENT_WAIT_TIMEOUT_SECONDS",
        0.05,
    ):
        with pytest.raises(BleakError, match="No advertisement"):
            await coordinator._wait_for_fresh_advertisement()


async def test_wait_for_fresh_advertisement_timeout_raises(
    hass: HomeAssistant,
) -> None:
    """Slow path with no advertisement within timeout -> BleakError."""
    _, coordinator = await _setup(hass)

    # Patch the timeout constant so the test doesn't have to wait 10 min.
    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        return_value=None,
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        return_value=MagicMock(),
    ), patch(
        "custom_components.atorch_ble.coordinator.ADVERTISEMENT_WAIT_TIMEOUT_SECONDS",
        0.05,
    ):
        with pytest.raises(BleakError, match="No advertisement"):
            await coordinator._wait_for_fresh_advertisement()


async def test_wait_for_fresh_advertisement_log_includes_scanner_details(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The 'Advertisement received' INFO log includes source/rssi/connectable.

    Regression test for v0.1.3: the callback now logs which scanner
    delivered the advert (``source``), its RSSI, and whether the
    scanner is connectable-eligible — so users debugging connection
    issues against mixed passive/active BT proxy fleets can see which
    proxy caught the advert. The matcher is also relaxed to
    address-only, but verifying log content is the cheapest faithful
    regression assertion: if the registered matcher were still
    ``connectable=True`` against a passive-source advert, the
    callback would never fire and the log line would never emit.
    """
    _, coordinator = await _setup(hass)
    fake_device = MagicMock(spec=BLEDevice)

    service_info = MagicMock()
    # HA delivers the address uppercase; the in-callback filter compares
    # case-insensitively against the lowercase stored address.
    service_info.address = TEST_MAC_NORMALIZED.upper()
    service_info.source = "AA:BB:CC:DD:EE:FF"
    service_info.rssi = -72
    service_info.connectable = False  # advert came in via passive-only scanner

    def _capture_register(_hass, cb, _matcher, _mode):
        hass.loop.call_soon(cb, service_info, MagicMock())
        return MagicMock()

    with patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_ble_device_from_address",
        side_effect=[None, fake_device],
    ), patch(
        "custom_components.atorch_ble.coordinator.bluetooth.async_register_callback",
        side_effect=_capture_register,
    ), caplog.at_level(logging.INFO, logger="custom_components.atorch_ble.coordinator"):
        result = await coordinator._wait_for_fresh_advertisement()

    assert result is fake_device
    advert_lines = [
        r.getMessage() for r in caplog.records if "Advertisement received" in r.message
    ]
    assert len(advert_lines) == 1
    line = advert_lines[0]
    assert "source=AA:BB:CC:DD:EE:FF" in line
    assert "rssi=-72" in line
    assert "connectable=False" in line


async def test_notification_callback_valid_frame(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """Feeding a valid J7-C frame updates last_reading and last_frame_at."""
    _, coordinator = await _setup(hass)
    frame = build_j7c_frame(voltage_v=5.0, current_a=1.5)
    coordinator._notification_callback(None, frame)
    await hass.async_block_till_done()
    assert coordinator.last_reading is not None
    assert coordinator.last_frame_at is not None
    assert abs(coordinator.last_reading.voltage_v - 5.0) < 0.01
    assert abs(coordinator.last_reading.current_a - 1.5) < 0.01


async def test_notification_callback_fires_native_listeners(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """A decoded frame fires native listeners and updates coordinator.data.

    Listeners registered through the base class's native
    ``async_add_listener`` must be invoked when ``_notification_callback``
    publishes a snapshot, and ``coordinator.data`` must reflect it.
    """
    _, coordinator = await _setup(hass)
    calls: list[None] = []
    coordinator.async_add_listener(lambda: calls.append(None))

    assert coordinator.data is None
    coordinator._notification_callback(None, build_j7c_frame(voltage_v=5.0, current_a=2.0))
    await hass.async_block_till_done()

    assert calls, "native listener was not fired on a decoded frame"
    assert coordinator.data is not None
    assert coordinator.data.power_w == 10.0


async def test_async_add_listener_honors_context(hass: HomeAssistant) -> None:
    """async_add_listener stores ``context`` and exposes it via async_contexts.

    The native base-class listener registry tracks a per-listener
    context object; ``async_contexts()`` yields the non-None contexts.
    """
    _, coordinator = await _setup(hass)
    sentinel = object()
    remove = coordinator.async_add_listener(lambda: None, context=sentinel)

    assert sentinel in set(coordinator.async_contexts())
    remove()
    assert sentinel not in set(coordinator.async_contexts())


async def test_notification_callback_unsupported_packet(
    hass: HomeAssistant,
) -> None:
    """An unsupported-type frame routes through the repair-issue path."""
    _, coordinator = await _setup(hass)
    # Build a minimal 36-byte frame with magic + direction + type 0x02.
    bad_frame = b"\xff\x55\x01\x02" + b"\x00" * 32
    coordinator._notification_callback(None, bad_frame)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, "unsupported_packet_type_0x02"
    )
    assert issue is not None


async def test_on_disconnected_callback_is_safe(hass: HomeAssistant) -> None:
    """bleak's disconnect callback only logs — never raises."""
    _, coordinator = await _setup(hass)
    # Should not raise.
    coordinator._on_disconnected_callback(MagicMock())


async def test_logging_once_discipline(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Exactly one WARNING per failure streak; subsequent failures DEBUG only.

    A successful connection resets the streak — a new failure after the
    success fires a fresh WARNING.
    """
    _, coordinator = await _setup(hass)

    logger_name = "custom_components.atorch_ble.coordinator"

    def _streak_warnings() -> int:
        return sum(
            1
            for r in caplog.records
            if r.name == logger_name
            and r.levelno == logging.WARNING
            and "BLE connection failed" in r.message
        )

    with caplog.at_level(logging.DEBUG, logger=logger_name):
        for _ in range(4):
            _drive_failure(coordinator)
        await hass.async_block_till_done()

        # Exactly one streak-WARNING after 4 failures (well under the
        # 5-failure raise threshold so no separate "Raised ..." warning).
        assert _streak_warnings() == 1

        # Reset streak via success; clear records to start fresh.
        coordinator._on_connect_success()
        await hass.async_block_till_done()
        caplog.clear()

        # New streak — first failure emits another WARNING.
        for _ in range(3):
            _drive_failure(coordinator)
        await hass.async_block_till_done()
        assert _streak_warnings() == 1


# ---------------------------------------------------------------------------
# v0.1.6 — polled mode waits for a DECODED frame (not a raw fragment)
# ---------------------------------------------------------------------------


async def test_decoded_reading_event_set_only_after_full_frame(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """A raw fragment does NOT set the decoded-reading event; a full frame does.

    The polled runner waits on ``_decoded_reading_event`` so it does not
    disconnect mid-frame. The event must stay unset while only partial
    notifications have arrived and only fire once the parser yields a
    complete ``UsbMeterReading``.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )
    event = asyncio.Event()
    coordinator._decoded_reading_event = event

    frame = build_j7c_frame(voltage_v=5.0, current_a=1.5)

    # Feed only the first half of the frame — a raw fragment. The parser
    # yields nothing, so the event must remain unset.
    coordinator._notification_callback(None, frame[:20])
    await hass.async_block_till_done()
    assert not event.is_set()
    assert coordinator.last_reading is None

    # Feed the remaining bytes — the parser now assembles a full frame.
    coordinator._notification_callback(None, frame[20:])
    await hass.async_block_till_done()
    assert event.is_set()
    assert coordinator.last_reading is not None


async def test_decoded_reading_event_none_is_noop(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """With no event armed (persistent mode), decoding a frame is a no-op.

    ``_decoded_reading_event`` is left ``None`` outside a polled cycle;
    the notification callback must not raise when a frame decodes.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    assert coordinator._decoded_reading_event is None
    coordinator._notification_callback(None, build_j7c_frame())
    await hass.async_block_till_done()
    assert coordinator.last_reading is not None


def _poll_service_info() -> MagicMock:
    """Return a fake service_info whose ``device`` is not a BLEDevice.

    Forces _poll_method onto the advertisement-wait fallback path so
    tests can patch ``_wait_for_fresh_advertisement`` to supply a
    controlled BLEDevice.
    """
    service_info = MagicMock()
    service_info.device = None
    return service_info


async def test_poll_method_does_not_disconnect_until_decoded_reading(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """_poll_method holds the connection until a frame fully decodes.

    Regression coverage for the v0.1.6 production bug: the cycle used to
    disconnect on the first raw notification, which is typically just a
    fragment, before the parser could reassemble a complete 36-byte
    frame. _poll_method must keep the connection open across fragmented
    notifications and disconnect only after a decoded ``UsbMeterReading``
    is published.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )

    fake_device = MagicMock(spec=BLEDevice)
    fake_client = MagicMock()
    fake_client.is_connected = False
    disconnect_calls: list[None] = []

    async def _fake_disconnect() -> None:
        disconnect_calls.append(None)

    fake_client.disconnect = _fake_disconnect

    frame = build_j7c_frame(voltage_v=12.0, current_a=2.0)

    async def _fake_start_notify(_uuid, callback) -> None:
        # Deliver a raw fragment first — this must NOT cause a disconnect.
        callback(None, frame[:20])
        assert not disconnect_calls, "disconnected on a raw fragment"
        # Then deliver the rest so the parser assembles a complete frame.
        callback(None, frame[20:])

    fake_client.start_notify = _fake_start_notify

    async def _fake_establish(*_args, **_kwargs):
        return fake_client

    async def _fake_wait_for_adv():
        return fake_device

    with patch.object(
        coordinator, "_wait_for_fresh_advertisement", _fake_wait_for_adv
    ), patch(
        "custom_components.atorch_ble.coordinator.establish_connection",
        _fake_establish,
    ):
        result = await coordinator._poll_method(_poll_service_info())

    # A complete reading was decoded ...
    assert coordinator.last_reading is not None
    # ... the snapshot is returned ...
    assert result is not None
    assert result.power_w == 24.0
    # ... and the disconnect happened only after that.
    assert len(disconnect_calls) >= 1


async def test_poll_method_happy_path_uses_service_info_device(
    hass: HomeAssistant, build_j7c_frame, make_service_info
) -> None:
    """_poll_method prefers the BLEDevice carried on service_info.

    When ``service_info.device`` is a real BLEDevice, _poll_method must
    connect with it directly and never fall through to the
    advertisement-wait helper.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )

    service_info = make_service_info(name="UC96_BLE", address=TEST_MAC_NORMALIZED)

    fake_client = MagicMock()
    fake_client.is_connected = False
    disconnect_calls: list[None] = []

    async def _fake_disconnect() -> None:
        disconnect_calls.append(None)

    fake_client.disconnect = _fake_disconnect

    frame = build_j7c_frame(voltage_v=5.0, current_a=1.0)

    async def _fake_start_notify(_uuid, callback) -> None:
        callback(None, frame)

    fake_client.start_notify = _fake_start_notify

    establish_args: dict[str, object] = {}

    async def _fake_establish(_cls, device, _name, **_kwargs):
        establish_args["device"] = device
        return fake_client

    async def _fail_wait_for_adv():
        raise AssertionError("advertisement-wait must not be used")

    with patch.object(
        coordinator, "_wait_for_fresh_advertisement", _fail_wait_for_adv
    ), patch(
        "custom_components.atorch_ble.coordinator.establish_connection",
        _fake_establish,
    ):
        result = await coordinator._poll_method(service_info)

    assert result is not None
    assert result.power_w == 5.0
    assert establish_args["device"] is service_info.device
    assert len(disconnect_calls) >= 1


async def test_poll_method_raises_bleak_error_on_connect_failure(
    hass: HomeAssistant,
) -> None:
    """_poll_method propagates a BleakError when the connection fails.

    A connect-layer BleakError must be re-raised so the base coordinator
    records ``last_poll_successful=False``. The failure is also counted
    for backoff/repair tracking.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )

    fake_device = MagicMock(spec=BLEDevice)

    async def _fake_wait_for_adv():
        return fake_device

    async def _fake_establish(*_args, **_kwargs):
        raise BleakError("connect failed")

    failures_before = coordinator._consecutive_connect_failures
    with patch.object(
        coordinator, "_wait_for_fresh_advertisement", _fake_wait_for_adv
    ), patch(
        "custom_components.atorch_ble.coordinator.establish_connection",
        _fake_establish,
    ):
        with pytest.raises(BleakError, match="connect failed"):
            await coordinator._poll_method(_poll_service_info())

    # The failure was recorded exactly once for backoff/repair tracking.
    assert coordinator._consecutive_connect_failures == failures_before + 1
    # The client handle is cleared after the failed cycle.
    assert coordinator._client is None


async def test_poll_method_raises_bleak_error_on_decode_timeout(
    hass: HomeAssistant,
) -> None:
    """_poll_method raises a BleakError when no frame decodes in time.

    A connection succeeds but no complete frame assembles within
    ``POLLED_NOTIFICATION_TIMEOUT_SECONDS``. The cycle surfaces a
    BleakError so the base records ``last_poll_successful=False``, and
    disconnects cleanly.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )

    fake_device = MagicMock(spec=BLEDevice)
    fake_client = MagicMock()
    fake_client.is_connected = False
    disconnect_calls: list[None] = []

    async def _fake_disconnect() -> None:
        disconnect_calls.append(None)

    fake_client.disconnect = _fake_disconnect

    async def _fake_start_notify(_uuid, _callback) -> None:
        # Never deliver a frame — the decoded-reading wait times out.
        return None

    fake_client.start_notify = _fake_start_notify

    async def _fake_establish(*_args, **_kwargs):
        return fake_client

    async def _fake_wait_for_adv():
        return fake_device

    failures_before = coordinator._consecutive_connect_failures
    with patch.object(
        coordinator, "_wait_for_fresh_advertisement", _fake_wait_for_adv
    ), patch(
        "custom_components.atorch_ble.coordinator.establish_connection",
        _fake_establish,
    ), patch(
        "custom_components.atorch_ble.coordinator."
        "POLLED_NOTIFICATION_TIMEOUT_SECONDS",
        0.01,
    ):
        with pytest.raises(BleakError, match="No decoded reading"):
            await coordinator._poll_method(_poll_service_info())

    assert coordinator._consecutive_connect_failures == failures_before + 1
    assert coordinator._client is None
    assert len(disconnect_calls) >= 1


# ---------------------------------------------------------------------------
# v0.1.9 — mode-switch connection-handle race
# ---------------------------------------------------------------------------


async def test_release_client_only_clears_own_handle(
    hass: HomeAssistant,
) -> None:
    """_release_client clears the shared handle only if it still owns it.

    Regression coverage for the v0.1.9 mode-switch race: a finishing
    poll must not null out a connection handle the persistent runner
    has since installed.
    """
    _, coordinator = await _setup(hass)

    own_client = MagicMock()
    foreign_client = MagicMock()

    # Releasing some other client leaves an installed foreign handle intact.
    coordinator._client = foreign_client
    coordinator._release_client(own_client)
    assert coordinator._client is foreign_client

    # Releasing the actually-installed handle clears it.
    coordinator._client = own_client
    coordinator._release_client(own_client)
    assert coordinator._client is None


async def test_poll_method_yields_to_persistent_mode_switch(
    hass: HomeAssistant, build_j7c_frame
) -> None:
    """A poll superseded by a switch to persistent mode yields cleanly.

    Regression coverage for the v0.1.9 wedge: when a polled->persistent
    switch starts the persistent runner while a poll is still finishing,
    the finishing poll must NOT (a) null out the persistent runner's
    connection handle or (b) stamp the polled-only "disconnected" state
    over the persistent runner's state machine.
    """
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_POLLED}
    )

    fake_device = MagicMock(spec=BLEDevice)
    poll_client = MagicMock()
    poll_client.is_connected = False
    persistent_client = MagicMock()

    async def _fake_disconnect() -> None:
        return None

    poll_client.disconnect = _fake_disconnect

    frame = build_j7c_frame(voltage_v=5.0, current_a=1.0)

    async def _fake_start_notify(_uuid, callback) -> None:
        # Simulate a polled->persistent switch landing mid-poll: the
        # persistent runner takes over the shared handle and the mode.
        coordinator._connection_mode = MODE_PERSISTENT
        coordinator._client = persistent_client
        # Deliver a full frame so the poll's decoded-reading wait returns.
        callback(None, frame)

    poll_client.start_notify = _fake_start_notify

    async def _fake_establish(*_args, **_kwargs):
        return poll_client

    async def _fake_wait_for_adv():
        return fake_device

    with patch.object(
        coordinator, "_wait_for_fresh_advertisement", _fake_wait_for_adv
    ), patch(
        "custom_components.atorch_ble.coordinator.establish_connection",
        _fake_establish,
    ):
        await coordinator._poll_method(_poll_service_info())

    # The persistent runner's handle survived the finishing poll ...
    assert coordinator._client is persistent_client
    # ... and the poll did not stamp the polled-only resting state.
    assert coordinator._connection_state != "disconnected"
