# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `vMAJOR.MINOR.PATCH` tag; the
`.github/workflows/release.yml` workflow packages
`custom_components/atorch_ble/` into `atorch_ble.zip` and attaches it
to the GitHub Release.

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
