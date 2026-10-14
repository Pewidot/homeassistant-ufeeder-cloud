"""Home Assistant fixtures with the plugin's network restrictions intact."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations_enabled(enable_custom_integrations):
    """Allow this local integration without enabling outbound sockets.

    pytest-homeassistant-custom-component supplies the hass fixture and blocks
    network sockets by default. API and MQTT operations are mocked in tests.
    """
