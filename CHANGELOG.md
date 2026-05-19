# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `vMAJOR.MINOR.PATCH` tag; the
`.github/workflows/release.yml` workflow packages
`custom_components/atorch_ble/` into `atorch_ble.zip` and attaches it
to the GitHub Release.

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
