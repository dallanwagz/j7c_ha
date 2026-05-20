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

**Listener-shim rationale.** :class:`PassiveBluetoothProcessorEntity`
is the native HA entity class for
:class:`~homeassistant.components.bluetooth.active_update_processor.ActiveBluetoothProcessorCoordinator`,
but it is advertisement-driven: entities refresh on each BLE
advertisement received by the scanner. Atorch measurements arrive via
GATT notifications, not advertisements — the advertisement stream is
only used for discovery and as a liveness trigger for the framework's
``needs_poll_method`` hook. Using ``PassiveBluetoothProcessorEntity``
directly would therefore produce entities that only update when an
advertisement is seen, not when a measurement arrives. Instead,
:class:`AtorchBleCoordinator` inherits from
:class:`~homeassistant.helpers.update_coordinator.CoordinatorEntity`
(via ``CoordinatorEntity[AtorchBleCoordinator]`` in ``sensor.py``) and
shims the ``async_add_listener`` / ``async_set_updated_data`` methods
that ``CoordinatorEntity.async_added_to_hass`` expects. This gives
entities the correct ``DataUpdateCoordinator``-style subscription
semantics driven by GATT notification delivery.

**Repair-issue dismissal asymmetry.** Two repair issues are raised with
deliberately different dismissal persistence:

* ``unsupported_packet_type`` — acknowledgement is persisted to
  ``entry.data`` (see ``ACK_UNSUPPORTED_KEY``). This is intentional:
  the issue represents a one-time discovery of an unsupported device
  variant; re-surfacing it on every HA restart would be noisy and
  unhelpful because the user cannot change the packet type the physical
  device emits.

* ``cannot_connect_after_setup`` — dismissal is runtime-only; the issue
  re-raises at ``CONNECT_FAILURE_RAISE_THRESHOLD`` (5) failures after
  setup, and re-raises again every ``CONNECT_FAILURE_RERAISE_INTERVAL``
  (50) additional failures thereafter. This is intentional: an
  unreachable device after an HA restart is a fresh condition the user
  needs to be notified about, so persisting the dismissal would hide a
  real problem.
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
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.loader import async_get_integration

