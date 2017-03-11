"""
Support for the LIFX platform that implements lights.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/light.lifx/
"""
import colorsys
import logging
import asyncio
from functools import partial

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP, ATTR_RGB_COLOR, ATTR_TRANSITION,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR_TEMP, SUPPORT_RGB_COLOR,
    SUPPORT_TRANSITION, Light, PLATFORM_SCHEMA)
from homeassistant.util.color import (
    color_temperature_mired_to_kelvin, color_temperature_kelvin_to_mired)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

REQUIREMENTS = ['aiolifx==0.4.1']

UDP_BROADCAST_PORT = 56700

CONF_SERVER = 'server'

BYTE_MAX = 255
SHORT_MAX = 65535

SUPPORT_LIFX = (SUPPORT_BRIGHTNESS | SUPPORT_COLOR_TEMP | SUPPORT_RGB_COLOR |
                SUPPORT_TRANSITION)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_SERVER, default='0.0.0.0'): cv.string,
})


# pylint: disable=unused-argument
@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Setup the LIFX platform."""
    import aiolifx

    server_addr = config.get(CONF_SERVER)

    lifx_library = LIFX(hass, async_add_devices)

    coro = hass.loop.create_datagram_endpoint(
        partial(aiolifx.LifxDiscovery, hass.loop, lifx_library),
        local_addr=(server_addr, UDP_BROADCAST_PORT))

    hass.loop.create_task(coro)
    return True


class LIFX(object):
    """Representation of all known LIFX entities."""
    def __init__(self, hass, async_add_devices):
        """Initialize the light."""
        self.entities = {}
        self.hass = hass
        self.async_add_devices = async_add_devices

    def register(self,device):
        """Callback for newly detected bulb."""
        if device.mac_addr in self.entities:
            entity = self.entities[device.mac_addr]
            _LOGGER.debug("%s register AGAIN", entity.ipaddr)
            entity._available = True
            entity.schedule_update_ha_state()
        else:
            _LOGGER.debug("%s register NEW", device.ip_addr)
            device.get_color(self.ready)

    def ready(self, device, msg):
        """Callback that adds the device once all data is retrieved."""
        entity = LIFXLight(device)
        _LOGGER.debug("%s register READY", entity.ipaddr)
        self.entities[device.mac_addr] = entity
        self.hass.async_add_job(self.async_add_devices([entity]))

    def unregister(self,device):
        """Callback for disappearing bulb."""
        entity = self.entities[device.mac_addr]
        _LOGGER.debug("%s unregister", entity.ipaddr)
        entity._available = False
        entity.updated_event.set()
        entity.schedule_update_ha_state()


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

        self.device = device
        self.updated_event = asyncio.Event()
        self.blocker = None
        self._available = True
        self.set_power(device.power_level)
        self.set_color(*device.color)

    @property
    def available(self):
        """Return the availability of the device."""
        return self._available

    @property
    def name(self):
        """Return the name of the device."""
        return self.device.label

    @property
    def ipaddr(self):
        """Return the IP address of the device."""
        return self.device.ip_addr[0]

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

    @asyncio.coroutine
    def update_later(self):
        """Wait two seconds with updates disabled and then update."""
        yield from asyncio.sleep(2, loop=self.hass.loop)
        self.blocker = None
        self.schedule_update_ha_state(force_refresh=True)

    def block_updates(self):
        """Lock out async_update while the new state settles on the bulb."""
        if self.blocker:
            self.blocker.cancel()
        self.blocker = asyncio.ensure_future(self.update_later())

    @asyncio.coroutine
    def async_turn_on(self, **kwargs):
        """Turn the device on."""
        if ATTR_TRANSITION in kwargs:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = 0

        changed_color = False

        if ATTR_RGB_COLOR in kwargs:
            hue, saturation, brightness = \
                convert_rgb_to_hsv(kwargs[ATTR_RGB_COLOR])
            changed_color = True
        else:
            hue = self._hue
            saturation = self._sat
            brightness = self._bri

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS] * (BYTE_MAX + 1)
            changed_color = True
        else:
            brightness = self._bri

        if ATTR_COLOR_TEMP in kwargs:
            kelvin = int(color_temperature_mired_to_kelvin(
                kwargs[ATTR_COLOR_TEMP]))
            changed_color = True
        else:
            kelvin = self._kel

        hsbk = [ hue, saturation, brightness, kelvin ]
        _LOGGER.debug("turn_on: %s (%d) %d %d %d %d %d",
                      self.ipaddr, self._power, fade, *hsbk)

        if self._power == 0:
            if changed_color:
                self.device.set_color(hsbk, None, 0)
            self.device.set_power(True, None, fade)
        else:
            self.device.set_power(True, None, 0)     # racing for power status
            if changed_color:
                self.device.set_color(hsbk, None, fade)

        self.block_updates()
        self.set_power(1)
        self.set_color(*hsbk)

    @asyncio.coroutine
    def async_turn_off(self, **kwargs):
        """Turn the device off."""
        if ATTR_TRANSITION in kwargs:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = 0

        self.device.set_power(False, None, fade)

        self.block_updates()
        self.set_power(0)

    def got_color(self,device,msg):
        """Callback that gets current power/color status"""
        _LOGGER.debug("%s got_color", self.ipaddr)
        self.set_power(device.power_level)
        self.set_color(*device.color)
        self.updated_event.set()

    @asyncio.coroutine
    def async_update(self):
        """Update bulb status (if it is available)."""
        _LOGGER.debug("%s async_update", self.ipaddr)
        if self.available and self.blocker is None:
            self.updated_event.clear()
            self.device.get_color(self.got_color)
            yield from self.updated_event.wait()
        _LOGGER.debug("%s async_update done", self.ipaddr)

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
