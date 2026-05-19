"""Shared fixtures for atorch_ble tests.

Stands up a Home Assistant test harness via
``pytest-homeassistant-custom-component`` and exports the building blocks
the config-flow and options-flow suites need:

* ``enable_custom_integrations`` (autouse) — wires the
  ``custom_components/`` package at the repo root into HA's loader so
  ``DOMAIN = "atorch_ble"`` is discoverable.
* ``mock_bluetooth`` (autouse) — re-exports phacc's bluetooth-manager
  mocks so flow tests can run without a real adapter.
* ``build_j7c_frame`` — constructs canonical 36-byte J7-C USB-meter
  payloads with the documented checksum. Mirrors the parser library's
  fixture builder; kept in this repo rather than depending on parser
  test fixtures so the integration test suite stays self-contained.
* ``discovery_info`` — a ready-to-inject ``BluetoothServiceInfoBleak``
  for the canonical UC96_BLE device used across discovery-step tests.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

# Re-export the autouse fixtures from phacc so simply importing this
# conftest is enough; the names are also picked up by pytest fixture
# resolution from the upstream plugin, but referencing them here makes
# the dependency explicit for future readers.

TEST_MAC = "AA:BB:CC:DD:EE:FF"
TEST_MAC_NORMALIZED = "aa:bb:cc:dd:ee:ff"
TEST_LAST4_UPPER = "EEFF"
TEST_TITLE = f"Atorch J7-C ({TEST_LAST4_UPPER})"
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"

# J7-C USB-meter (packet type 0x03) frame layout constants.
_FRAME_LEN = 36
_MAGIC = b"\xff\x55"
_DIR_FROM_DEVICE = 0x01
_TYPE_USB_METER = 0x03


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Force phacc's custom-integrations loader on for every test."""
    return None


@pytest.fixture(autouse=True)
def auto_mock_bluetooth(mock_bluetooth: None) -> None:
    """Mock the bluetooth manager for every test so flows can run headless."""
    return None


@pytest.fixture
def build_j7c_frame() -> Callable[..., bytes]:
    """Return a builder for canonical 36-byte J7-C USB-meter payloads.

    The builder writes the documented field layout (magic, direction,
    packet type, big-endian voltage / current / capacity / energy /
    duration / temperature, configurable price + currency byte, and the
    documented ``(sum(payload[2:33]) & 0xFF) ^ 0x44`` checksum at
    offset 0x21). The remaining bytes are zero-filled.
    """

    def _build(
        *,
        voltage_v: float = 5.0,
        current_a: float = 1.0,
        capacity_mah: int = 0,
        energy_wh: float = 0.0,
        price_per_kwh: float = 0.0,
        currency: int = 0,
        duration_s: int = 0,
        temperature_c: int = 25,
    ) -> bytes:
        frame = bytearray(_FRAME_LEN)
        frame[0:2] = _MAGIC
        frame[2] = _DIR_FROM_DEVICE
        frame[3] = _TYPE_USB_METER
        frame[0x04:0x07] = int(round(voltage_v * 100)).to_bytes(3, "big")
        frame[0x07:0x0A] = int(round(current_a * 100)).to_bytes(3, "big")
        frame[0x0A:0x0D] = int(capacity_mah).to_bytes(3, "big")
        frame[0x0D:0x11] = int(round(energy_wh * 100)).to_bytes(4, "big")
        frame[0x11:0x15] = int(round(price_per_kwh * 100)).to_bytes(4, "big")
        frame[0x15] = currency & 0xFF
        frame[0x16:0x1A] = int(duration_s).to_bytes(4, "big")
        frame[0x1A:0x1C] = int(temperature_c).to_bytes(2, "big")
        # Checksum lives at 0x21; preceding bytes 0x1C..0x20 are zero.
        checksum = (sum(frame[2:33]) & 0xFF) ^ 0x44
        frame[0x21] = checksum
        return bytes(frame)

    return _build


def _make_service_info(
    *,
    name: str,
    address: str,
    service_uuids: list[str] | None = None,
    rssi: int = -60,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> BluetoothServiceInfoBleak:
    """Construct a minimally-valid ``BluetoothServiceInfoBleak``."""
    # bleak ≥0.22 dropped the deprecated kwargs (rssi, details, ...); pass
    # only the required positional pair so we don't trip
    # DeprecationWarning under newer bleak releases.
    device = BLEDevice(address, name, details={})
    # AdvertisementData is optional in the dataclass; pass a Mock that
    # carries the bare attributes a hypothetical downstream consumer
    # might read. None is also acceptable but a few HA paths assume the
    # attribute exists.
    advertisement = MagicMock()
    advertisement.local_name = name
    advertisement.service_uuids = service_uuids or [SERVICE_UUID]
    advertisement.manufacturer_data = manufacturer_data or {}
    advertisement.service_data = service_data or {}
    advertisement.rssi = rssi
    advertisement.tx_power = None
    advertisement.platform_data = ()

    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data=manufacturer_data or {},
        service_data=service_data or {},
        service_uuids=service_uuids or [SERVICE_UUID],
        source="local",
        device=device,
        advertisement=advertisement,
        connectable=True,
        time=0,
        tx_power=None,
        raw=None,
    )


@pytest.fixture
def make_service_info() -> Callable[..., BluetoothServiceInfoBleak]:
    """Expose the service-info factory so individual tests can vary fields."""
    return _make_service_info


@pytest.fixture
def discovery_info() -> BluetoothServiceInfoBleak:
    """Return a canonical J7-C ``UC96_BLE`` discovery payload."""
    return _make_service_info(name="UC96_BLE", address=TEST_MAC)


@pytest.fixture
def stray_discovery_info() -> BluetoothServiceInfoBleak:
    """Return a discovery payload with a non-UC96_BLE local name."""
    return _make_service_info(name="Some Other Device", address=TEST_MAC)
