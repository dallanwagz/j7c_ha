# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `vMAJOR.MINOR.PATCH` tag; the
`.github/workflows/release.yml` workflow packages
`custom_components/atorch_ble/` into `atorch_ble.zip` and attaches it
to the GitHub Release.

## [0.2.2] - 2026-06-15

### Changed
- Bumped the `atorch-ble` pin (`0.1.1` → `0.1.2`) in `pyproject.toml` and
  `manifest.json`. `atorch-ble` 0.1.2 fixes USB-meter duration decoding:
  bytes `0x17`-`0x18` are a 16-bit big-endian hours counter, not a days
  byte plus an hours byte (see that project's CHANGELOG). Before this
  bump the `runtime` sensor under-reported for any meter running ≥ 256 h
  continuous. Confirmed against the official Atorch "E_Test" app.

### Added
- Persistent-mode data-flow watchdog. The runner's heartbeat previously
  only checked `client.is_connected`, so a link that stayed up while the
  meter silently stopped streaming (notify subscription dropped or
  firmware wedged) would report `connected` with stale readings forever.
  The heartbeat now also forces a reconnect when no raw notification has
  arrived for `PERSISTENT_DATA_TIMEOUT_SECONDS` (60 s, comfortably above
  the worst-case ~15 s frame-assembly time). Decision logic lives in the
  unit-tested `_data_is_stale` helper.

## [0.2.1] - 2026-06-14

### Changed
- Aligned the `atorch-ble` pin in `pyproject.toml` (`0.1.0` → `0.1.1`) with the version already required by `custom_components/atorch_ble/manifest.json`, which is the requirement Home Assistant actually installs. No runtime behavior change for installed integrations — the component already ran against `atorch-ble==0.1.1`; this only fixes the stale development/packaging pin so a fresh `pip install -e .` resolves the same version HA uses.

### Fixed
- Removed an unused `contextlib` import in `tests/test_coordinator.py` flagged by `ruff`.

## [0.2.0] - 2026-05-20

### Changed
- `async_setup_entry` now verifies the meter is reachable before completing setup (HA Core `test-before-setup` quality-scale rule). It resolves a connectable `BLEDevice` via `bluetooth.async_ble_device_from_address` (uppercased address) and raises `ConfigEntryNotReady` when the meter is not currently advertising. Home Assistant then retries setup automatically when the meter next advertises, instead of setting up immediately with unavailable entities. The `test-before-setup` rule in `quality_scale.yaml` moves from `exempt` to `done` — the previous "passive BLE" exemption rationale was inaccurate, since the integration actively opens GATT connections.

### Removed
- Data-rate instrumentation. The 30-second INFO heartbeat that logged raw-notification and decoded-frame counts while connected (added in v0.1.6 to diagnose the now-fixed v0.1.7 checksum bug) has been removed entirely, along with the `_raw_notification_count` / `_decoded_frame_count` counters and the `DATA_RATE_SUMMARY_INTERVAL_SECONDS` constant. A periodic INFO heartbeat is not acceptable for HA Core. Observability is unaffected: `last_seen`, `connection_state`, and the `parser_error_rate_5m` rolling-window metric remain in diagnostics.

## [0.1.9] - 2026-05-20

### Fixed
- Connection-handle race during connection-mode switches. The shared `self._client` handle is used by both the persistent runner and the polled `poll_method`, and `poll_method`'s cleanup cleared it unconditionally. When a polled→persistent mode switch started the persistent runner while a poll was still finishing, the finishing poll could null out the persistent runner's live connection handle — orphaning a real GATT connection that holds the meter's single connection slot, after which every reconnect failed and the integration wedged in "reconnecting" with no data. Client-handle cleanup is now identity-guarded (a path releases the handle only if it still owns it), the persistent runner holds its connection in a local rather than re-reading the shared handle, and `_async_options_updated` is serialized with a lock so rapid successive mode switches cannot interleave their stop/start sequences.
- Persistent mode no longer briefly reports the polled-only `disconnected` connection state on a connection drop. `_run_persistent` set `disconnected` — whose display text reads "polled mode — between polls" — for an instant before transitioning to `reconnecting`, which was misleading in the activity log. A persistent-mode drop now goes straight to `reconnecting`; `disconnected` is now a polled-mode-only state.

## [0.1.8] - 2026-05-19

### Changed
- Migrated the coordinator from `ActiveBluetoothProcessorCoordinator` to `ActiveBluetoothDataUpdateCoordinator` (the bluetooth "coordinator" branch instead of the "processor" branch). This is an internal architecture change with no user-facing behavior difference. `ActiveBluetoothDataUpdateCoordinator` natively provides the `DataUpdateCoordinator`-style subscription surface (`async_add_listener` / `async_update_listeners` / `async_contexts` / `self.data`), so the integration no longer carries a hand-rolled, non-idiomatic listener shim that re-implemented those methods on top of the processor base. Sensor entities now subscribe through HA's native `PassiveBluetoothCoordinatorEntity`. This brings the integration closer to HA Core idioms and HA-Core-readiness.
- Polled-mode cadence is now advertisement-gated. The framework's poll debouncer drives a poll on the next BLE advertisement once `poll_interval_seconds` has elapsed since the last poll, so the effective cadence is "approximately every `poll_interval_seconds`, on the next advertisement after the interval elapses". A meter advertising infrequently while idle may therefore poll slightly slower than its configured interval. This also corrects a latent bug in the old `needs_poll` check, which treated the framework's seconds-since-last-poll value as an absolute timestamp.

## [0.1.7] - 2026-05-19

### Fixed
- The integration decoded almost no data from a live meter even over a rock-solid BLE connection. The root cause was in the `atorch-ble` parser: USB-meter checksum validation summed the wrong byte range (`payload[2:33]`) and compared it against the wrong frame offset (`payload[0x21]`, a constant data byte), so real J7-C frames failed the checksum gate roughly 99.7% of the time and were silently discarded. A 35-minute production capture showed a steady 30 raw BLE notifications per 30 s but ~0 decoded frames, decoding only on a rare chance collision. The real checksum — `(sum(payload[0x03:0x23]) & 0xFF) ^ 0x44` in the final byte — was reverse-engineered and confirmed against 639 frames captured from a live meter. With `atorch-ble==0.1.1` every well-formed frame decodes. This was the true cause behind the symptoms the v0.1.1–v0.1.6 hotfixes chipped away at.

### Changed
- Bumped the `atorch-ble` requirement from `0.1.0` to `0.1.1`.

## [0.1.6] - 2026-05-19

### Fixed
- Polled mode now waits for an actual decoded reading before disconnecting. The polled runner previously called `got_reading.set()` on the **first raw BLE notification**, but a raw notification is typically just a fragment of the 36-byte Atorch frame — the parser reassembles a complete frame from several notifications. The runner connected for only a few seconds, grabbed one fragment, and disconnected before the parser could yield a `UsbMeterReading`, so polled mode decoded zero readings in production. The runner now subscribes via `_notification_callback` and waits on a `_decoded_reading_event` that is set only after a complete frame is decoded and published.

### Changed
- `POLLED_NOTIFICATION_TIMEOUT_SECONDS` raised from 5 s to 25 s. Production logs show a complete frame can take 15+ seconds to assemble on this hardware, so the polled runner must wait that long for a real decoded reading. A timeout with no decoded reading is still treated as a failure for backoff/repair tracking.

### Added
- Data-rate instrumentation. The coordinator now counts raw BLE notifications (`_raw_notification_count`) and decoded frames (`_decoded_frame_count`) and logs a 30-second INFO summary while a connection is held in either mode: `Data rate (mac=...): N raw notifications, M decoded frames in last 30s`. If a full window passes with no data while connected, the line says so explicitly (`NO data received in last 30s while connected — meter may need a start command`). This will reveal whether the meter streams continuously or sends only a token frame after subscription — the open question behind sporadic sensor updates.
- `DATA_RATE_SUMMARY_INTERVAL_SECONDS` constant in `const.py`.

## [0.1.5] - 2026-05-19

### Fixed
- `bluetooth.async_ble_device_from_address` lookups now pass an UPPERCASE address. HA's bluetooth manager keys its internal device-history dictionaries by uppercase address and does a plain `dict.get()` with no case normalization, so the coordinator's lowercase `format_mac()` address always missed and the lookup returned `None` — even immediately after a connectable advertisement was received, producing `BLE connection failed: Advertisement observed ... but BLEDevice still missing from HA bluetooth registry`. This was the second hiding spot of the same address-casing bug fixed in v0.1.4 for the callback matcher. The coordinator now keeps an uppercase `self._ble_address` for all HA bluetooth-API lookups, while the lowercase `mac_normalized` stays for device-registry identifiers and entity unique_ids (which must not change). The inaccurate "case-insensitive" comments claiming `async_ble_device_from_address` normalizes case have been corrected.

## [0.1.4] - 2026-05-19

### Fixed
- Advertisement-wait callback now actually fires. The coordinator registered its slow-path advertisement callback with `BluetoothCallbackMatcher(address=...)` using a lowercase address (from `format_mac`). HA's bluetooth subsystem represents advertisement addresses in UPPERCASE and `BluetoothCallbackMatcher` compares them case-sensitively, so the callback never fired — confirmed in production where a 900 s advertisement wait timed out while the meter was visibly present in HA's Advertisements panel with a strong signal. The callback is now registered against the Atorch service UUID (the same field the manifest's discovery matcher uses successfully) and filters to the target meter with a case-insensitive address comparison inside the callback, sidestepping address-case fragility entirely. This was the root cause behind every "No advertisement within Ns; meter unreachable" timeout since v0.1.1 — the advertisement-driven coordinator could never actually hear an advertisement.

### Added
- `ATORCH_SERVICE_UUID` constant in `const.py` (`0000ffe0-0000-1000-8000-00805f9b34fb`, lowercase to match HA's internal UUID representation).

## [0.1.3] - 2026-05-19

### Fixed
- Bluetooth advertisement-wait callback now actually fires on production HA installations. The previous matcher (`address` + `connectable=True` + `BluetoothScanningMode.ACTIVE`) silently failed to fire for advertisements that HA's bluetooth subsystem clearly received via the same matchers used by the config_flow's discovery callback — confirmed in production with the J7-C against a mix of passive-only and active ESPHome BT proxies, where 600 s waits timed out while multiple in-window advertisements arrived. Matcher relaxed to `address`-only with `BluetoothScanningMode.PASSIVE` (a scanner-mode hint, not a hard filter), and the connectable-eligibility check now happens AFTER the wait via the existing `async_ble_device_from_address(..., connectable=True)` lookup — the same check the consumer would do anyway. Advertisements seen only by passive-only scanners produce a `None` lookup and loop back into another wait, exactly like a connect failure.

### Changed
- `ADVERTISEMENT_WAIT_TIMEOUT_SECONDS` raised from 600 s to 900 s (15 minutes) so the wait comfortably catches the next advertisement cycle on slow-advertising firmware variants. The user's J7-C was observed advertising roughly every 10 minutes when idle, which was just on the edge of the previous 600 s budget.
- "Advertisement received" INFO log now includes `source`, `rssi`, and `connectable` fields so users debugging connection issues can see which scanner caught the advert and whether it was a connectable-eligible observation.

## [0.1.2] - 2026-05-19

### Fixed
- `connection_state` diagnostic sensor now refreshes live on every state transition. Previously the value was assigned via direct attribute write that bypassed the CoordinatorEntity listener registry, so the sensor cached its first read forever — making real-time troubleshooting impossible. Every state transition now flows through a `_set_connection_state` setter that notifies listeners on change (and is idempotent on no-op transitions to avoid spurious updates).
- Polled mode no longer reports `connection_state == "polling"` while it is actually waiting for a BLE advertisement (a wait that can take up to 600s on slow-advertising firmware). The state now transitions through `disconnected` → (advertisement arrives) → `polling` → (connection established) → `connected` → `disconnected` between polls, so the UI accurately reflects what the runner is doing.

### Changed
- Critical connection-lifecycle log lines promoted from DEBUG to INFO so they are visible in HA's default WARN-or-above log viewer without enabling debug logging: advertisement-wait start, advertisement received, GATT-connect attempt + success, notify-subscribe success, first notification per session (throttled), and disconnect. Per-frame parse logs and intra-cycle chatter remain at DEBUG.

## [0.1.1] - 2026-05-19

### Fixed
- Coordinator now waits for a fresh BLE advertisement before attempting connection, instead of retrying on a timer. Fixes the case where slow-advertising J7-C firmware variants (advert interval > 60s when idle) would never connect because every timer-driven retry attempt fell outside HA's connectable-registry freshness window.
- Outer reconnect backoff cap shortened from 60s to 30s. Since advertisement-wait now handles the "device unreachable" case implicitly, the outer backoff only needs to handle transient GATT-layer failures.

## [0.1.0] - 2026-05-19

Initial release.

### Added

- Home Assistant custom integration for the Atorch J7-C BLE USB power meter
- Bluetooth discovery-based config flow with confirm step (no manual MAC entry)
- Per-entry options flow: persistent vs polled connection modes with configurable poll interval (10–3600 s); switching applies live
- 10 sensors:
  - 7 default-enabled measurement sensors: voltage, current, power, energy (Wh), capacity (mAh), temperature, runtime
  - 2 default-disabled advanced sensors: USB D+ voltage, USB D- voltage
  - 1 default-disabled diagnostic: `connection_state` ENUM
- Active-Bluetooth coordinator with a 5-state lifecycle (`connected`, `polling`, `disconnected`, `reconnecting`, `failed_after_setup`), `parser_error_rate_5m` rolling-window diagnostic, and persistent dismissal of unsupported-packet-type repair issues
- Two repair-issue surfaces:
  - `cannot_connect_after_setup` (raised at 5 consecutive failures, re-raised after 50 failures if previously dismissed)
  - `unsupported_packet_type` (raised once per offending packet-type byte; dismissal is persistent via entry data)
- Diagnostics download exposing `parser_error_count` and `parser_error_rate_5m`
- Test suite: 58 tests, 98%+ line coverage on `custom_components/atorch_ble/`
- Quality Scale: **Gold** tier declared in `quality_scale.yaml`
- Translations: full English `strings.json` + `translations/en.json`
- HACS distribution metadata (`hacs.json`, `info.md`) and end-user `README.md`
- GitHub Actions workflows: `validate.yml` (HACS + hassfest + pytest) and `release.yml` (tag-triggered ZIP release)

### Dependencies

- `atorch-ble==0.1.0` (parser library, PyPI)
- Home Assistant `>=2026.5.0`
