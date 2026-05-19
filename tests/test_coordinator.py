"""Tests for the atorch_ble coordinator.

Covers initial connection_state per mode, parser_error_rate_5m rolling
window, unsupported-packet repair-issue + persistent-dismissal +
title/model rewriting, and the cannot_connect_after_setup
5-failure-raise / 50-failure-re-raise / clear-on-success lifecycle.
"""

from __future__ import annotations

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


async def test_update_method_and_poll_method(
    hass: HomeAssistant,
) -> None:
    """ActiveBluetoothProcessorCoordinator hooks pass through to _snapshot."""
    _, coordinator = await _setup(hass)
    # Both methods return None pre-first-reading.
    assert coordinator._update_method(MagicMock()) is None
    assert await coordinator._poll_method(MagicMock()) is None


async def test_needs_poll_method_persistent(hass: HomeAssistant) -> None:
    """Persistent mode polls only when no client is connected."""
    _, coordinator = await _setup(
        hass, options={CONF_CONNECTION_MODE: MODE_PERSISTENT}
    )
    assert coordinator._needs_poll_method(MagicMock(), None) is True

    fake_client = MagicMock()
    fake_client.is_connected = True
    coordinator._client = fake_client
    assert coordinator._needs_poll_method(MagicMock(), 0.0) is False

    fake_client.is_connected = False
    assert coordinator._needs_poll_method(MagicMock(), 0.0) is True


async def test_needs_poll_method_polled(hass: HomeAssistant) -> None:
    """Polled mode polls based on elapsed time vs. interval."""
    _, coordinator = await _setup(
        hass,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )
    # last_poll None -> always polls.
    assert coordinator._needs_poll_method(MagicMock(), None) is True
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=30.0,
    ):
        # 30s elapsed against a 60s interval — no poll yet.
        assert coordinator._needs_poll_method(MagicMock(), 0.0) is False
    with patch(
        "custom_components.atorch_ble.coordinator.time.monotonic",
        return_value=70.0,
    ):
        # 70s elapsed — poll due.
        assert coordinator._needs_poll_method(MagicMock(), 0.0) is True


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

    def _capture_register(_hass, cb, _matcher, _mode):
        captured["cb"] = cb
        # Fire the advertisement on the loop as soon as it spins.
        hass.loop.call_soon(cb, MagicMock(), MagicMock())
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
    """Feeding a valid J7-C frame updates last_reading and last_seen."""
    _, coordinator = await _setup(hass)
    frame = build_j7c_frame(voltage_v=5.0, current_a=1.5)
    coordinator._notification_callback(None, frame)
    await hass.async_block_till_done()
    assert coordinator.last_reading is not None
    assert coordinator.last_seen is not None
    assert abs(coordinator.last_reading.voltage_v - 5.0) < 0.01
    assert abs(coordinator.last_reading.current_a - 1.5) < 0.01


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
