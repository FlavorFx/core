"""Test the Network UPS Tools (NUT) config flow."""

from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.components.nut.const import DOMAIN
from homeassistant.const import CONF_RESOURCES, CONF_SCAN_INTERVAL

from .util import _get_mock_pynutclient

from tests.common import MockConfigEntry

VALID_CONFIG = {
    "host": "localhost",
    "port": 123,
    "name": "name",
    "resources": ["battery.charge"],
}


async def test_form_zeroconf(hass):
    """Test we can setup from zeroconf."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data={"host": "192.168.1.5", "port": 1234},
    )
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    mock_pynut = _get_mock_pynutclient(
        list_vars={"battery.voltage": "voltage", "ups.status": "OL"}, list_ups=["ups1"]
    )

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test-username", "password": "test-password"},
        )

    assert result2["step_id"] == "resources"
    assert result2["type"] == "form"

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ), patch(
        "homeassistant.components.nut.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {"resources": ["battery.voltage", "ups.status", "ups.status.display"]},
        )
        await hass.async_block_till_done()

    assert result3["type"] == "create_entry"
    assert result3["title"] == "192.168.1.5:1234"
    assert result3["data"] == {
        "host": "192.168.1.5",
        "password": "test-password",
        "port": 1234,
        "resources": ["battery.voltage", "ups.status", "ups.status.display"],
        "username": "test-username",
    }
    assert result3["result"].unique_id is None
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_user_one_ups(hass):
    """Test we get the form."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    mock_pynut = _get_mock_pynutclient(
        list_vars={"battery.voltage": "voltage", "ups.status": "OL"}, list_ups=["ups1"]
    )

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "1.1.1.1",
                "username": "test-username",
                "password": "test-password",
                "port": 2222,
            },
        )

    assert result2["step_id"] == "resources"
    assert result2["type"] == "form"

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ), patch(
        "homeassistant.components.nut.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {"resources": ["battery.voltage", "ups.status", "ups.status.display"]},
        )
        await hass.async_block_till_done()

    assert result3["type"] == "create_entry"
    assert result3["title"] == "1.1.1.1:2222"
    assert result3["data"] == {
        "host": "1.1.1.1",
        "password": "test-password",
        "port": 2222,
        "resources": ["battery.voltage", "ups.status", "ups.status.display"],
        "username": "test-username",
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_user_multiple_ups(hass):
    """Test we get the form."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "2.2.2.2", "port": 123, "resources": ["battery.charge"]},
        options={CONF_RESOURCES: ["battery.charge"]},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    mock_pynut = _get_mock_pynutclient(
        list_vars={"battery.voltage": "voltage"},
        list_ups={"ups1": "UPS 1", "ups2": "UPS2"},
    )

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "1.1.1.1",
                "username": "test-username",
                "password": "test-password",
                "port": 2222,
            },
        )

    assert result2["step_id"] == "ups"
    assert result2["type"] == "form"

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {"alias": "ups2"},
        )

    assert result3["step_id"] == "resources"
    assert result3["type"] == "form"

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ), patch(
        "homeassistant.components.nut.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result4 = await hass.config_entries.flow.async_configure(
            result3["flow_id"],
            {"resources": ["battery.voltage"]},
        )
        await hass.async_block_till_done()

    assert result4["type"] == "create_entry"
    assert result4["title"] == "ups2@1.1.1.1:2222"
    assert result4["data"] == {
        "host": "1.1.1.1",
        "password": "test-password",
        "alias": "ups2",
        "port": 2222,
        "resources": ["battery.voltage"],
        "username": "test-username",
    }
    assert len(mock_setup_entry.mock_calls) == 2


async def test_form_user_one_ups_with_ignored_entry(hass):
    """Test we can setup a new one when there is an ignored one."""
    ignored_entry = MockConfigEntry(
        domain=DOMAIN, data={}, source=config_entries.SOURCE_IGNORE
    )
    ignored_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    mock_pynut = _get_mock_pynutclient(
        list_vars={"battery.voltage": "voltage", "ups.status": "OL"}, list_ups=["ups1"]
    )

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "1.1.1.1",
                "username": "test-username",
                "password": "test-password",
                "port": 2222,
            },
        )

    assert result2["step_id"] == "resources"
    assert result2["type"] == "form"

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ), patch(
        "homeassistant.components.nut.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {"resources": ["battery.voltage", "ups.status", "ups.status.display"]},
        )
        await hass.async_block_till_done()

    assert result3["type"] == "create_entry"
    assert result3["title"] == "1.1.1.1:2222"
    assert result3["data"] == {
        "host": "1.1.1.1",
        "password": "test-password",
        "port": 2222,
        "resources": ["battery.voltage", "ups.status", "ups.status.display"],
        "username": "test-username",
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_cannot_connect(hass):
    """Test we handle cannot connect error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    mock_pynut = _get_mock_pynutclient()

    with patch(
        "homeassistant.components.nut.PyNUTClient",
        return_value=mock_pynut,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "1.1.1.1",
                "username": "test-username",
                "password": "test-password",
                "port": 2222,
            },
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_options_flow(hass):
    """Test config flow options."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="abcde12345",
        data=VALID_CONFIG,
    )
    config_entry.add_to_hass(hass)

    with patch("homeassistant.components.nut.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

        assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={}
        )

        assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
        assert config_entry.options == {
            CONF_SCAN_INTERVAL: 60,
        }

    with patch("homeassistant.components.nut.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.options.async_init(config_entry.entry_id)

        assert result2["type"] == data_entry_flow.RESULT_TYPE_FORM
        assert result2["step_id"] == "init"

        result2 = await hass.config_entries.options.async_configure(
            result2["flow_id"],
            user_input={CONF_SCAN_INTERVAL: 12},
        )

        assert result2["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
        assert config_entry.options == {
            CONF_SCAN_INTERVAL: 12,
        }
