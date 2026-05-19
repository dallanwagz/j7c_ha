# Atorch BLE (J7-C)

Home Assistant custom integration for the **Atorch J7-C USB power meter** over Bluetooth Low Energy — surfacing live voltage, current, power, energy (Wh), capacity (mAh), USB D+/D- voltages, temperature, and runtime as native HA sensors, with no cloud and no manual MAC entry.

## Features

- BLE discovery; no manual MAC entry
- 10 sensors (8 default-enabled, 2 advanced USB D+/D-, plus a disabled-by-default `connection_state` diagnostic)
- Two connection modes via options flow: **persistent** (1 Hz, holds one proxy slot) and **polled** (configurable 10–3600 s interval)
- Works identically over a local Bluetooth adapter or an ESPHome Bluetooth proxy
- Repair issues surface unsupported packet types and cannot-connect-after-setup conditions
- Diagnostics download exposes `parser_error_rate_5m` for RF-quality triage
- Gold Quality Scale target

## Supported devices

| Model | Packet type | Status |
|-------|-------------|--------|
| J7-C  | 0x03        | Supported |
| UC96 (bare module) | 0x03 (same as J7-C) | Should work |
| DL24, UD18, AC meters | 0x01, 0x02 | Detected but not supported; will surface a repair issue |

## Setup

1. Power on the Atorch J7-C USB power meter and place it within range of either a local Bluetooth adapter on your Home Assistant host or an ESPHome Bluetooth proxy.
2. Home Assistant should auto-discover the device under **Settings → Devices & Services**. Click **Configure**, confirm the device, and submit.
3. Optionally tune the **Connection mode** (persistent / polled) and **Poll interval** via the entry's options (the gear icon on the integration entry).

Screenshots: see `docs/img/` in the repository.

## First install — what to expect

After confirming the device, sensors may show 'Unknown' for up to one poll interval (1s in persistent mode, 10–3600s in polled mode) until the first measurement arrives. If they remain Unknown for several minutes, see Troubleshooting.

Sensors will show **Unknown** or **Unavailable** until the first measurement arrives — typically <5 seconds in persistent mode, up to your configured poll interval in polled mode.

## Known limitations

- Atorch J7-C only in v1 (packet type 0x03). DL24/UD18/AC meters are detected but unsupported and will raise a repair issue.
- Energy (Wh) and Capacity (mAh) are device-side cumulative counters and **cannot be reset over BLE** — reset requires the physical button on the meter.
- Persistent mode holds one ESPHome-proxy connection slot per meter, continuously.
- v1 cannot migrate Energy Dashboard history across a MAC change (replacing the meter).

## Troubleshooting

- **Device not discovered**: verify the meter is powered, within BLE range, and that Home Assistant's Bluetooth integration is set up. Toggle the meter's screen to confirm it is awake.
- **Sensors Unknown for longer than expected**: see the README's *First install — what to expect* and *Troubleshooting* sections.
- **High parser-error count**: open *Download diagnostics* on the device page and look at `parser_error_rate_5m` (rolling 5-minute ratio in `[0, 1]`). Sustained values above 0.05 (5%) indicate RF interference; move the meter closer to the adapter/proxy. See the README for full guidance.
- **Unsupported device variant**: a repair issue under **Settings → System → Repairs** identifies the offending packet-type byte (0x01 / 0x02). Dismissing it is persistent per config entry.

See the [full README](README.md) for installation, configuration, sensor table, MAC-change procedure, and roadmap.
