"""Tests for the atorch_ble sensor platform.

Covers all ten sensors (five primary + temperature + runtime + two USB
data-line voltages + diagnostic connection_state), default-enabled vs.
default-disabled flags, value extraction from a synthesized
``AtorchBleData`` snapshot, and the device-class / state-class /
entity-category attribute matrix.
"""

from __future__ import annotations

from unittest.mock import patch

from atorch_ble import UsbMeterReading
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import DOMAIN
from custom_components.atorch_ble.coordinator import AtorchBleData

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


# Map sensor description key -> (device_class, state_class, entity_category, enabled_by_default)
_EXPECTED_KEYS = {
    "voltage",
    "current",
    "power",
    "energy",
    "capacity",
    "voltage_dplus",
    "voltage_dminus",
    "temperature",
    "runtime",
    "connection_state",
}

_DEFAULT_ENABLED = {
    "voltage",
    "current",
    "power",
    "energy",
    "capacity",
    "temperature",
    "runtime",
}

_DEFAULT_DISABLED = {"voltage_dplus", "voltage_dminus", "connection_state"}

_TOTAL_INCREASING = {"energy", "capacity", "runtime"}


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED},
        title=TEST_TITLE,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = _make_entry(hass)
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    return entry


def _unique_id_for(key: str) -> str:
    mac_stripped = TEST_MAC_NORMALIZED.replace(":", "")
    return f"{mac_stripped}_{key}"


async def test_all_sensors_present(hass: HomeAssistant) -> None:
    """All 10 sensor entities are registered after setup."""
    entry = await _setup(hass)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    keys_seen = {e.unique_id.split("_", 1)[1] for e in entries}
    assert keys_seen == _EXPECTED_KEYS


async def test_default_enabled_sensors(hass: HomeAssistant) -> None:
    """Default-enabled vs. default-disabled flag matrix is correct."""
    await _setup(hass)
    registry = er.async_get(hass)

    for key in _DEFAULT_ENABLED:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, _unique_id_for(key)
        )
        assert entity_id is not None, f"missing entity for {key}"
        entry_obj = registry.async_get(entity_id)
        assert entry_obj.disabled_by is None, f"{key} should be enabled"

    for key in _DEFAULT_DISABLED:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, _unique_id_for(key)
        )
        assert entity_id is not None, f"missing entity for {key}"
        entry_obj = registry.async_get(entity_id)
        assert entry_obj.disabled_by is not None, f"{key} should be disabled"


