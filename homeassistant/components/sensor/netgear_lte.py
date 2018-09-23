"""Netgear LTE sensors.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.netgear_lte/
"""

import voluptuous as vol
import attr

from homeassistant.const import CONF_HOST, CONF_SENSORS
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity import Entity
import homeassistant.helpers.config_validation as cv

from ..netgear_lte import DATA_KEY

DEPENDENCIES = ['netgear_lte']

SENSOR_SMS = 'sms'
SENSOR_USAGE = 'usage'

SENSOR_UNITS = {
    SENSOR_SMS: 'unread',
    SENSOR_USAGE: 'MiB',
    'radio_quality': '%',
    'rx_level': 'level',
    'tx_level': 'level',
    'upstream': None,
    'connection': None,
    'connection_text': None,
    'connection_type': None,
    'current_nw_service_type': None,
    'current_ps_service_type': None,
    'register_network_display': None,
    'roaming': None,
    'current_band': None,
    'cell_id': None,
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_HOST): cv.string,
    vol.Required(CONF_SENSORS): vol.All(
        cv.ensure_list, [vol.In(SENSOR_UNITS.keys())])
})


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info):
    """Set up Netgear LTE sensor devices."""
    modem_data = hass.data[DATA_KEY].get_modem_data(config)

    if not modem_data:
        raise PlatformNotReady

    sensors = []
    for sensor_type in config[CONF_SENSORS]:
        if sensor_type == SENSOR_SMS:
            sensors.append(SMSSensor(modem_data, sensor_type))
        elif sensor_type == SENSOR_USAGE:
            sensors.append(UsageSensor(modem_data, sensor_type))
        else:
            sensors.append(InformationSensor(modem_data, sensor_type))

    async_add_entities(sensors, True)


@attr.s
class LTESensor(Entity):
    """Base LTE sensor entity."""

    modem_data = attr.ib()
    sensor_type = attr.ib()

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Netgear LTE {}".format(self.sensor_type)

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return SENSOR_UNITS[self.sensor_type]

    async def async_update(self):
        """Update state."""
        await self.modem_data.async_update()

    @property
    def unique_id(self):
        """Return a unique ID like 'usage_5TG365AB0078V'."""
        return "{}_{}".format(self.sensor_type, self.modem_data.serial_number)


class SMSSensor(LTESensor):
    """Unread SMS sensor entity."""

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.modem_data.unread_count


class UsageSensor(LTESensor):
    """Data usage sensor entity."""

    @property
    def state(self):
        """Return the state of the sensor."""
        if self.modem_data.information is None:
            return None

        return round(self.modem_data.information.usage / 1024**2, 1)


class InformationSensor(LTESensor):
    """Miscellaneous sensor entity."""

    @property
    def state(self):
        """Return the state of the sensor."""
        if self.modem_data.information is None:
            return None

        return getattr(self.modem_data.information, self.sensor_type)
