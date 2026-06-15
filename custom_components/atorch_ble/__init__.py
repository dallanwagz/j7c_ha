"""The atorch_ble integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PACKET_TYPE_TO_MODEL
from .coordinator import AtorchBleCoordinator

type AtorchBleConfigEntry = ConfigEntry[AtorchBleCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> bool:
    """Set up atorch_ble from a config entry."""
    # Establish hass.data[DOMAIN] slot before any downstream ticket writes to it.
    # Per implementer-locked "hass.data slot ownership" in PROJECT_CONTEXT.md.
    hass.data.setdefault(DOMAIN, {})

    # test-before-setup: the integration actively opens GATT connections,
    # so it must not set up entities for a meter that is not currently
    # reachable. Resolve a connectable BLEDevice up front and raise
    # ConfigEntryNotReady if none exists; HA then retries setup when the
    # meter next advertises. The address MUST be uppercased — HA's
    # bluetooth manager keys its device dictionaries by uppercase address
    # and does a plain dict.get() (a lowercase address was a real
    # production bug fixed in v0.1.5).
    address: str = entry.data[CONF_ADDRESS]
    if (
        bluetooth.async_ble_device_from_address(
            hass, address.upper(), connectable=True
        )
        is None
    ):
        raise ConfigEntryNotReady(
            f"{address} is not currently reachable; will retry when it advertises"
        )

    coordinator = AtorchBleCoordinator(hass, entry)
    await coordinator.async_start_runner()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Initial device-registry registration (per ticket #15). Runtime model
    # updates on observing an unsupported packet type are owned by the
    # coordinator (#21) which calls device_registry.async_update_device
    # from the notification callback path. We register the canonical
    # J7-C entry here so sensors created in batch-3 have a device to
    # attach to, even before the first frame is decoded.
    mac_normalized = coordinator.mac_normalized
    mac_last4_upper = mac_normalized.replace(":", "")[-4:].upper()
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, mac_normalized)},
        connections={(dr.CONNECTION_BLUETOOTH, mac_normalized)},
        manufacturer="Atorch",
        model=PACKET_TYPE_TO_MODEL[0x03],
        name=f"Atorch J7-C ({mac_last4_upper})",
        hw_version="UC96",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: AtorchBleCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_unload()
    return unload_ok
