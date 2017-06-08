"""
Support for the LIFX platform that implements lights.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/light.lifx/
"""
import logging
import asyncio
import sys
import math
from os import path
from functools import partial
from datetime import timedelta

import voluptuous as vol

from homeassistant.components.light import (
    Light, DOMAIN, PLATFORM_SCHEMA, LIGHT_TURN_ON_SCHEMA,
    ATTR_BRIGHTNESS, ATTR_BRIGHTNESS_PCT, ATTR_COLOR_NAME, ATTR_RGB_COLOR,
    ATTR_XY_COLOR, ATTR_COLOR_TEMP, ATTR_KELVIN, ATTR_TRANSITION, ATTR_EFFECT,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR_TEMP, SUPPORT_RGB_COLOR,
    SUPPORT_XY_COLOR, SUPPORT_TRANSITION, SUPPORT_EFFECT,
    VALID_BRIGHTNESS, VALID_BRIGHTNESS_PCT,
    preprocess_turn_on_alternatives)
from homeassistant.config import load_yaml_config_file
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant import util
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.service import extract_entity_ids
import homeassistant.helpers.config_validation as cv
import homeassistant.util.color as color_util

_LOGGER = logging.getLogger(__name__)

REQUIREMENTS = ['aiolifx==0.5.0', 'aiolifx_effects==0.1.0']

UDP_BROADCAST_PORT = 56700

CONF_SERVER = 'server'
CONF_DISCOVERY_INTERVAL = 'discovery_interval'
CONF_MESSAGE_TIMEOUT = 'message_timeout'
CONF_MESSAGE_RETRIES = 'message_retries'
CONF_UNAVAILABLE_GRACE = 'unavailable_grace'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_SERVER, default='0.0.0.0'): cv.string,
    # The following are for testing only, they will be removed without warning
    vol.Optional(CONF_DISCOVERY_INTERVAL, default=10): cv.positive_int,
    vol.Optional(CONF_MESSAGE_TIMEOUT, default=2.0): vol.Coerce(float),
    vol.Optional(CONF_MESSAGE_RETRIES, default=4): cv.positive_int,
    vol.Optional(CONF_UNAVAILABLE_GRACE, default=60): cv.positive_int,
})

SERVICE_LIFX_SET_STATE = 'lifx_set_state'

ATTR_INFRARED = 'infrared'
ATTR_POWER = 'power'

LIFX_SET_STATE_SCHEMA = LIGHT_TURN_ON_SCHEMA.extend({
    ATTR_INFRARED: vol.All(vol.Coerce(int), vol.Clamp(min=0, max=255)),
    ATTR_POWER: cv.boolean,
})

SERVICE_EFFECT_PULSE = 'lifx_effect_pulse'
SERVICE_EFFECT_COLORLOOP = 'lifx_effect_colorloop'
SERVICE_EFFECT_STOP = 'lifx_effect_stop'

ATTR_POWER_ON = 'power_on'
ATTR_MODE = 'mode'
ATTR_PERIOD = 'period'
ATTR_CYCLES = 'cycles'
ATTR_SPREAD = 'spread'
ATTR_CHANGE = 'change'

PULSE_MODE_BLINK = 'blink'
PULSE_MODE_BREATHE = 'breathe'
PULSE_MODE_PING = 'ping'
PULSE_MODE_STROBE = 'strobe'
PULSE_MODE_SOLID = 'solid'

PULSE_MODES = [PULSE_MODE_BLINK, PULSE_MODE_BREATHE, PULSE_MODE_PING,
               PULSE_MODE_STROBE, PULSE_MODE_SOLID]

LIFX_EFFECT_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_POWER_ON, default=True): cv.boolean,
})

LIFX_EFFECT_PULSE_SCHEMA = LIFX_EFFECT_SCHEMA.extend({
    ATTR_BRIGHTNESS: VALID_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT: VALID_BRIGHTNESS_PCT,
    ATTR_COLOR_NAME: cv.string,
    ATTR_RGB_COLOR: vol.All(vol.ExactSequence((cv.byte, cv.byte, cv.byte)),
                            vol.Coerce(tuple)),
    ATTR_COLOR_TEMP: vol.All(vol.Coerce(int), vol.Range(min=1)),
    ATTR_KELVIN: vol.All(vol.Coerce(int), vol.Range(min=0)),
    ATTR_PERIOD: vol.All(vol.Coerce(float), vol.Range(min=0.05)),
    ATTR_CYCLES: vol.All(vol.Coerce(float), vol.Range(min=1)),
    ATTR_MODE: vol.In(PULSE_MODES),
})

