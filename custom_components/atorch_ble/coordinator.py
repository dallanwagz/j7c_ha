"""Coordinator for the atorch_ble integration.

Owns the BLE connection lifecycle to a single Atorch USB-meter (J7-C
family) and republishes parsed :class:`UsbMeterReading` frames via a
:class:`ActiveBluetoothProcessorCoordinator`. Two operating modes are
supported:

* **persistent** — hold a continuous GATT connection and subscribe to
  notifications on :data:`~atorch_ble.const.CHARACTERISTIC_UUID`. The
  notification stream drives data freshness. On disconnect the runner
  reconnects with capped exponential backoff.
* **polled** — every ``poll_interval_seconds``: connect, await one
  notification, disconnect, sleep. Easier on ESPHome-proxy connection
  slot budgets.

The two modes are implemented by a single private ``_runner`` task that
calls a mode-specific inner coroutine. Mode and interval changes from
the options flow drain the active runner and start a new one in place
via the options-update listener — no HA restart required, and HA-side
``last_reading``/``last_seen`` survive the transition.

Repair issues, device-registry ``model`` sync, and config-entry title
rewriting for unsupported packet types are all driven from the
notification callback path; see :meth:`_handle_unsupported_packet_type`
and :meth:`_handle_decoded_frame`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from atorch_ble import (
    AtorchBleParser,
    InvalidPacket,
    UnsupportedPacketType,
    UsbMeterReading,
)
from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.loader import async_get_integration

from .const import (
    ACK_UNSUPPORTED_KEY,
    CHARACTERISTIC_UUID,
    CONF_CONNECTION_MODE,
    CONF_POLL_INTERVAL_SECONDS,
    CONNECT_FAILURE_RAISE_THRESHOLD,
    CONNECT_FAILURE_RERAISE_INTERVAL,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    ISSUE_CANNOT_CONNECT,
    ISSUE_UNSUPPORTED_PACKET_TYPE_PREFIX,
    MODE_PERSISTENT,
    MODE_POLLED,
    PACKET_TYPE_TO_MODEL,
    POLLED_NOTIFICATION_TIMEOUT_SECONDS,
    RECONNECT_INITIAL_BACKOFF_SECONDS,
    RECONNECT_MAX_BACKOFF_SECONDS,
    packet_type_family,
)

_LOGGER = logging.getLogger(__name__)

# Bucket settings for parser_error_rate_5m: 10 × 30s = 5 minutes.
_BUCKET_SECONDS = 30
_BUCKET_COUNT = 10
_WINDOW_SECONDS = _BUCKET_SECONDS * _BUCKET_COUNT  # 300

# Closed set of connection_state values (used in diagnostics legend).
ConnectionState = str
_VALID_STATES = frozenset(
    {"connected", "polling", "disconnected", "reconnecting", "failed_after_setup"}
)

# Packet type successfully decoded by the J7-C path. The parser only
# yields UsbMeterReading for type 0x03 today; harden against future
# expansion by accepting an explicit override if the parser ever exposes
# a per-reading type byte.
_SUCCESS_PACKET_TYPE = 0x03


@dataclasses.dataclass(frozen=True, slots=True)
class AtorchBleData:
    """Coordinator-published data snapshot.

    ``power_w`` is coordinator-derived (``voltage_v * current_a``); the
    parser library is intentionally protocol-pure and does not compute
    it. Sensor entities read ``data.reading.<field>`` for parsed values
    and ``data.power_w`` for the computed product.
    """

    reading: UsbMeterReading
    power_w: float


class AtorchBleCoordinator(
    ActiveBluetoothProcessorCoordinator[AtorchBleData | None]
):
    """Active-Bluetooth coordinator for an Atorch USB-meter config entry.

    Public attributes consumed by other batches:

    * ``mac_normalized`` — canonical MAC ``format_mac()`` string.
    * ``last_reading`` — most recent :class:`UsbMeterReading` or ``None``.
    * ``last_seen`` — UTC datetime of most recent successful frame.
    * ``connection_state`` — one of the closed-set strings above.
    * ``parser_error_count`` — canonical re-export of the parser
      library's ``error_count``.
    * ``parser_error_rate_5m`` — rolling 5-min error rate (0..1).
    * ``expected_cadence_seconds`` — for batch-3/03 staleness debouncing.
    """

    # Class name is locked by the ticket; do not rename.

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator from a loaded config entry."""
        self.hass = hass
        self.entry = entry
        self.mac_normalized: str = format_mac(entry.data[CONF_ADDRESS])

        self._parser = AtorchBleParser()

        # Mode + poll interval are read from options (with const defaults
        # for first-load entries that pre-date the options flow).
        self._connection_mode: str = entry.options.get(
            CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE
        )
        self._poll_interval_seconds: int = int(
            entry.options.get(
                CONF_POLL_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS
            )
        )

        # Persistent dismissal mirror — load on setup, write through on
        # first observation of a new unsupported byte.
        self._acked_unsupported: set[int] = set(
            entry.data.get(ACK_UNSUPPORTED_KEY, [])
        )

        # Connection-state machine.
        self._connection_state: ConnectionState = (
            "reconnecting" if self._connection_mode == MODE_PERSISTENT else "disconnected"
        )

        # Latest snapshot fields.
        self._last_reading: UsbMeterReading | None = None
        self._last_seen: datetime | None = None

        # Failure tracking + log-once discipline.
        self._consecutive_connect_failures: int = 0
        self._failures_since_last_raise: int = 0
        self._cannot_connect_issue_raised: bool = False
        self._warned_about_current_failure_streak: bool = False

        # Rolling-window buckets: list of (bucket_start_monotonic, notifs, errors)
        # newest-last. Pruned lazily on access.
        self._buckets: list[list[float | int]] = []

        # Background runner task + shared client handle.
        self._runner_task: asyncio.Task[None] | None = None
        self._client: BleakClient | None = None

        # Resolved issue_tracker URL — cached after first lookup.
        self._issue_url: str | None = None

        # Last model string written to the device registry — avoid
        # redundant registry writes.
        self._last_model_written: str | None = None

        # Whether async_start has been called and the underlying base
        # coordinator is live; used to gate option-update transitions.
        self._started: bool = False

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=entry.data[CONF_ADDRESS],
            mode=BluetoothScanningMode.ACTIVE,
            update_method=self._update_method,
            needs_poll_method=self._needs_poll_method,
            poll_method=self._poll_method,
        )

        # Register the options-update listener via async_on_unload so HA
        # tears it down automatically when the entry unloads. The
        # coordinator stores no unsubscribe handle (per ticket).
        entry.async_on_unload(entry.add_update_listener(self._async_options_updated))

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def connection_state(self) -> ConnectionState:
        """Return the current connection-state machine value."""
        return self._connection_state

    @property
    def last_reading(self) -> UsbMeterReading | None:
        """Return the most recently decoded reading, or ``None``."""
        return self._last_reading

    @property
    def last_seen(self) -> datetime | None:
        """Return the UTC datetime of the most recent successful frame."""
        return self._last_seen

    @property
    def expected_cadence_seconds(self) -> int:
        """Return the cadence batch-3/03 should use for staleness debouncing.

        In persistent mode the device sends a frame roughly every second,
        so we report 1s. In polled mode we report the configured poll
        interval.
        """
        if self._connection_mode == MODE_PERSISTENT:
            return 1
        return self._poll_interval_seconds

    @property
    def parser_error_count(self) -> int:
        """Re-export the parser's swallowed-InvalidPacket count under the canonical name."""
        return self._parser.error_count

    @property
    def parser_error_rate_5m(self) -> float:
        """Return ``errors / notifications`` over the trailing 5 minutes.

        Buckets older than ``now - 300s`` (measured against the current
        monotonic clock) are pruned before the ratio is computed. This
        also subsumes any clock-backward rewind: ordinary pruning drops
        every bucket whose start lies outside the new window. Returns
        ``0.0`` when no notifications have been seen in the window.
        """
        self._prune_buckets(time.monotonic())
        if not self._buckets:
            return 0.0
        total_notifs = sum(int(b[1]) for b in self._buckets)
        total_errors = sum(int(b[2]) for b in self._buckets)
        return total_errors / max(1, total_notifs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> Callable[[], None]:
        """Start the base coordinator and the BLE runner.

        Returns the base coordinator's stop callable so callers can
        compose teardown if they wish; ``async_unload`` is the supported
        path though.
        """
        # async_start() on the base class is sync-returning in current
        # HA Core; we await defensively in case that ever changes.
        unload_base = super().async_start()
        self._started = True
        self._start_runner()
        return unload_base

    async def async_unload(self) -> None:
        """Cancel runner and disconnect any open client."""
        self._started = False
        await self._stop_runner()

    # ------------------------------------------------------------------
    # ActiveBluetoothProcessorCoordinator hooks
    # ------------------------------------------------------------------

    @callback
    def _update_method(
        self, service_info: BluetoothServiceInfoBleak
    ) -> AtorchBleData | None:
        """Return the most-recent published snapshot.

        Atorch meters do not encode measurements in advertisements — the
        data comes from GATT notifications — so this hook is essentially
        a passthrough that returns the latest data the notification path
        has stored.
        """
        return self._snapshot()

    @callback
    def _needs_poll_method(
        self,
        service_info: BluetoothServiceInfoBleak,
        last_poll: float | None,
    ) -> bool:
        """Tell the framework whether to invoke ``poll_method`` now.

        * Persistent mode: only when no connection is held — the
          framework "polls" us to (re)establish the subscription.
        * Polled mode: when the interval has elapsed since the last
          successful poll.
        """
        if self._connection_mode == MODE_PERSISTENT:
            return self._client is None or not self._client.is_connected
        if last_poll is None:
            return True
        return (time.monotonic() - last_poll) >= self._poll_interval_seconds

    async def _poll_method(
        self, service_info: BluetoothServiceInfoBleak
    ) -> AtorchBleData | None:
        """Return the latest snapshot.

        The actual connect/notify logic lives in :meth:`_run_persistent`
        and :meth:`_run_polled` (driven by ``_runner_task``); this hook
        exists only to satisfy the framework contract and surface the
        most recent data.
        """
        return self._snapshot()

    def _snapshot(self) -> AtorchBleData | None:
        if self._last_reading is None:
            return None
        return AtorchBleData(
            reading=self._last_reading,
            power_w=self._last_reading.voltage_v * self._last_reading.current_a,
        )

    # ------------------------------------------------------------------
    # Options-update listener
    # ------------------------------------------------------------------

    async def _async_options_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Apply options-flow changes live without dropping HA-side state.

        Mode change → drain old runner and start a new one. Interval
        change while still polled → update in-place (no restart needed);
        the running cycle finishes at the old interval and the next
        cycle observes the new one.
        """
        new_mode = entry.options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
        new_interval = int(
            entry.options.get(
                CONF_POLL_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS
            )
        )

        mode_changed = new_mode != self._connection_mode
        interval_changed = new_interval != self._poll_interval_seconds

        self._poll_interval_seconds = new_interval

        if mode_changed:
            _LOGGER.info(
                "Connection mode change applied: %s -> %s (mac=%s)",
                self._connection_mode,
                new_mode,
                self.mac_normalized,
            )
            self._connection_mode = new_mode
            # Drain old runner before starting new one — never two alive.
            await self._stop_runner()
            self._connection_state = (
                "reconnecting" if new_mode == MODE_PERSISTENT else "disconnected"
            )
            if self._started:
                self._start_runner()
        elif interval_changed:
            _LOGGER.info(
                "Poll interval updated in-place: %ds (mac=%s)",
                new_interval,
                self.mac_normalized,
            )

    # ------------------------------------------------------------------
    # Runner task management
    # ------------------------------------------------------------------

    def _start_runner(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            return
        if self._connection_mode == MODE_PERSISTENT:
            self._runner_task = self.hass.async_create_background_task(
                self._run_persistent(), name=f"{DOMAIN}-runner-{self.mac_normalized}"
            )
        else:
            self._runner_task = self.hass.async_create_background_task(
                self._run_polled(), name=f"{DOMAIN}-runner-{self.mac_normalized}"
            )

    async def _stop_runner(self) -> None:
        task = self._runner_task
        self._runner_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Best-effort disconnect of any open client.
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    # ------------------------------------------------------------------
    # Persistent-mode runner
    # ------------------------------------------------------------------

    async def _run_persistent(self) -> None:
        """Hold one long-lived connection; reconnect with capped backoff."""
        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while True:
                self._connection_state = "reconnecting"
                try:
                    await self._connect_and_subscribe_persistent()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — broad on purpose
                    self._on_connect_failure(exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_SECONDS)
                    continue

                # Connection established and notify subscription live.
                self._on_connect_success()
                backoff = RECONNECT_INITIAL_BACKOFF_SECONDS

                # Block until the client disconnects. We poll is_connected
                # rather than wiring a disconnect event because bleak's
                # disconnect-callback semantics differ across backends;
                # a 1s heartbeat is plenty responsive for a meter that
                # ticks at ~1Hz.
                client = self._client
                try:
                    while client is not None and client.is_connected:
                        await asyncio.sleep(1.0)
                finally:
                    with contextlib.suppress(Exception):
                        if client is not None:
                            await client.disconnect()
                    self._client = None

                if self._connection_state != "failed_after_setup":
                    self._connection_state = "disconnected"
                _LOGGER.info(
                    "BLE disconnected (mac=%s); will reconnect", self.mac_normalized
                )
        except asyncio.CancelledError:
            return

    async def _connect_and_subscribe_persistent(self) -> None:
        ble_device = self._resolve_ble_device()
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"{DOMAIN}-{self.mac_normalized}",
            disconnected_callback=self._on_disconnected_callback,
        )
        try:
            await client.start_notify(
                CHARACTERISTIC_UUID, self._notification_callback
            )
        except Exception:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        self._client = client
        self._connection_state = "connected"

    @callback
    def _on_disconnected_callback(self, _client: BleakClient) -> None:
        """Disconnect callback — only logs; the heartbeat loop handles state."""
        _LOGGER.debug("bleak disconnect_callback fired (mac=%s)", self.mac_normalized)

    # ------------------------------------------------------------------
    # Polled-mode runner
    # ------------------------------------------------------------------

    async def _run_polled(self) -> None:
        """Connect-read-disconnect each cycle on the configured interval."""
        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while True:
                self._connection_state = "polling"
                got_reading = asyncio.Event()

                def _one_shot_callback(
                    _sender: Any, data: bytearray | bytes
                ) -> None:
                    self._notification_callback(_sender, data)
                    got_reading.set()

                client: BleakClient | None = None
                try:
                    ble_device = self._resolve_ble_device()
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        ble_device,
                        f"{DOMAIN}-{self.mac_normalized}",
                    )
                    self._client = client
                    await client.start_notify(
                        CHARACTERISTIC_UUID, _one_shot_callback
                    )
                    try:
                        await asyncio.wait_for(
                            got_reading.wait(),
                            timeout=POLLED_NOTIFICATION_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError as exc:
                        # Timeout counts as a failure for backoff/repair
                        # tracking, but we still loop on schedule.
                        self._on_connect_failure(exc)
                        backoff = min(
                            backoff * 2, RECONNECT_MAX_BACKOFF_SECONDS
                        )
                    else:
                        self._on_connect_success()
                        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._on_connect_failure(exc)
                    backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_SECONDS)
                finally:
                    if client is not None:
                        with contextlib.suppress(Exception):
                            await client.disconnect()
                    self._client = None
                    if self._connection_state != "failed_after_setup":
                        self._connection_state = "disconnected"

                # Sleep until next cycle. On failure, prefer the longer
                # of (backoff, poll_interval) so we don't hammer.
                sleep_for = (
                    max(backoff, self._poll_interval_seconds)
                    if self._consecutive_connect_failures > 0
                    else self._poll_interval_seconds
                )
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # BLE device resolution
    # ------------------------------------------------------------------

    def _resolve_ble_device(self) -> BLEDevice:
        """Resolve the BLEDevice handle from the HA bluetooth registry.

        Raises ``BleakError`` if the device hasn't been seen recently —
        that's transient and caller treats it as a connect failure.
        """
        address = self.entry.data[CONF_ADDRESS]
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, address, connectable=True
        )
        if ble_device is None:
            raise BleakError(
                f"BLEDevice for {address} not currently in HA bluetooth registry"
            )
        return ble_device

    # ------------------------------------------------------------------
    # Notification handling
    # ------------------------------------------------------------------

    @callback
    def _notification_callback(
        self, _sender: Any, data: bytearray | bytes
    ) -> None:
        """bleak notification callback — sync; schedules async work on hass."""
        now = time.monotonic()
        self._increment_bucket(now, notifs=1, errors=0)
        try:
            readings = self._parser.feed(bytes(data))
        except UnsupportedPacketType as exc:
            self._increment_bucket(now, notifs=0, errors=1)
            # Schedule async repair-issue/title work on the loop.
            self.hass.async_create_task(
                self._handle_unsupported_packet_type(exc.packet_type)
            )
            return
        except InvalidPacket:
            # AtorchBleParser.feed should swallow InvalidPacket internally
            # and increment its own counter, but catch defensively in case
            # of future API changes.
            self._increment_bucket(now, notifs=0, errors=1)
            _LOGGER.debug("InvalidPacket from parser (mac=%s)", self.mac_normalized)
            return

        for reading in readings:
            _LOGGER.debug(
                "Decoded reading mac=%s V=%.3f I=%.3f",
                self.mac_normalized,
                reading.voltage_v,
                reading.current_a,
            )
            self._last_reading = reading
            self._last_seen = datetime.now(timezone.utc)
            # Idempotent device-registry model sync for the success case.
            self.hass.async_create_task(
                self._handle_decoded_frame(_SUCCESS_PACKET_TYPE)
            )

        if readings:
            # Push fresh snapshot through the base coordinator's listeners.
            self.async_set_updated_data(self._snapshot())

    # ------------------------------------------------------------------
    # Bucket bookkeeping
    # ------------------------------------------------------------------

    def _increment_bucket(self, now: float, *, notifs: int, errors: int) -> None:
        self._prune_buckets(now)
        bucket_start = (int(now) // _BUCKET_SECONDS) * _BUCKET_SECONDS
        if self._buckets and int(self._buckets[-1][0]) == bucket_start:
            self._buckets[-1][1] = int(self._buckets[-1][1]) + notifs
            self._buckets[-1][2] = int(self._buckets[-1][2]) + errors
            return
        self._buckets.append([float(bucket_start), notifs, errors])
        if len(self._buckets) > _BUCKET_COUNT:
            # Drop oldest above cap; pruning by age handles most cases
            # but this guards against pathological many-bucket growth.
            self._buckets = self._buckets[-_BUCKET_COUNT:]

    def _prune_buckets(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        self._buckets = [b for b in self._buckets if float(b[0]) >= cutoff]

    # ------------------------------------------------------------------
    # Connection success / failure tracking + repair issues
    # ------------------------------------------------------------------

    def _on_connect_success(self) -> None:
        """Mark a successful connection; clear failure repair issue if raised."""
        if self._consecutive_connect_failures > 0:
            _LOGGER.info(
                "BLE connection recovered (mac=%s); %d failures cleared",
                self.mac_normalized,
                self._consecutive_connect_failures,
            )
        self._consecutive_connect_failures = 0
        self._failures_since_last_raise = 0
        self._warned_about_current_failure_streak = False
        self._connection_state = "connected"
        if self._cannot_connect_issue_raised:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CANNOT_CONNECT)
            self._cannot_connect_issue_raised = False
            _LOGGER.info(
                "Cleared cannot_connect_after_setup repair issue (mac=%s)",
                self.mac_normalized,
            )

    def _on_connect_failure(self, exc: BaseException) -> None:
        """Increment failure counters and emit log/repair issue per discipline."""
        self._consecutive_connect_failures += 1
        self._failures_since_last_raise += 1

        if not self._warned_about_current_failure_streak:
            _LOGGER.warning(
                "BLE connection failed (mac=%s): %s",
                self.mac_normalized,
                exc,
            )
            self._warned_about_current_failure_streak = True
        else:
            _LOGGER.debug(
                "BLE connection failed again (mac=%s, streak=%d): %s",
                self.mac_normalized,
                self._consecutive_connect_failures,
                exc,
            )

        # Raise/re-raise the repair issue per the 5 / +50 cadence.
        # Re-raise path uses explicit delete+create to force a fresh
        # notification in case HA Core's dismissal is sticky against
        # bare async_create_issue calls.
        if (
            not self._cannot_connect_issue_raised
            and self._consecutive_connect_failures >= CONNECT_FAILURE_RAISE_THRESHOLD
        ):
            self.hass.async_create_task(self._raise_cannot_connect_issue())
        elif (
            self._cannot_connect_issue_raised
            and self._failures_since_last_raise >= CONNECT_FAILURE_RERAISE_INTERVAL
        ):
            self.hass.async_create_task(self._reraise_cannot_connect_issue())

    async def _raise_cannot_connect_issue(self) -> None:
        device_name = self._resolve_device_name()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_CANNOT_CONNECT,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_CANNOT_CONNECT,
            translation_placeholders={"device_name": device_name},
        )
        self._cannot_connect_issue_raised = True
        self._failures_since_last_raise = 0
        self._connection_state = "failed_after_setup"
        _LOGGER.warning(
            "Raised cannot_connect_after_setup repair (mac=%s, name=%s)",
            self.mac_normalized,
            device_name,
        )

    async def _reraise_cannot_connect_issue(self) -> None:
        """Force-resurface the cannot_connect issue after 50 more failures.

        Implementation choice: explicit delete + create cycle. HA Core's
        dismissal semantics treat a fresh async_create_issue on a
        previously-dismissed issue id as a no-op in some versions; the
        delete-then-create cycle is safe across both code paths.
        """
        with contextlib.suppress(Exception):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CANNOT_CONNECT)
        await self._raise_cannot_connect_issue()

    def _resolve_device_name(self) -> str:
        """Resolve the user-facing device name per the UX-locked rule."""
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.entry.data[CONF_ADDRESS])}
        )
        if device is None:
            return self.entry.title
        return device.name_by_user or device.name or self.entry.title

    # ------------------------------------------------------------------
    # Unsupported packet type + device model sync
    # ------------------------------------------------------------------

    async def _handle_unsupported_packet_type(self, packet_type: int) -> None:
        """Persist acknowledgement + raise repair issue for a new unsupported byte.

        Steps (deliberately NOT transactional — see ticket Implementation
        Notes): (1) raise repair issue, (2) update entry title, (3)
        update device-registry model, (4) persist acknowledgement to
        entry.data. A crash between any two steps may re-fire on next
        boot, which is acceptable.
        """
        # Idempotent device-registry model update first (also done for
        # already-acknowledged bytes — cheap, no-op when value matches).
        await self._update_device_model(packet_type)

        if packet_type in self._acked_unsupported:
            _LOGGER.debug(
                "Unsupported packet type 0x%02X already acknowledged (mac=%s)",
                packet_type,
                self.mac_normalized,
            )
            return

        issue_url = await self._resolve_issue_url()
        issue_id = f"{ISSUE_UNSUPPORTED_PACKET_TYPE_PREFIX}{packet_type:02X}"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="unsupported_packet_type",
            translation_placeholders={
                "packet_type": f"{packet_type:02X}",
                "packet_type_family": packet_type_family(packet_type),
                "issue_url": issue_url,
            },
        )
        _LOGGER.warning(
            "Unsupported Atorch packet type 0x%02X observed (mac=%s, family=%s)",
            packet_type,
            self.mac_normalized,
            packet_type_family(packet_type),
        )

        # Update entry title to reflect the unknown-device finding.
        mac_last4_upper = self.mac_normalized.replace(":", "")[-4:].upper()
        new_title = f"Atorch unknown ({mac_last4_upper})"
        self._acked_unsupported.add(packet_type)
        new_data = {
            **self.entry.data,
            ACK_UNSUPPORTED_KEY: sorted(self._acked_unsupported),
        }
        try:
            self.hass.config_entries.async_update_entry(
                self.entry, title=new_title, data=new_data
            )
        except Exception:  # noqa: BLE001 — defensive
            _LOGGER.exception(
                "Failed to persist unsupported-packet acknowledgement (mac=%s)",
                self.mac_normalized,
            )

    async def _handle_decoded_frame(self, packet_type: int) -> None:
        """Idempotent device-registry model sync for a successful frame."""
        await self._update_device_model(packet_type)

    async def _update_device_model(self, packet_type: int) -> None:
        model = PACKET_TYPE_TO_MODEL.get(
            packet_type, f"Unknown Atorch device (type 0x{packet_type:02X})"
        )
        if model == self._last_model_written:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, self.entry.data[CONF_ADDRESS])}
        )
        if device is None:
            # Device entry not created yet — the sensor platform creates
            # it on first entity add. We'll try again on the next frame.
            return
        if device.model == model:
            self._last_model_written = model
            return
        registry.async_update_device(device.id, model=model)
        self._last_model_written = model

    async def _resolve_issue_url(self) -> str:
        """Fetch the manifest's issue_tracker URL — cached after first call."""
        if self._issue_url is not None:
            return self._issue_url
        try:
            integration = await async_get_integration(self.hass, DOMAIN)
            self._issue_url = integration.manifest.get("issue_tracker", "") or ""
        except Exception:  # noqa: BLE001
            self._issue_url = ""
        return self._issue_url
