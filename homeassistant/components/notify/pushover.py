"""
Pushover platform for notify component.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/notify.pushover/
"""
import logging

import voluptuous as vol

from homeassistant.components.notify import (
    ATTR_TITLE, ATTR_TITLE_DEFAULT, ATTR_TARGET, ATTR_DATA,
    BaseNotificationService, PLATFORM_SCHEMA)
from homeassistant.const import CONF_API_KEY
import homeassistant.helpers.config_validation as cv

REQUIREMENTS = ['https://github.com/Thibauth/python-pushover/archive/'
                'master.zip#python-pushover==0.5']
_LOGGER = logging.getLogger(__name__)


CONF_USER_KEY = 'user_key'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_USER_KEY): cv.string,
    vol.Required(CONF_API_KEY): cv.string,
})


def get_service(hass, config, discovery_info=None):
    """Get the Pushover notification service."""
    return PushoverNotificationService(
        config[CONF_USER_KEY], config[CONF_API_KEY])


class PushoverNotificationService(BaseNotificationService):
    """Implement the notification service for Pushover."""

    def __init__(self, user_key, api_token):
        """Initialize the service."""
        from pushover import Pushover
        self.user_key = user_key
        self.pushover = Pushover(api_token)

    def send_message(self, message='', **kwargs):
        """Send a message to a user."""
        from pushover import RequestError

        # Make a copy and use empty dict if necessary
        data = dict(kwargs.get(ATTR_DATA) or {})

        data['title'] = kwargs.get(ATTR_TITLE, ATTR_TITLE_DEFAULT)

        targets = kwargs.get(ATTR_TARGET)

        if not isinstance(targets, list):
            targets = [targets]

        for target in targets:
            if target is not None:
                data['device'] = target

            try:
                self.pushover.message(self.user_key, message, **data)
            except (RequestError, ValueError) as ex:
                _LOGGER.error("Could not send notification: %s", str(ex))
