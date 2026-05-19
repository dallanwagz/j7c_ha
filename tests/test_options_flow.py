"""Tests for the atorch_ble options flow.

Covers the dynamic schema (mode selector + conditional poll-interval),
boundary validation on ``poll_interval_seconds``, and the schema
re-render when the user flips ``connection_mode`` between persistent
and polled.
"""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import (
    CONF_CONNECTION_MODE,
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MODE_PERSISTENT,
    MODE_POLLED,
)

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


def _make_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED},
        title=TEST_TITLE,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def test_default_persistent(hass: HomeAssistant) -> None:
    """Opening options on a fresh entry defaults to persistent mode.

    Submitting with no overrides keeps mode persistent and stores only
    the connection_mode key on the entry's options.
    """
    entry = _make_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # The rendered schema's default for connection_mode should be persistent.
    schema = result["data_schema"].schema
    mode_key = next(
        k for k in schema if getattr(k, "schema", None) == CONF_CONNECTION_MODE
    )
    assert mode_key.default() == MODE_PERSISTENT
    # No interval field on the default-persistent render.
    assert not any(
        getattr(k, "schema", None) == CONF_POLL_INTERVAL_SECONDS for k in schema
    )

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_CONNECTION_MODE: MODE_PERSISTENT},
    )
    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"] == {CONF_CONNECTION_MODE: MODE_PERSISTENT}


async def test_switch_to_polled_with_valid_interval(hass: HomeAssistant) -> None:
    """Switching mode to polled with a valid interval saves both fields."""
    entry = _make_entry(hass)

    # First render: persistent default, no interval field.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Submit mode=polled; flow re-renders with the interval field.
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_CONNECTION_MODE: MODE_POLLED},
    )
    assert result2["type"] is FlowResultType.FORM
    schema = result2["data_schema"].schema
    assert any(
        getattr(k, "schema", None) == CONF_POLL_INTERVAL_SECONDS for k in schema
    )

    # Submit the polled mode + a valid interval; entry saves.
    result3 = await hass.config_entries.options.async_configure(
        result2["flow_id"],
        user_input={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )
    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["data"] == {
        CONF_CONNECTION_MODE: MODE_POLLED,
        CONF_POLL_INTERVAL_SECONDS: 60,
    }


@pytest.mark.parametrize(
    ("interval", "should_pass"),
    [
        (9, False),
        (10, True),
        (3600, True),
        (3601, False),
    ],
)
async def test_polled_interval_out_of_range(
    hass: HomeAssistant, interval: int, should_pass: bool
) -> None:
    """Boundary values for the poll interval enforce the 10..3600 range."""
    entry = _make_entry(
        hass,
        options={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: 60,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: interval,
        },
    )

    if should_pass:
        assert result2["type"] is FlowResultType.CREATE_ENTRY
        assert result2["data"] == {
            CONF_CONNECTION_MODE: MODE_POLLED,
            CONF_POLL_INTERVAL_SECONDS: interval,
        }
    else:
        assert result2["type"] is FlowResultType.FORM
        assert result2["errors"] == {
            CONF_POLL_INTERVAL_SECONDS: "interval_out_of_range"
        }


async def test_mode_change_persistent_to_polled_shows_interval_field(
    hass: HomeAssistant,
) -> None:
    """Flipping the mode selector mid-flow re-renders the schema."""
    entry = _make_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema_initial = result["data_schema"].schema
    assert not any(
        getattr(k, "schema", None) == CONF_POLL_INTERVAL_SECONDS
        for k in schema_initial
    )

    # User flips to polled without supplying an interval yet — flow
    # re-renders the schema with the interval field present so the user
    # can fill it in.
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_CONNECTION_MODE: MODE_POLLED},
    )
    assert result2["type"] is FlowResultType.FORM
    schema_polled = result2["data_schema"].schema
    interval_keys = [
        k for k in schema_polled if getattr(k, "schema", None) == CONF_POLL_INTERVAL_SECONDS
    ]
    assert len(interval_keys) == 1
    # Default for the interval field comes from DEFAULT_POLL_INTERVAL_SECONDS.
    assert interval_keys[0].default() == 60
