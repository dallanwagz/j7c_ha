"""Tests for the atorch_ble config flow.

Covers every documented step and edge case for the Bluetooth-discovery
config flow: the happy path through ``async_step_bluetooth`` →
``async_step_bluetooth_confirm`` → ``CREATE_ENTRY``, plus the three
abort paths (already configured, unsupported local name, and the
discovery-only abort wired into the manual user step).
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import DOMAIN

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


async def test_bluetooth_discovery_creates_entry(
    hass: HomeAssistant, discovery_info
) -> None:
    """A fresh UC96_BLE discovery flows through confirm to a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=discovery_info,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == TEST_TITLE
    assert result2["data"] == {CONF_ADDRESS: TEST_MAC_NORMALIZED}
    assert result2["result"].unique_id == TEST_MAC_NORMALIZED


async def test_bluetooth_already_configured(
    hass: HomeAssistant, discovery_info
) -> None:
    """A second discovery for an already-configured MAC aborts cleanly."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED},
        title=TEST_TITLE,
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=discovery_info,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_not_supported_device(
    hass: HomeAssistant, stray_discovery_info
) -> None:
    """A discovery with a non-UC96_BLE local name aborts as unsupported."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=stray_discovery_info,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported_device"


async def test_user_step_aborts_discovery_only(hass: HomeAssistant) -> None:
    """Clicking Add Integration → atorch_ble aborts with discovery_only."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "discovery_only"


@pytest.mark.usefixtures("hass")
async def test_bluetooth_confirm_form_placeholders(
    hass: HomeAssistant, discovery_info
) -> None:
    """The confirm step surfaces the advertised name + MAC for the user.

    Covers the form-render branch (``user_input is None``) inside
    ``async_step_bluetooth_confirm`` — the happy-path test above
    only exercises the create-entry branch.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=discovery_info,
    )

    assert result["type"] is FlowResultType.FORM
    placeholders = result["description_placeholders"]
    assert placeholders["name"] == "UC96_BLE"
    assert placeholders["address"] == TEST_MAC_NORMALIZED