async def test_sensor_values_from_reading(
    hass: HomeAssistant,
) -> None:
    """Each sensor's value_fn extracts the expected field from AtorchBleData.

    Exercises the entity-description value-source contract directly
    against an ``AtorchBleData`` snapshot, sidestepping HA's
    state-machine plumbing — the per-description ``value_fn`` /
    ``value_fn_coordinator`` callables are the load-bearing surface for
    "what value does the sensor report?".
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    reading = UsbMeterReading(
        voltage_v=5.123,
        current_a=1.500,
        capacity_mah=200,
        energy_wh=12.50,
        voltage_dplus_v=2.1,
        voltage_dminus_v=2.2,
        temperature_c=30,
        duration_s=120,
    )
    snapshot = AtorchBleData(
        reading=reading,
        power_w=reading.voltage_v * reading.current_a,
    )

    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    assert by_key["voltage"].value_fn(snapshot) == 5.123
    assert by_key["current"].value_fn(snapshot) == 1.5
    assert abs(by_key["power"].value_fn(snapshot) - 5.123 * 1.5) < 1e-6
    assert by_key["energy"].value_fn(snapshot) == 12.5
    assert by_key["capacity"].value_fn(snapshot) == 200
    assert by_key["temperature"].value_fn(snapshot) == 30
    assert by_key["runtime"].value_fn(snapshot) == 120
    assert by_key["voltage_dplus"].value_fn(snapshot) == 2.1
    assert by_key["voltage_dminus"].value_fn(snapshot) == 2.2

    # connection_state reads from the coordinator object directly.
    assert by_key["connection_state"].value_fn_coordinator(coordinator) == (
        coordinator.connection_state
    )


async def test_state_class_total_increasing(hass: HomeAssistant) -> None:
    """energy/capacity/runtime carry TOTAL_INCREASING; voltage carries MEASUREMENT."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    for key in _TOTAL_INCREASING:
        assert by_key[key].state_class == SensorStateClass.TOTAL_INCREASING

    assert by_key["voltage"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["current"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["power"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["temperature"].state_class == SensorStateClass.MEASUREMENT


async def test_device_class_correct(hass: HomeAssistant) -> None:
    """Per-sensor device_class matches the inventory."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    assert by_key["voltage"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["current"].device_class == SensorDeviceClass.CURRENT
    assert by_key["power"].device_class == SensorDeviceClass.POWER
    assert by_key["energy"].device_class == SensorDeviceClass.ENERGY
    assert by_key["temperature"].device_class == SensorDeviceClass.TEMPERATURE
    assert by_key["runtime"].device_class == SensorDeviceClass.DURATION
    assert by_key["voltage_dplus"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["voltage_dminus"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["connection_state"].device_class == SensorDeviceClass.ENUM
    # capacity has no canonical mAh device_class in HA yet.
    assert by_key["capacity"].device_class is None


async def test_entity_native_value_and_availability(
    hass: HomeAssistant,
) -> None:
    """AtorchBleSensor.native_value / available work against the coordinator.

    Exercises the entity class directly (sidestepping HA's state machine)
    so we cover the freshness-window availability rule, the value_fn
    branch, the value_fn_coordinator diagnostic branch, and the
    transition-debug log path on becoming unavailable.
    """
    from datetime import datetime, timedelta, timezone

    from custom_components.atorch_ble.sensor import (
        DESCRIPTIONS,
        AtorchBleSensor,
    )

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    by_key = {d.key: d for d in DESCRIPTIONS}
    voltage_sensor = AtorchBleSensor(coordinator, by_key["voltage"])
    conn_state_sensor = AtorchBleSensor(coordinator, by_key["connection_state"])

    # Seed coordinator.data — the ActiveBluetoothProcessorCoordinator base
    # class does not auto-create this attribute the way DataUpdateCoordinator
    # does, so the AtorchBleSensor.native_value access path needs it
    # explicitly. None means "no reading yet".
    coordinator.data = None

    # No reading yet -> voltage sensor unavailable (returns None / False).
    assert voltage_sensor.native_value is None
    assert voltage_sensor.available is False
    # Diagnostic sensor is always available; reports the connection_state.
    assert conn_state_sensor.available is True
    assert conn_state_sensor.native_value == coordinator.connection_state

    # Publish a reading + recent last_seen -> voltage sensor available.
    reading = UsbMeterReading(
        voltage_v=5.0,
        current_a=1.0,
        capacity_mah=10,
        energy_wh=1.0,
        voltage_dplus_v=2.5,
        voltage_dminus_v=2.5,
        temperature_c=20,
        duration_s=10,
    )
    coordinator._last_reading = reading
    coordinator._last_seen = datetime.now(timezone.utc)
    # Push the snapshot through the base coordinator's data setter
    # by patching the underlying _data attribute so the entity's
    # coordinator.data accessor sees the new snapshot. We avoid
    # async_set_updated_data here because it fires entity-registration
    # listener callbacks that the test isn't standing up.
    snap = AtorchBleData(reading=reading, power_w=5.0)
    coordinator.data = snap
    assert voltage_sensor.native_value == 5.0
    assert voltage_sensor.available is True

    # Backdate last_seen well past the freshness window -> unavailable.
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
    assert voltage_sensor.available is False


async def test_connection_state_enum_options(hass: HomeAssistant) -> None:
    """connection_state sensor exposes the closed-set options + DIAGNOSTIC category."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    desc = by_key["connection_state"]
    assert set(desc.options) == {
        "connected",
        "polling",
        "disconnected",
        "reconnecting",
        "failed_after_setup",
    }
    assert desc.entity_category == EntityCategory.DIAGNOSTIC
    assert desc.entity_registry_enabled_default is False
