"""Sensor platform for the atorch_ble integration.

Declarative ``SensorEntityDescription``-driven entity model. A single
:class:`AtorchBleSensor` class hosts all per-sensor variation via the
:class:`AtorchBleSensorEntityDescription` dataclass, which carries two
mutually-exclusive value-source fields:

* ``value_fn`` — extracts a value from the coordinator-published
  :class:`~.coordinator.AtorchBleData` snapshot.
* ``value_fn_coordinator`` — reads a value from the
  :class:`~.coordinator.AtorchBleCoordinator` directly (used by the
  diagnostic ``connection_state`` sensor landed in ``batch-3/05``).

Exactly one of the two is set per description.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util.dt import utcnow

from .const import CONNECTION_STATES, DOMAIN
from .coordinator import AtorchBleCoordinator, AtorchBleData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AtorchBleSensorEntityDescription(SensorEntityDescription):
    """Description for an atorch_ble sensor entity.

    Exactly one of ``value_fn`` / ``value_fn_coordinator`` must be
    non-None per description. ``value_fn`` is the standard idiom for
    sensors whose value is extracted from the coordinator-published
    :class:`AtorchBleData` snapshot. ``value_fn_coordinator`` is the
    escape hatch for diagnostic sensors (e.g. ``connection_state``,
    landed in ``batch-3/05``) whose value comes from the coordinator
    object itself and must remain available even when no data has been
    published yet.
    """

    value_fn: Callable[[AtorchBleData], StateType] | None = None
    value_fn_coordinator: Callable[[AtorchBleCoordinator], StateType] | None = None

    def __post_init__(self) -> None:
        """Enforce exactly-one-of invariant."""
        both_set = self.value_fn is not None and self.value_fn_coordinator is not None
        neither_set = self.value_fn is None and self.value_fn_coordinator is None
        if both_set or neither_set:
            raise ValueError(
                f"AtorchBleSensorEntityDescription {self.key!r}: exactly one of "
                "value_fn / value_fn_coordinator must be set"
            )


# Capacity unit: HA Core's UnitOfElectricCharge enum does not include a
# MILLIAMPERE_HOUR member as of HA 2026. Use the literal "mAh" string
# until the enum gains the value; sensor device_class is None for the
# same reason (no `ELECTRIC_CHARGE` SensorDeviceClass mAh variant
# exists). Migrate to the enum when HA Core adds it.
_CAPACITY_UNIT: str = "mAh"


DESCRIPTIONS: tuple[AtorchBleSensorEntityDescription, ...] = (
    AtorchBleSensorEntityDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.voltage_v,
    ),
    AtorchBleSensorEntityDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=3,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.current_a,
    ),
    AtorchBleSensorEntityDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=3,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.power_w,
    ),
    AtorchBleSensorEntityDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_display_precision=2,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.energy_wh,
    ),
    AtorchBleSensorEntityDescription(
        key="capacity",
        translation_key="capacity",
        device_class=None,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=_CAPACITY_UNIT,
        suggested_display_precision=0,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.capacity_mah,
    ),
    # Secondary sensors (batch-3/02). Temperature and runtime are
    # default-enabled; the two USB data-line voltages below are
    # advanced/disabled-by-default.
    AtorchBleSensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.temperature_c,
    ),
    AtorchBleSensorEntityDescription(
        key="runtime",
        translation_key="runtime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        entity_registry_enabled_default=True,
        value_fn=lambda d: d.reading.duration_s,
    ),
    # D+ / D- expose the USB data-line voltages reported by the J7-C.
    # They are advanced/disabled-by-default — useful primarily for
    # USB-PD or charger-protocol debugging — so they ship with
    # ``entity_registry_enabled_default=False`` and a translation_key
    # so users can rename / enable them later.
    AtorchBleSensorEntityDescription(
        key="voltage_dplus",
        translation_key="voltage_dplus",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.reading.voltage_dplus_v,
    ),
    AtorchBleSensorEntityDescription(
        key="voltage_dminus",
        translation_key="voltage_dminus",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.reading.voltage_dminus_v,
    ),
    # Diagnostic connection-state sensor (batch-3/05). Reads directly
    # from the coordinator (not coordinator.data) so it remains
    # available even when the device is unreachable — that is precisely
    # when the user needs to see ``failed_after_setup``. Disabled by
    # default to keep the device card uncluttered for users who do not
    # need to troubleshoot the connection lifecycle.
    AtorchBleSensorEntityDescription(
        key="connection_state",
        translation_key="connection_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(CONNECTION_STATES),
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn_coordinator=lambda coord: coord.connection_state,
    ),
)


class AtorchBleSensor(
    PassiveBluetoothCoordinatorEntity[AtorchBleCoordinator], SensorEntity
):
    """Sensor entity backed by the atorch_ble coordinator.

    Variation across the five primary sensors (and future additions)
    lives entirely in the :class:`AtorchBleSensorEntityDescription` —
    this class is a thin generic shim.
    """

    entity_description: AtorchBleSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AtorchBleCoordinator,
        description: AtorchBleSensorEntityDescription,
    ) -> None:
        """Initialize the entity from its coordinator + description."""
        super().__init__(coordinator)
        self.entity_description = description
        # mac_normalized includes colons; strip them for the unique_id
        # suffix to avoid colon-bearing entity ids in the registry while
        # still leaving the canonical colon-form on the device identifier.
        mac_stripped = coordinator.mac_normalized.replace(":", "")
        self._attr_unique_id = f"{mac_stripped}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.mac_normalized)},
        )
        # Tracks the previous availability value so we can emit a single
        # DEBUG line on each True -> False transition (per ticket #12).
        # Seeded ``True`` so the first stale read produces one log line;
        # value_fn_coordinator-backed entities never flip and never log.
        self._available_prev: bool = True

    @property
    def native_value(self) -> StateType:
        """Return the current value of this sensor.

        Branches on which value-source field is set on the description.
        For ``value_fn``-based sensors, returns ``None`` when the
        coordinator has not yet produced a snapshot (HA UI renders as
        "Unknown"). For ``value_fn_coordinator``-based sensors, calls
        the callable unconditionally — those are diagnostic and must
        report regardless of data freshness.
        """
        description = self.entity_description
        if description.value_fn is not None:
            data = self.coordinator.data
            if data is None:
                return None
            return description.value_fn(data)
        if description.value_fn_coordinator is not None:
            return description.value_fn_coordinator(self.coordinator)
        return None

    @property
    def available(self) -> bool:
        """Return availability per the locked freshness-only rule (#12).

        Diagnostic ``value_fn_coordinator``-backed entities (notably the
        ``connection_state`` sensor) report ``True`` unconditionally so
        the user can see *why* measurement sensors are unavailable.

        All other (``value_fn``-backed) measurement entities are
        available iff ``coordinator.last_seen`` is within a freshness
        window of ``2 * coordinator.expected_cadence_seconds``. The rule
        is freshness-alone — ``coordinator.connection_state`` is
        intentionally NOT consulted because polled-mode sensors spend
        the bulk of each cadence in the ``"disconnected"`` idle state
        and would otherwise be permanently unavailable. ``super().available``
        is also not consulted: freshness is the authoritative signal.
        """
        # This override deliberately supersedes
        # PassiveBluetoothCoordinatorEntity.available (advertisement-driven,
        # not measurement-freshness driven).
        if self.entity_description.value_fn_coordinator is not None:
            return True
        last_seen = self.coordinator.last_seen
        if last_seen is None:
            available = False
        else:
            window = timedelta(
                seconds=2 * self.coordinator.expected_cadence_seconds
            )
            available = (utcnow() - last_seen) <= window
        # Emit one DEBUG line per True -> False transition; never log
        # the reverse direction to keep logs quiet on healthy reconnects.
        if self._available_prev and not available:
            _LOGGER.debug(
                "entity %s became unavailable (stale data)", self.entity_id
            )
        self._available_prev = available
        return available


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up atorch_ble sensors from a config entry."""
    coordinator: AtorchBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AtorchBleSensor(coordinator, description)
        for description in DESCRIPTIONS
    )
