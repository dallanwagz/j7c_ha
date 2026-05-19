# Atorch BLE (J7-C) — Home Assistant custom integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Quality Scale: Gold](https://img.shields.io/badge/quality--scale-gold-yellow.svg)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
[![Tests](https://github.com/dallanwagz/j7c_ha/actions/workflows/validate.yml/badge.svg)](https://github.com/dallanwagz/j7c_ha/actions/workflows/validate.yml)

This integration turns an **Atorch J7-C USB power meter** into a first-class Home Assistant device over Bluetooth Low Energy — no cloud, no manual MAC entry. Voltage, current, power, energy (Wh), capacity (mAh), USB D+/D- voltages, temperature, and runtime are exposed as native HA sensors. The integration treats local Bluetooth adapters and ESPHome Bluetooth proxies identically and works with both.

## Supported devices

| Model | Packet type | Status |
|-------|-------------|--------|
| J7-C  | 0x03        | Supported |
| UC96 (bare module) | 0x03 (same as J7-C) | Should work |
| DL24, UD18, AC meters | 0x01, 0x02 | Detected but not supported; will surface a repair issue |

## ESPHome Bluetooth proxy basics

ESPHome [Bluetooth proxies](https://esphome.io/components/bluetooth_proxy.html) are inexpensive ESP32 nodes that forward BLE advertisements and active connections to Home Assistant, extending Bluetooth coverage beyond the host's radio. **Proxies are optional** — this integration works equally well with a local Bluetooth adapter on the HA host. From the integration's perspective the two are identical: discovery, connection, and notification handling go through the same Home Assistant `bluetooth` stack regardless of which transport carries the packets.

## Hardware setup

1. **Power the Atorch J7-C USB power meter.** Plug it into any USB-A or USB-C source (the meter is USB-bus-powered) and confirm the display is on.
2. **Place the meter within BLE range** of either:
   - A **local Bluetooth adapter** on the Home Assistant host (built-in radio or a USB BT dongle recognized by HA's Bluetooth integration), or
   - An **ESPHome Bluetooth proxy** that already appears under **Settings → Devices & Services → ESPHome**.
3. **Wake the meter** by tapping its physical button if the screen is asleep — some firmware variants only advertise while the display is active.
4. **No pairing PIN is required.** The J7-C advertises as `UC96_BLE` and does not require bonding.

## Installation (via HACS)

Until this integration is accepted into the HACS default integrations list, install it as a **custom repository**:

1. In Home Assistant, open **HACS → Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add `https://github.com/dallanwagz/j7c_ha` with category **Integration**.
4. Find **Atorch BLE (J7-C)** in the HACS integrations list and click **Download**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services**. The Atorch J7-C USB power meter should appear as a discovered device — click **Configure** and confirm.

## Configuration

The integration uses Bluetooth discovery — **no manual MAC entry**. When the meter is in range it is auto-discovered and presented in **Settings → Devices & Services** with a confirm step. Submitting the form creates one config entry per meter.

### Options flow

Each config entry has its own options (gear icon on the entry):

- **`connection_mode`**: `persistent` or `polled` (enum).
- **`poll_interval_seconds`**: integer 10–3600. Only applies in polled mode.

Switching `connection_mode` applies live — no restart needed.

### Choosing a connection mode

| Mode | Latency | Slot usage | When to choose |
|------|---------|------------|----------------|
| **Persistent** | ~1 Hz updates | Holds **one ESPHome-proxy connection slot continuously per meter** | Slot budget allows; you want fast updates |
| **Polled** | One frame every `poll_interval_seconds` (10–3600 s) | Briefly occupies a slot per poll, otherwise idle | Proxy slots are scarce; multiple meters share one proxy |

**Slot-arithmetic rule of thumb:** A typical ESP32 ESPHome BT proxy supports about **3 simultaneous BLE connections**. If you have more meters than the proxy has slots minus one (reserve at least one slot for other BLE devices), choose **Polled** mode for at least the extras. In formula terms, recommend polled when `N_meters > slots - 1`.

**Mode change confirmation:** Switching `connection_mode` via the options flow applies live; sensors may briefly show `Unknown` during the transition while the new mode's first frame is acquired. This is expected behavior, not a fault.

## Sensors exposed

The integration exposes 10 entities per meter. "Default enabled" entities are visible immediately; "Default disabled" entities can be enabled per device from the entity registry.

| Entity | Device class | State class | Unit | Default enabled | Notes |
|--------|--------------|-------------|------|-----------------|-------|
| Voltage | `voltage` | `measurement` | V | Yes | Bus voltage at the USB port |
| Current | `current` | `measurement` | A | Yes | Bus current |
| Power | `power` | `measurement` | W | Yes | Computed (V × A) by the meter |
| Energy | `energy` | `total_increasing` | Wh | Yes | **Device-side cumulative counter. Cannot be reset over BLE** — reset requires the meter's physical button. Not eligible for the Energy Dashboard because non-monotonic counter resets cannot be guaranteed. |
| Capacity | `None` (HA Core's `UnitOfElectricCharge` lacks mAh) | `total_increasing` | mAh | Yes | **Device-side cumulative counter. Cannot be reset over BLE** — reset requires the meter's physical button. |
| Temperature | `temperature` | `measurement` | °C | Yes | Internal sensor on the meter |
| Runtime duration | `duration` | `total_increasing` | s | Yes | Seconds since last reset on the device |
| D+ voltage | `voltage` | `measurement` | V | No (advanced) | USB data-plus line voltage |
| D- voltage | `voltage` | `measurement` | V | No (advanced) | USB data-minus line voltage |
| Connection state | `enum` | — | — | No (diagnostic) | One of `connected`, `polling`, `disconnected`, `reconnecting`, `failed_after_setup`. Useful for automations that react to integration health. |

## First install — what to expect

After confirming the device, sensors may show 'Unknown' for up to one poll interval (1s in persistent mode, 10–3600s in polled mode) until the first measurement arrives. If they remain Unknown for several minutes, see Troubleshooting.

Sensors will show **Unknown** or **Unavailable** until the first measurement arrives — typically <5 seconds in persistent mode, up to your configured poll interval in polled mode.

If sensors remain Unknown for more than a few seconds in persistent mode, or longer than 2× your poll interval in polled mode, see Troubleshooting. Brief Unknown flickers (a single missed frame in persistent mode) are normal and self-recover within 1-2 seconds.

If you want a real-time indicator of the integration's connection lifecycle, enable the disabled-by-default 'Connection state' diagnostic sensor on the device page.

## Replacing or moving a meter (MAC changed)

> **WARNING: v1 cannot migrate Energy Dashboard history across a MAC change.** If you swap the Atorch J7-C USB power meter for a different physical unit, or otherwise cause its Bluetooth MAC address to change, the new device will be a new config entry with new entity IDs. Long-term statistics — including Energy Dashboard history — are keyed to entity IDs and will not automatically follow the new MAC.

The procedure in v1:

1. **Delete the old config entry** under **Settings → Devices & Services**.
2. **Add the new device via discovery** when it appears (no manual MAC entry).
3. *(Optional, see warning below)* Use Home Assistant's **entity-rename UI** to graft the new entity IDs onto the old entity IDs to preserve Energy Dashboard continuity and history.

**Entity-rename grafting is the only mitigation available in v1, and incorrect grafting silently corrupts long-term statistics** — e.g. grafting a fresh `total_increasing` counter (starting at 0) onto an existing series whose last value was 12 345 Wh produces a non-monotonic step that HA's statistics engine treats as a counter reset, double-counting the next delta into the Energy Dashboard. Only graft if you understand the implication.

A "rebind to new MAC" reconfigure flow that preserves the existing config entry (and therefore the entity IDs and statistics) is a v0.2 candidate — see [Roadmap](#roadmap).

## Troubleshooting

### Device not discovered

- Verify the meter is **powered** (display on; tap the button to wake it).
- Verify it is **within BLE range** of either a local adapter or an ESPHome proxy.
- Verify the **HA Bluetooth integration is configured** (Settings → Devices & Services → Bluetooth) and shows an active adapter or proxy.
- Discovery requires the meter's local name to be `UC96_BLE` and the GATT service `0000ffe0-0000-1000-8000-00805f9b34fb` to be advertised.

### Sensors Unknown for longer than expected

See the *First install — what to expect* section above for normal-acquisition windows. If sensors stay Unknown beyond those windows, check the **Connection state** diagnostic sensor (enable it if needed) and download diagnostics (below).

### Unsupported device variant (repair issue)

If the integration receives packet types `0x01` or `0x02`, it raises a **repair issue** under **Settings → System → Repairs** identifying the packet-type byte and recommending you file an upstream issue. Dismissing the repair issue is persistent for that config entry's offending packet-type byte (it will not re-surface for the same byte). v1 supports packet type `0x03` only.

### Capturing diagnostic logs

Download a full diagnostic snapshot via:

**Settings → Devices & Services → Atorch BLE → device → "Download diagnostics"**

To turn up debug logging temporarily, add this to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.atorch_ble: debug
    atorch_ble: debug
```

Then restart Home Assistant. Remove the entries when finished — debug logging is verbose.

### High parser error count

The diagnostics download exposes two parser-quality metrics:

- **`parser_error_count`** — cumulative count of `InvalidPacket` exceptions since HA startup.
- **`parser_error_rate_5m`** — rolling 5-minute ratio in `[0, 1]` of `InvalidPacket` exceptions to received notifications.

**Look at the rate, not the absolute count.** Cumulative counts grow unbounded on long-running installs and are not a useful health signal on their own. The rolling rate is the actionable metric.

Threshold heuristic: **`parser_error_rate_5m > 0.05`** (more than 5% of received notifications in the last 5 minutes are unparseable) indicates a problem, almost always **RF interference** between the meter and the BLE adapter/proxy.

Recommended actions:

1. **Move the meter physically closer** to the adapter or proxy, or reduce obstructions (metal cases, microwaves, dense walls).
2. If the device is a **confirmed J7-C** and the rate stays elevated after relocation, **file an issue** at the [issue tracker](https://github.com/dallanwagz/j7c_ha/issues) and attach the downloaded diagnostics file.

### Mode change confirmation

Switching `connection_mode` between persistent and polled via the options flow applies live; sensors may briefly show `Unknown` during the transition while the new mode's first frame is acquired. This is expected and self-recovers within one poll interval.

## Known limitations

- **Atorch J7-C only in v1** (packet type `0x03`). DL24, UD18, and AC meters are detected but unsupported.
- **No counter reset over BLE.** Energy (Wh) and Capacity (mAh) reset requires the meter's physical button.
- **Packet type `0x03` only.** Packet types `0x01` and `0x02` raise a repair issue.
- **Persistent mode holds one ESPHome-proxy connection slot continuously per meter.**

## Removing the integration

To cleanly remove the integration:

1. Open **Settings → Devices & Services → Atorch BLE**.
2. For each Atorch J7-C device, click the device, then **Delete**. This removes the config entry and its entities.
3. Open **HACS → Integrations → Atorch BLE (J7-C)** and choose **Remove** to uninstall the integration code.
4. Restart Home Assistant.

**Note on the `atorch-ble` Python package:** Removing the integration via HACS removes the integration code; the `atorch-ble` Python package will remain installed in your Home Assistant environment. It is harmless and will be removed on the next HA container rebuild or environment refresh.

## Roadmap

The v0.2 backlog:

- RSSI as a disabled-by-default `EntityCategory.DIAGNOSTIC` sensor
- Reconfigure-flow step that rebinds an existing config entry to a new MAC address (Energy Dashboard history preservation across hardware swap)
- DL24, UD18, AC-meter packet-type support
- Auto-create a low-severity repair issue when parser-error-rate exceeds a threshold (currently surfaced only via diagnostics download)

## Protocol references

Documentation of the Atorch BLE protocol that informed this work:

- adlerweb's reverse-engineering write-up: <https://www.adlerweb.info/blog/2020/04/19/elektronische-last-atorch-modbus/>
- NiceLabs `atorch-console`: <https://github.com/NiceLabs/atorch-console>
- syssi `esphome-atorch-dl24`: <https://github.com/syssi/esphome-atorch-dl24>

## License

[MIT](LICENSE)
