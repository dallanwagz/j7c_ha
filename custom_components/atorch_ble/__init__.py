"""The atorch_ble integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AtorchBleCoordinator

type AtorchBleConfigEntry = ConfigEntry[AtorchBleCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> bool:
    """Set up atorch_ble from a config entry."""
    # Establish hass.data[DOMAIN] slot before any downstream ticket writes to it.
    # Per implementer-locked "hass.data slot ownership" in PROJECT_CONTEXT.md.
    hass.data.setdefault(DOMAIN, {})

    coordinator = AtorchBleCoordinator(hass, entry)
    await coordinator.async_start()
    hass.data[DOMAIN][entry.entry_id] = coordinator

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


async def async_remove_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> None:
    """Remove a config entry."""
    return None