LIFX_EFFECT_COLORLOOP_SCHEMA = LIFX_EFFECT_SCHEMA.extend({
    ATTR_BRIGHTNESS: VALID_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT: VALID_BRIGHTNESS_PCT,
    ATTR_PERIOD: vol.All(vol.Coerce(float), vol.Clamp(min=0.05)),
    ATTR_CHANGE: vol.All(vol.Coerce(float), vol.Clamp(min=0, max=360)),
    ATTR_SPREAD: vol.All(vol.Coerce(float), vol.Clamp(min=0, max=360)),
    ATTR_TRANSITION: vol.All(vol.Coerce(float), vol.Range(min=0)),
})

LIFX_EFFECT_STOP_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
})


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the LIFX platform."""
    import aiolifx

    if sys.platform == 'win32':
        _LOGGER.warning("The lifx platform is known to not work on Windows. "
                        "Consider using the lifx_legacy platform instead")

    server_addr = config.get(CONF_SERVER)

    lifx_manager = LIFXManager(
        hass,
        async_add_devices,
        timeout=config.get(CONF_MESSAGE_TIMEOUT),
        retries=config.get(CONF_MESSAGE_RETRIES),
        grace=config.get(CONF_UNAVAILABLE_GRACE))

    lifx_discovery = aiolifx.LifxDiscovery(
        hass.loop,
        lifx_manager,
        discovery_interval=config.get(CONF_DISCOVERY_INTERVAL))

    coro = hass.loop.create_datagram_endpoint(
        lambda: lifx_discovery, local_addr=(server_addr, UDP_BROADCAST_PORT))

    hass.async_add_job(coro)

    @callback
    def cleanup(event):
        """Clean up resources."""
        lifx_discovery.cleanup()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, cleanup)

    return True


def find_hsbk(**kwargs):
    """Find the desired color from a number of possible inputs."""
    hue, saturation, brightness, kelvin = [None]*4

    preprocess_turn_on_alternatives(kwargs)

    if ATTR_RGB_COLOR in kwargs:
        hue, saturation, brightness = \
            color_util.color_RGB_to_hsv(*kwargs[ATTR_RGB_COLOR])
        saturation = convert_8_to_16(saturation)
        brightness = convert_8_to_16(brightness)
        kelvin = 3500

    if ATTR_XY_COLOR in kwargs:
        hue, saturation = color_util.color_xy_to_hs(*kwargs[ATTR_XY_COLOR])
        saturation = convert_8_to_16(saturation)
        kelvin = 3500

    if ATTR_COLOR_TEMP in kwargs:
        kelvin = int(color_util.color_temperature_mired_to_kelvin(
            kwargs[ATTR_COLOR_TEMP]))
        saturation = 0

    if ATTR_BRIGHTNESS in kwargs:
        brightness = convert_8_to_16(kwargs[ATTR_BRIGHTNESS])

    hsbk = [hue, saturation, brightness, kelvin]
    return None if hsbk == [None]*4 else hsbk


def merge_hsbk(base, change):
    """Copy change on top of base, except when None."""
    if change is None:
        return None
    return list(map(lambda x, y: y if y is not None else x, base, change))


class LIFXManager(object):
    """Representation of all known LIFX entities."""

    def __init__(self, hass, async_add_devices, timeout, retries, grace):
        """Initialize the light."""
        import aiolifx_effects
        self.entities = {}
        self.hass = hass
        self.async_add_devices = async_add_devices
        self.message_timeout = timeout
        self.message_retries = retries
        self.unavailable_grace = timedelta(seconds=grace)
        self.effects_conductor = aiolifx_effects.Conductor(loop=hass.loop)

        descriptions = load_yaml_config_file(
            path.join(path.dirname(__file__), 'services.yaml'))

        self.register_set_state(descriptions)
        self.register_effects(descriptions)

    def register_set_state(self, descriptions):
        """Register the LIFX set_state service call."""
        @asyncio.coroutine
        def async_service_handle(service):
            """Apply a service."""
            tasks = []
            for light in self.service_to_entities(service):
                if service.service == SERVICE_LIFX_SET_STATE:
                    task = light.async_set_state(**service.data)
                tasks.append(self.hass.async_add_job(task))
            if tasks:
                yield from asyncio.wait(tasks, loop=self.hass.loop)

        self.hass.services.async_register(
            DOMAIN, SERVICE_LIFX_SET_STATE, async_service_handle,
            descriptions.get(SERVICE_LIFX_SET_STATE),
            schema=LIFX_SET_STATE_SCHEMA)

    def register_effects(self, descriptions):
        """Register the LIFX effects as hass service calls."""
        @asyncio.coroutine
        def async_service_handle(service):
            """Apply a service, i.e. start an effect."""
            entities = self.service_to_entities(service)
            if entities:
                yield from self.start_effect(
                    entities, service.service, **service.data)

        self.hass.services.async_register(
            DOMAIN, SERVICE_EFFECT_PULSE, async_service_handle,
            descriptions.get(SERVICE_EFFECT_PULSE),
            schema=LIFX_EFFECT_PULSE_SCHEMA)

        self.hass.services.async_register(
            DOMAIN, SERVICE_EFFECT_COLORLOOP, async_service_handle,
            descriptions.get(SERVICE_EFFECT_COLORLOOP),
            schema=LIFX_EFFECT_COLORLOOP_SCHEMA)

        self.hass.services.async_register(
            DOMAIN, SERVICE_EFFECT_STOP, async_service_handle,
            descriptions.get(SERVICE_EFFECT_STOP),
            schema=LIFX_EFFECT_STOP_SCHEMA)

    @asyncio.coroutine
    def start_effect(self, entities, service, **kwargs):
        """Start a light effect on entities."""
        import aiolifx_effects
        devices = list(map(lambda l: l.device, entities))

        if service == SERVICE_EFFECT_PULSE:
            effect = aiolifx_effects.EffectPulse(
                power_on=kwargs.get(ATTR_POWER_ON, None),
                period=kwargs.get(ATTR_PERIOD, None),
                cycles=kwargs.get(ATTR_CYCLES, None),
                mode=kwargs.get(ATTR_MODE, None),
                hsbk=find_hsbk(**kwargs),
            )
            yield from self.effects_conductor.start(effect, devices)
        elif service == SERVICE_EFFECT_COLORLOOP:
            preprocess_turn_on_alternatives(kwargs)

            brightness = None
            if ATTR_BRIGHTNESS in kwargs:
                brightness = convert_8_to_16(kwargs[ATTR_BRIGHTNESS])

            effect = aiolifx_effects.EffectColorloop(
                power_on=kwargs.get(ATTR_POWER_ON, None),
                period=kwargs.get(ATTR_PERIOD, None),
                change=kwargs.get(ATTR_CHANGE, None),
                spread=kwargs.get(ATTR_SPREAD, None),
                transition=kwargs.get(ATTR_TRANSITION, None),
                brightness=brightness,
            )
            yield from self.effects_conductor.start(effect, devices)
        elif service == SERVICE_EFFECT_STOP:
            yield from self.effects_conductor.stop(devices)

    def service_to_entities(self, service):
        """Return the known devices that a service call mentions."""
        entity_ids = extract_entity_ids(self.hass, service)
        if entity_ids:
            entities = [entity for entity in self.entities.values()
                        if entity.entity_id in entity_ids]
        else:
            entities = list(self.entities.values())

        return entities

    @callback
    def register(self, device):
        """Handle for newly detected bulb."""
        if device.mac_addr in self.entities:
            entity = self.entities[device.mac_addr]
            _LOGGER.debug("%s register AGAIN", entity.who)
            self.hass.async_add_job(entity.set_available())
        else:
            _LOGGER.debug("%s register NEW", device.ip_addr)
            device.timeout = self.message_timeout
            device.retry_count = self.message_retries
            device.get_version(self.got_version)

    @callback
    def got_version(self, device, msg):
        """Request current color setting once we have the product version."""
        device.get_color(self.ready)

    @callback
    def ready(self, device, msg):
        """Handle the device once all data is retrieved."""
        entity = LIFXLight(device, self.effects_conductor)
        _LOGGER.debug("%s register READY", entity.who)
        self.entities[device.mac_addr] = entity
        self.async_add_devices([entity])

    @callback
    def unregister(self, device):
        """Message lost; schedule light to be unavailable."""
        if device.mac_addr in self.entities:
            entity = self.entities[device.mac_addr]
            _LOGGER.debug("%s unregister", entity.who)
            if entity.available and entity.unavailable_task is None:
                entity.unavailable_task = async_track_point_in_utc_time(
                    self.hass, entity.set_unavailable,
                    util.dt.utcnow() + self.unavailable_grace)


class AwaitAioLIFX:
    """Wait for an aiolifx callback and return the message."""

    def __init__(self, light):
        """Initialize the wrapper."""
        self.light = light
        self.device = None
        self.message = None
        self.event = asyncio.Event()

    @callback
    def callback(self, device, message):
        """Handle responses."""
        self.device = device
        self.message = message
        self.event.set()

    @asyncio.coroutine
    def wait(self, method):
        """Call an aiolifx method and wait for its response."""
        self.device = None
        self.message = None
        self.event.clear()
        method(self.callback)

        yield from self.event.wait()

        return self.message


def convert_8_to_16(value):
    """Scale an 8 bit level into 16 bits."""
    return (value << 8) | value


def convert_16_to_8(value):
    """Scale a 16 bit level into 8 bits."""
    return value >> 8


class LIFXLight(Light):
    """Representation of a LIFX light."""

    def __init__(self, device, effects_conductor):
        """Initialize the light."""
        self.device = device
        self.effects_conductor = effects_conductor
        self.registered = True
        self.unavailable_task = None
        self.product = device.product
        self.postponed_update = None

    @property
    def lifxwhite(self):
        """Return whether this is a white-only bulb."""
        # https://lan.developer.lifx.com/docs/lifx-products
        return self.product in [10, 11, 18]

    @property
    def available(self):
        """Return the availability of the device."""
        return self.registered

    @asyncio.coroutine
    def set_available(self):
        """Handle bulbs returning to service."""
        self.registered = True
        if self.unavailable_task:
            self.unavailable_task()
            self.unavailable_task = None
        yield from self.async_update()
        yield from self.async_update_ha_state()

    @asyncio.coroutine
    def set_unavailable(self, now):
        """Handle bulbs disappearing."""
        self.registered = False
        self.unavailable_task = None
        yield from self.async_update_ha_state()

    @property
    def name(self):
        """Return the name of the device."""
        return self.device.label

    @property
    def who(self):
        """Return a string identifying the device."""
        ip_addr = '-'
        if self.device:
            ip_addr = self.device.ip_addr[0]
        return "%s (%s)" % (ip_addr, self.name)

    @property
    def rgb_color(self):
        """Return the RGB value."""
        hue, sat, bri, _ = self.device.color

        return color_util.color_hsv_to_RGB(
            hue, convert_16_to_8(sat), convert_16_to_8(bri))

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        brightness = convert_16_to_8(self.device.color[2])
        _LOGGER.debug("brightness: %d", brightness)
        return brightness

    @property
    def color_temp(self):
        """Return the color temperature."""
        kelvin = self.device.color[3]
        temperature = color_util.color_temperature_kelvin_to_mired(kelvin)

        _LOGGER.debug("color_temp: %d", temperature)
        return temperature

    @property
    def min_mireds(self):
        """Return the coldest color_temp that this light supports."""
        # The 3 LIFX "White" products supported a limited temperature range
        if self.lifxwhite:
            kelvin = 6500
        else:
            kelvin = 9000
        return math.floor(color_util.color_temperature_kelvin_to_mired(kelvin))

    @property
    def max_mireds(self):
        """Return the warmest color_temp that this light supports."""
        # The 3 LIFX "White" products supported a limited temperature range
        if self.lifxwhite:
            kelvin = 2700
        else:
            kelvin = 2500
        return math.ceil(color_util.color_temperature_kelvin_to_mired(kelvin))

    @property
    def is_on(self):
        """Return true if device is on."""
        return self.device.power_level != 0

    @property
    def effect(self):
        """Return the name of the currently running effect."""
        effect = self.effects_conductor.effect(self.device)
        if effect:
            return 'lifx_effect_' + effect.name
        return None

    @property
    def supported_features(self):
        """Flag supported features."""
        features = (SUPPORT_BRIGHTNESS | SUPPORT_COLOR_TEMP |
                    SUPPORT_TRANSITION | SUPPORT_EFFECT)

        if not self.lifxwhite:
            features |= SUPPORT_RGB_COLOR | SUPPORT_XY_COLOR

        return features

    @property
    def effect_list(self):
        """Return the list of supported effects for this light."""
        if self.lifxwhite:
            return [
                SERVICE_EFFECT_PULSE,
                SERVICE_EFFECT_STOP,
            ]

        return [
            SERVICE_EFFECT_COLORLOOP,
            SERVICE_EFFECT_PULSE,
            SERVICE_EFFECT_STOP,
        ]

    @asyncio.coroutine
    def update_after_transition(self, now):
        """Request new status after completion of the last transition."""
        self.postponed_update = None
        yield from self.async_update()
        yield from self.async_update_ha_state()

    def update_later(self, when):
        """Schedule an update requests when a transition is over."""
        if self.postponed_update:
            self.postponed_update()
            self.postponed_update = None
        if when > 0:
            self.postponed_update = async_track_point_in_utc_time(
                self.hass, self.update_after_transition,
                util.dt.utcnow() + timedelta(milliseconds=when))

    @asyncio.coroutine
    def async_turn_on(self, **kwargs):
        """Turn the device on."""
        kwargs[ATTR_POWER] = True
        yield from self.async_set_state(**kwargs)

    @asyncio.coroutine
    def async_turn_off(self, **kwargs):
        """Turn the device off."""
        kwargs[ATTR_POWER] = False
        yield from self.async_set_state(**kwargs)

    @asyncio.coroutine
    def async_set_state(self, **kwargs):
        """Set a color on the light and turn it on/off."""
        yield from self.effects_conductor.stop([self.device])

        if ATTR_EFFECT in kwargs:
            yield from self.default_effect(**kwargs)
            return

        if ATTR_INFRARED in kwargs:
            self.device.set_infrared(convert_8_to_16(kwargs[ATTR_INFRARED]))

        if ATTR_TRANSITION in kwargs:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = 0

        # These are both False if ATTR_POWER is not set
        power_on = kwargs.get(ATTR_POWER, False)
        power_off = not kwargs.get(ATTR_POWER, True)

        hsbk = merge_hsbk(self.device.color, find_hsbk(**kwargs))

        # Send messages, waiting for ACK each time
        ack = AwaitAioLIFX(self).wait
        bulb = self.device

        if not self.is_on:
            if power_off:
                yield from ack(partial(bulb.set_power, False))
            if hsbk:
                yield from ack(partial(bulb.set_color, hsbk))
            if power_on:
                yield from ack(partial(bulb.set_power, True, duration=fade))
        else:
            if power_on:
                yield from ack(partial(bulb.set_power, True))
            if hsbk:
                yield from ack(partial(bulb.set_color, hsbk, duration=fade))
            if power_off:
                yield from ack(partial(bulb.set_power, False, duration=fade))

        # Avoid state ping-pong by holding off updates while the state settles
        yield from asyncio.sleep(0.25)

        # Schedule an update when the transition is complete
        self.update_later(fade)

    @asyncio.coroutine
    def default_effect(self, **kwargs):
        """Start an effect with default parameters."""
        service = kwargs[ATTR_EFFECT]
        data = {
            ATTR_ENTITY_ID: self.entity_id,
        }
        yield from self.hass.services.async_call(DOMAIN, service, data)

    @asyncio.coroutine
    def async_update(self):
        """Update bulb status."""
        _LOGGER.debug("%s async_update", self.who)
        if self.available:
            yield from AwaitAioLIFX(self).wait(self.device.get_color)