from .const import (
    ACK_UNSUPPORTED_KEY,
    ADVERTISEMENT_WAIT_TIMEOUT_SECONDS,
    ATORCH_SERVICE_UUID,
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

        # HA's bluetooth manager keys its device dictionaries by UPPERCASE
        # address. Use this form for every bluetooth.async_* lookup. The
        # lowercase mac_normalized stays for device-registry identifiers
        # and entity unique_ids, which must not change.
        self._ble_address = self.mac_normalized.upper()

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
        self._connection_state: str = (
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

        # First-notification-per-session log gate. Reset at the start of
        # each connection in both persistent and polled mode so the
        # "data flowing" INFO line emits exactly once per connection
        # attempt that successfully receives a frame.
        self._first_notification_logged: bool = False

        # Resolved issue_tracker URL — cached after first lookup.
        self._issue_url: str | None = None

        # Last model string written to the device registry — avoid
        # redundant registry writes.
        self._last_model_written: str | None = None

        # Whether async_start has been called and the underlying base
        # coordinator is live; used to gate option-update transitions.
        self._started: bool = False

        # Latest published snapshot. ``CoordinatorEntity`` and our sensor
        # entity both read ``coordinator.data``; the
        # ``PassiveBluetoothProcessorCoordinator`` base does not expose
        # this attribute (it dispatches updates through registered
        # processors instead), so we maintain it ourselves and refresh
        # it from inside ``async_set_updated_data``.
        self.data: AtorchBleData | None = None

        # CoordinatorEntity listener registry.
        # ``ActiveBluetoothProcessorCoordinator`` extends
        # ``PassiveBluetoothProcessorCoordinator``, NOT ``DataUpdateCoordinator``,
        # so the ``async_add_listener`` method that
        # ``CoordinatorEntity.async_added_to_hass`` invokes is absent from the
        # base class. Without this shim, every entity raises ``AttributeError``
        # the moment HA tries to subscribe it, and sensors would never receive
        # coordinator updates at runtime. The shim mirrors the
        # ``DataUpdateCoordinator`` API surface that ``CoordinatorEntity``
        # depends on: callable registration + idempotent removal.
        self._listeners: list[CALLBACK_TYPE] = []

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
    def connection_state(self) -> str:
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
    # CoordinatorEntity listener-registry shim
    # ------------------------------------------------------------------

    @callback
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> CALLBACK_TYPE:
        """Register a listener for entity updates.

        Mirrors ``DataUpdateCoordinator.async_add_listener`` so that
        ``CoordinatorEntity.async_added_to_hass`` can subscribe entities
        without ``AttributeError``. ``context`` is accepted for API
        parity but unused — every listener gets every update.
        """
        @callback
        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        self._listeners.append(update_callback)
        return remove_listener

    @callback
    def async_set_updated_data(self, update: AtorchBleData | None) -> None:
        """Publish a fresh snapshot.

        Wraps the base coordinator's dispatch with two responsibilities
        the base class does NOT cover:

        * Update ``self.data`` so that ``CoordinatorEntity.coordinator.data``
          (the canonical read path for sensor entities) reflects the
          latest snapshot. ``PassiveBluetoothProcessorCoordinator`` only
          fans out to registered processors and never stores ``data``.
        * Notify the entity listener registry so ``CoordinatorEntity``
          subscribers call ``async_write_ha_state`` and HA's state
          machine picks up the new value.
        """
        self.data = update
        super().async_set_updated_data(update)
        self._async_notify_listeners()

    @callback
    def _async_notify_listeners(self) -> None:
        """Invoke all registered listeners.

        Called whenever a fresh data snapshot is published. Iterates a
        copy so listener-side ``async_on_remove`` callbacks that mutate
        the list during iteration are safe.
        """
        for cb in list(self._listeners):
            cb()

    @callback
    def _set_connection_state(self, value: str) -> None:
        """Update the connection-state machine value and notify listeners.

        Direct assignment to ``self._connection_state`` does not trigger
        any HA-side state-write, so the ``connection_state`` diagnostic
        sensor (whose ``value_fn_coordinator`` reads this attribute on
        every render) would otherwise cache its first value forever.
        Funnelling every transition through this setter guarantees the
        sensor refreshes live. Idempotent on no-op transitions to avoid
        spurious listener storms during steady-state operation.
        """
        if self._connection_state == value:
            return
        self._connection_state = value
        self._async_notify_listeners()

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
            self._set_connection_state(
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

    async def _run_persistent(self) -> None:  # pragma: no cover - requires real bleak loop; covered by smoke test
        """Hold one long-lived connection; reconnect with capped backoff."""
        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while True:
                self._set_connection_state("reconnecting")
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
                    self._set_connection_state("disconnected")
                _LOGGER.info(
                    "Disconnected from %s; will reconnect", self.mac_normalized
                )
        except asyncio.CancelledError:
            return

    async def _connect_and_subscribe_persistent(self) -> None:  # pragma: no cover - requires real bleak loop; covered by smoke test
        ble_device = await self._wait_for_fresh_advertisement()
        _LOGGER.info("Attempting GATT connection to %s", self.mac_normalized)
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"{DOMAIN}-{self.mac_normalized}",
            disconnected_callback=self._on_disconnected_callback,
        )
        _LOGGER.info("GATT connection established to %s", self.mac_normalized)
        try:
            await client.start_notify(
                CHARACTERISTIC_UUID, self._notification_callback
            )
        except Exception:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        _LOGGER.info("Subscribed to notifications on %s", self.mac_normalized)
        self._client = client
        # Reset the per-session "first notification" log gate so the
        # next notification logs at INFO once. Set BEFORE the state
        # transition so a notification arriving during the listener
        # storm of the state change still logs the first-frame line.
        self._first_notification_logged = False
        self._set_connection_state("connected")

    @callback
    def _on_disconnected_callback(self, _client: BleakClient) -> None:
        """Disconnect callback — only logs; the heartbeat loop handles state."""
        _LOGGER.debug("bleak disconnect_callback fired (mac=%s)", self.mac_normalized)

    # ------------------------------------------------------------------
    # Polled-mode runner
    # ------------------------------------------------------------------

    async def _run_polled(self) -> None:  # pragma: no cover - requires real bleak loop; covered by smoke test
        """Connect-read-disconnect each cycle on the configured interval."""
        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while True:
                # Stay "disconnected" while we wait for an advertisement
                # — the wait can take up to ADVERTISEMENT_WAIT_TIMEOUT_SECONDS
                # (default 600s) and reporting "polling" during that
                # window misleads users into thinking the meter is
                # connected. Transition to "polling" only once we have
                # a BLEDevice and are actively trying to establish the
                # GATT connection.
                self._set_connection_state("disconnected")
                got_reading = asyncio.Event()

                def _one_shot_callback(
                    _sender: Any, data: bytearray | bytes
                ) -> None:
                    self._notification_callback(_sender, data)
                    got_reading.set()

                client: BleakClient | None = None
                try:
                    ble_device = await self._wait_for_fresh_advertisement()
                    self._set_connection_state("polling")
                    self._first_notification_logged = False
                    _LOGGER.info(
                        "Attempting GATT connection to %s", self.mac_normalized
                    )
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        ble_device,
                        f"{DOMAIN}-{self.mac_normalized}",
                    )
                    _LOGGER.info(
                        "GATT connection established to %s", self.mac_normalized
                    )
                    self._client = client
                    await client.start_notify(
                        CHARACTERISTIC_UUID, _one_shot_callback
                    )
                    _LOGGER.info(
                        "Subscribed to notifications on %s", self.mac_normalized
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
                        _LOGGER.info(
                            "Disconnected from %s", self.mac_normalized
                        )
                    self._client = None
                    if self._connection_state != "failed_after_setup":
                        self._set_connection_state("disconnected")

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

    def _resolve_ble_device(self) -> BLEDevice | None:
        """Synchronous "right now" lookup against HA's bluetooth registry.

        Returns ``None`` if the device hasn't been seen by a connectable
        scanner within HA's freshness window. Callers that can afford to
        wait should prefer :meth:`_wait_for_fresh_advertisement`, which
        is advertisement-driven rather than timer-driven and so does not
        miss meters whose firmware advertises only every few minutes
        when idle.
        """
        return bluetooth.async_ble_device_from_address(
            self.hass, self._ble_address, connectable=True
        )

    async def _wait_for_fresh_advertisement(self) -> BLEDevice:
        """Return a connectable :class:`BLEDevice` once one is in registry.

        Two-phase resolution:

        1. **Fast path** — direct lookup via
           :func:`bluetooth.async_ble_device_from_address`. If a
           connectable scanner has seen the meter within HA's freshness
           window, return immediately. This is the common case in
           production (meter advertising continuously) and in tests
           (registry pre-populated with a fake BLEDevice).
        2. **Slow path** — register a bluetooth callback scoped to the
           Atorch service UUID (any scanner, passive-mode hint),
           filter delivered advertisements to this meter by a
           case-insensitive address compare inside the callback, and
           ``await`` an :class:`asyncio.Event` that the callback sets
           when a matching advertisement arrives. After the event fires,
           re-resolve via the direct (``connectable=True``) lookup —
           if the advert came in via a passive-only scanner the
           lookup returns ``None`` and the caller treats this as a
           connect failure, looping back into another wait; if it
           came in via a connectable scanner we proceed to connect.

        Raises :class:`BleakError` on timeout
        (``ADVERTISEMENT_WAIT_TIMEOUT_SECONDS``) so the outer reconnect
        backoff treats this as an ordinary connect failure.

        This replaces the original timer-driven retry loop that polled
        ``async_ble_device_from_address`` on a fixed cadence: meters
        that advertise every several minutes when idle would never
        intersect the ~90 s connectable-registry freshness window, so
        every retry returned ``None`` and the integration accumulated
        hundreds of failed attempts without ever connecting.
        """
        address = self.entry.data[CONF_ADDRESS]

        # Fast path. ``async_ble_device_from_address`` is NOT
        # case-insensitive: HA's bluetooth manager keys its internal
        # device-history dictionaries by UPPERCASE address and does a
        # plain ``dict.get()`` with no normalization, so a lowercase
        # address always misses. Pass the uppercase ``self._ble_address``.
        # Note the related asymmetry with the slow-path callback below:
        # the callback matcher CANNOT be keyed on ``address`` because
        # ``BluetoothCallbackMatcher`` compares addresses case-sensitively
        # and HA represents advertisement addresses uppercase — hence the
        # service-uuid matcher + in-callback case-insensitive address
        # filter approach.
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._ble_address, connectable=True
        )
        if ble_device is not None:
            return ble_device

        # Slow path: arm callback before we await so we cannot miss an
        # advertisement that lands between the failed fast-path lookup
        # and the registration.
        _LOGGER.info("Waiting for advertisement from %s", address)
        arrived = asyncio.Event()

        @callback
        def _on_advertisement(
            service_info: BluetoothServiceInfoBleak,
            _change: BluetoothChange,
        ) -> None:
            # The callback is registered against the Atorch service UUID,
            # so it fires for ANY Atorch meter in range. Filter to OUR
            # meter by a case-insensitive address compare — HA represents
            # advertisement addresses uppercase, our stored address is
            # lowercase.
            if service_info.address.lower() != address.lower():
                return
            # Log at INFO so the user can see we received an advert and
            # which scanner caught it. ``connectable`` here reflects the
            # scanner that delivered THIS advertisement; the
            # post-wait registry lookup still gates connect-eligibility.
            _LOGGER.info(
                "Advertisement received for %s "
                "(source=%s, rssi=%d, connectable=%s)",
                self.mac_normalized,
                service_info.source,
                service_info.rssi,
                service_info.connectable,
            )
            arrived.set()

        # Register the callback against the Atorch service UUID — the
        # same field the manifest's discovery matcher uses successfully —
        # rather than by address. ``BluetoothCallbackMatcher`` compares
        # addresses case-sensitively, and HA's bluetooth subsystem
        # represents advertisement addresses in UPPERCASE while our
        # stored address (from ``format_mac``) is lowercase, so an
        # address-keyed matcher NEVER fires. Confirmed in production: a
        # 900 s wait timed out while the meter was visibly present in
        # HA's Advertisements panel with a strong signal. We deliberately
        # do NOT constrain the matcher with ``connectable`` — we've been
        # burned by over-constraining the matcher before; the in-callback
        # address filter plus the post-wait connectable lookup are
        # sufficient.
        cancel = bluetooth.async_register_callback(
            self.hass,
            _on_advertisement,
            BluetoothCallbackMatcher(service_uuid=ATORCH_SERVICE_UUID),
            BluetoothScanningMode.PASSIVE,
        )
        try:
            try:
                await asyncio.wait_for(
                    arrived.wait(),
                    timeout=ADVERTISEMENT_WAIT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise BleakError(
                    f"No advertisement from {address} within "
                    f"{ADVERTISEMENT_WAIT_TIMEOUT_SECONDS}s; meter unreachable"
                ) from exc
        finally:
            cancel()

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._ble_address, connectable=True
        )
        if ble_device is None:
            # Advertisement was observed but the registry didn't catch
            # up — very unlikely race but worth surfacing as a connect
            # failure for the outer backoff.
            raise BleakError(
                f"Advertisement observed for {address} but BLEDevice "
                f"still missing from HA bluetooth registry"
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
        # Once-per-session "data flowing" INFO log. Throttled by a flag
        # the runner clears at the start of each connection attempt; the
        # per-frame decoded-reading log below stays at DEBUG because at
        # ~1Hz it would be too chatty for the default log viewer.
        if not self._first_notification_logged:
            self._first_notification_logged = True
            _LOGGER.info(
                "First notification received from %s, data flowing",
                self.mac_normalized,
            )
        try:
            readings = self._parser.feed(bytes(data))
        except UnsupportedPacketType as exc:
            self._increment_bucket(now, notifs=0, errors=1)
            # Schedule async repair-issue/title work on the loop.
            self.hass.async_create_task(
                self._handle_unsupported_packet_type(exc.packet_type)
            )
            return
        except InvalidPacket:  # pragma: no cover - defensive; parser swallows InvalidPacket internally today
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
            # Push fresh snapshot. The overridden
            # ``async_set_updated_data`` updates ``self.data``, fans the
            # snapshot through the base processor pipeline, AND notifies
            # the CoordinatorEntity listener registry so sensor entities
            # call ``async_write_ha_state`` and the HA UI reflects the
            # new value.
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
        self._set_connection_state("connected")
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
        self._set_connection_state("failed_after_setup")
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
        except Exception:  # noqa: BLE001 — defensive  # pragma: no cover - HA core async_update_entry doesn't raise in practice
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
        except Exception:  # noqa: BLE001  # pragma: no cover - manifest is always present in real environments
            self._issue_url = ""
        return self._issue_url
