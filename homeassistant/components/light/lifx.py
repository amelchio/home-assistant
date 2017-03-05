"""
Support for the LIFX platform that implements lights.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/light.lifx/
"""
import colorsys
import logging

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP, ATTR_RGB_COLOR, ATTR_TRANSITION,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR_TEMP, SUPPORT_RGB_COLOR,
    SUPPORT_TRANSITION, Light, PLATFORM_SCHEMA)
from homeassistant.helpers.event import track_time_change
from homeassistant.util.color import (
    color_temperature_mired_to_kelvin, color_temperature_kelvin_to_mired)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

# TODO: Not yet available for Python 3
# REQUIREMENTS = ['lifxlan==0.5.0']

BYTE_MAX = 255

CONF_BROADCAST = 'broadcast'
CONF_SERVER = 'server'

SHORT_MAX = 65535

TEMP_MAX = 9000
TEMP_MAX_HASS = 500
TEMP_MIN = 2500
TEMP_MIN_HASS = 154

SUPPORT_LIFX = (SUPPORT_BRIGHTNESS | SUPPORT_COLOR_TEMP | SUPPORT_RGB_COLOR |
                SUPPORT_TRANSITION)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_SERVER, default=None): cv.string,
    vol.Optional(CONF_BROADCAST, default=None): cv.string,
})


# pylint: disable=unused-argument
def setup_platform(hass, config, add_devices, discovery_info=None):
    """Setup the LIFX platform."""
    server_addr = config.get(CONF_SERVER)
    broadcast_addr = config.get(CONF_BROADCAST)

    lifx_library = LIFX(add_devices, server_addr, broadcast_addr)

    # Register our poll service
    track_time_change(hass, lifx_library.poll, second=[10, 40])

    lifx_library.probe()


class LIFX(object):
    """Representation of a LIFX light."""

    def __init__(self, add_devices_callback, server_addr=None,
                 broadcast_addr=None):
        """Initialize the light."""
        import lifxlan

        self._devices = {}

        self._add_devices_callback = add_devices_callback

        # TODO: support broadcast_addr?
        self._lifxlan = lifxlan.LifxLAN()

    def on_device(self, device):
        """Initialize the light."""

        # Request label, power and color from the network
        hsbk = device.get_color()

        _LOGGER.debug("bulb %s %s %d %d %d %d %d",
                      device.ip_addr, device.label, device.power_level, *hsbk)

        try:
            bulb = self._devices[device.ip_addr]
            bulb.set_power(device.power_level)
            bulb.set_color(*hsbk)
            bulb.schedule_update_ha_state()
        except (KeyError):
            _LOGGER.debug("new bulb %s %s", device.ip_addr, device.label)
            bulb = LIFXLight(device)
            self._devices[device.ip_addr] = bulb
            self._add_devices_callback([bulb])

    # pylint: disable=unused-argument
    def poll(self, now):
        """Polling for the light."""
        self.probe()

    def probe(self, address=None):
        """Probe the light."""
        for device in self._lifxlan.get_lights():
            self.on_device(device)


def convert_rgb_to_hsv(rgb):
    """Convert Home Assistant RGB values to HSV values."""
    red, green, blue = [_ / BYTE_MAX for _ in rgb]

    hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)

    return [int(hue * SHORT_MAX),
            int(saturation * SHORT_MAX),
            int(brightness * SHORT_MAX)]


class LIFXLight(Light):
    """Representation of a LIFX light."""

    def __init__(self, device):
        """Initialize the light."""
        _LOGGER.debug("LIFXLight: %s %s", device.ip_addr, device.label)

        self._device = device
        self.set_power(device.power_level)
        self.set_color(*device.color)

    @property
    def should_poll(self):
        """No polling needed for LIFX light."""
        return False

    @property
    def name(self):
        """Return the name of the device."""
        return self._device.label

    @property
    def ipaddr(self):
        """Return the IP address of the device."""
        return self._device.ip_addr

    @property
    def rgb_color(self):
        """Return the RGB value."""
        _LOGGER.debug(
            "rgb_color: [%d %d %d]", self._rgb[0], self._rgb[1], self._rgb[2])
        return self._rgb

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        brightness = int(self._bri / (BYTE_MAX + 1))
        _LOGGER.debug("brightness: %d", brightness)
        return brightness

    @property
    def color_temp(self):
        """Return the color temperature."""
        temperature = color_temperature_kelvin_to_mired(self._kel)

        _LOGGER.debug("color_temp: %d", temperature)
        return temperature

    @property
    def is_on(self):
        """Return true if device is on."""
        _LOGGER.debug("is_on: %d", self._power)
        return self._power != 0

    @property
    def supported_features(self):
        """Flag supported features."""
        return SUPPORT_LIFX

    def turn_on(self, **kwargs):
        """Turn the device on."""
        if ATTR_TRANSITION in kwargs:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = 0

        if ATTR_RGB_COLOR in kwargs:
            hue, saturation, brightness = \
                convert_rgb_to_hsv(kwargs[ATTR_RGB_COLOR])
        else:
            hue = self._hue
            saturation = self._sat
            brightness = self._bri

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS] * (BYTE_MAX + 1)
        else:
            brightness = self._bri

        if ATTR_COLOR_TEMP in kwargs:
            kelvin = int(color_temperature_mired_to_kelvin(
                kwargs[ATTR_COLOR_TEMP]))
        else:
            kelvin = self._kel

        hsbk = [ hue, saturation, brightness, kelvin ]
        _LOGGER.debug("turn_on: %s (%d) %d %d %d %d %d",
                      self.ipaddr, self._power, *hsbk, fade)

        if self._power == 0:
            self._device.set_color(hsbk, 0)
            self._device.set_power(True, fade)
        else:
            self._device.set_power(True, 0)     # racing for power status
            self._device.set_color(hsbk, fade)

        self.set_power(True)
        self.set_color(*hsbk)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs):
        """Turn the device off."""
        if ATTR_TRANSITION in kwargs:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = 0

        _LOGGER.debug("turn_off: %s %d", self.ipaddr, fade)
        self._device.set_power(False, fade)

        self.set_power(0)
        self.schedule_update_ha_state()

    def set_name(self, name):
        """Set name of the light."""
        self._name = name

    def set_power(self, power):
        """Set power state value."""
        _LOGGER.debug("set_power: %d", power)
        self._power = (power != 0)

    def set_color(self, hue, sat, bri, kel):
        """Set color state values."""
        self._hue = hue
        self._sat = sat
        self._bri = bri
        self._kel = kel

        red, green, blue = colorsys.hsv_to_rgb(hue / SHORT_MAX,
                                               sat / SHORT_MAX,
                                               bri / SHORT_MAX)

        red = int(red * BYTE_MAX)
        green = int(green * BYTE_MAX)
        blue = int(blue * BYTE_MAX)

        _LOGGER.debug("set_color: %d %d %d %d [%d %d %d]",
                      hue, sat, bri, kel, red, green, blue)

        self._rgb = [red, green, blue]
