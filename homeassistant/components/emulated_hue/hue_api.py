"""Provides a Hue API to control Home Assistant."""
import asyncio
import logging

from aiohttp import web
from homeassistant import core
from homeassistant.components.fan import (
    ATTR_SPEED, SPEED_HIGH, SPEED_LOW, SPEED_MEDIUM, SPEED_OFF,
    SUPPORT_SET_SPEED)
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_TRANSITION, SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR, SUPPORT_TRANSITION)
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL, SUPPORT_VOLUME_SET)
from homeassistant.const import (
    ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, HTTP_BAD_REQUEST, HTTP_NOT_FOUND,
    SERVICE_CLOSE_COVER, SERVICE_OPEN_COVER, SERVICE_TURN_OFF, SERVICE_TURN_ON,
    SERVICE_VOLUME_SET, STATE_OFF, STATE_ON)
import homeassistant.util.color as color_util

_LOGGER = logging.getLogger(__name__)

HUE_API_STATE_ON = 'on'
HUE_API_STATE_BRI = 'bri'
HUE_API_STATE_HUE = 'hue'
HUE_API_STATE_SAT = 'sat'
HUE_API_STATE_TIME = 'transitiontime'

HUE_API_STATE_HUE_MAX = 65535
HUE_API_STATE_SAT_MAX = 254

STATE_BRIGHTNESS = HUE_API_STATE_BRI
STATE_HUE = HUE_API_STATE_HUE
STATE_SATURATION = HUE_API_STATE_SAT
STATE_TIME = HUE_API_STATE_TIME


class HueUsernameView(HomeAssistantView):
    """Handle requests to create a username for the emulated hue bridge."""

    url = '/api'
    name = 'emulated_hue:api:create_username'
    extra_urls = ['/api/']
    requires_auth = False

    @asyncio.coroutine
    def post(self, request):
        """Handle a POST request."""
        try:
            data = yield from request.json()
        except ValueError:
            return self.json_message('Invalid JSON', HTTP_BAD_REQUEST)

        if 'devicetype' not in data:
            return self.json_message('devicetype not specified',
                                     HTTP_BAD_REQUEST)

        return self.json([{'success': {'username': '12345678901234567890'}}])


class HueGroupView(HomeAssistantView):
    """Group handler to get Logitech Pop working."""

    url = '/api/{username}/groups/0/action'
    name = 'emulated_hue:groups:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @core.callback
    def put(self, request, username):
        """Process a request to make the Logitech Pop working."""
        return self.json([{
            'error': {
                'address': '/groups/0/action/scene',
                'type': 7,
                'description': 'invalid value, dummy for parameter, scene'
            }
        }])


class HueAllLightsStateView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights'
    name = 'emulated_hue:lights:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @core.callback
    def get(self, request, username):
        """Process a request to get the list of available lights."""
        hass = request.app['hass']
        json_response = {}

        for entity in hass.states.async_all():
            if self.config.is_entity_exposed(entity):
                state = get_entity_state(self.config, entity)

                number = self.config.entity_id_to_number(entity.entity_id)
                json_response[number] = entity_to_json(self.config,
                                                       entity, state)

        return self.json(json_response)


class HueOneLightStateView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights/{entity_id}'
    name = 'emulated_hue:light:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @core.callback
    def get(self, request, username, entity_id):
        """Process a request to get the state of an individual light."""
        hass = request.app['hass']
        entity_id = self.config.number_to_entity_id(entity_id)
        entity = hass.states.get(entity_id)

        if entity is None:
            _LOGGER.error('Entity not found: %s', entity_id)
            return web.Response(text="Entity not found", status=404)

        if not self.config.is_entity_exposed(entity):
            _LOGGER.error('Entity not exposed: %s', entity_id)
            return web.Response(text="Entity not exposed", status=404)

        state = get_entity_state(self.config, entity)

        json_response = entity_to_json(self.config, entity, state)

        return self.json(json_response)


class HueOneLightChangeView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights/{entity_number}/state'
    name = 'emulated_hue:light:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @asyncio.coroutine
    def put(self, request, username, entity_number):
        """Process a request to set the state of an individual light."""
        config = self.config
        hass = request.app['hass']
        entity_id = config.number_to_entity_id(entity_number)

        if entity_id is None:
            _LOGGER.error('Unknown entity number: %s', entity_number)
            return self.json_message('Entity not found', HTTP_NOT_FOUND)

        entity = hass.states.get(entity_id)

        if entity is None:
            _LOGGER.error('Entity not found: %s', entity_id)
            return self.json_message('Entity not found', HTTP_NOT_FOUND)

        if not config.is_entity_exposed(entity):
            _LOGGER.error('Entity not exposed: %s', entity_id)
            return web.Response(text="Entity not exposed", status=404)

        try:
            request_json = yield from request.json()
        except ValueError:
            _LOGGER.error('Received invalid json')
            return self.json_message('Invalid JSON', HTTP_BAD_REQUEST)

        # Parse the request into requested "on" status and brightness
        parsed = parse_hue_api_put_light_body(request_json, entity)

        if parsed is None:
            _LOGGER.error('Unable to parse data: %s', request_json)
            return web.Response(text="Bad request", status=400)

        # Choose general HA domain
        domain = core.DOMAIN

        # Entity needs separate call to turn on
        turn_on_needed = False

        # Convert the resulting "on" status into the service we need to call
        service = SERVICE_TURN_ON if parsed[STATE_ON] else SERVICE_TURN_OFF

        # Construct what we need to send to the service
        data = {ATTR_ENTITY_ID: entity_id}

        # Make sure the entity actually supports brightness
        entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

        if entity.domain == "light":
            if parsed[STATE_ON]:
                if entity_features & SUPPORT_BRIGHTNESS:
                    if parsed[STATE_BRIGHTNESS] is not None:
                        data[ATTR_BRIGHTNESS] = parsed[STATE_BRIGHTNESS]
                if entity_features & SUPPORT_COLOR:
                    if parsed[STATE_HUE] is not None:
                        sat = parsed[STATE_SATURATION] if parsed[STATE_SATURATION] else 0
                        hue = parsed[STATE_HUE]

                        # Convert hs values to hass hs values
                        sat = int((sat / HUE_API_STATE_SAT_MAX) * 100)
                        hue = int((hue / HUE_API_STATE_HUE_MAX) * 360)
                        rgb = color_util.color_hs_to_RGB(hue, sat)
                        _LOGGER.info("hue: %d, sat: %d -> rgb: %s" % (hue, sat, repr(rgb)))
                        data[ATTR_RGB_COLOR] = rgb
            if entity_features & SUPPORT_TRANSITION:
                if parsed[STATE_TIME] is not None:
                    # Convert STATE_TIME (multiples of 100ms) to ATTR_TRANSITION (seconds)
                    data[ATTR_TRANSITION] = parsed[STATE_TIME] / 10

        # If the requested entity is a script add some variables
        elif entity.domain == "script":
            data['variables'] = {
                'requested_state': STATE_ON if parsed[STATE_ON] else STATE_OFF
            }

            if parsed[STATE_BRIGHTNESS] is not None:
                data['variables']['requested_level'] = parsed[STATE_BRIGHTNESS]

        # If the requested entity is a media player, convert to volume
        elif entity.domain == "media_player":
            if entity_features & SUPPORT_VOLUME_SET:
                if parsed[STATE_BRIGHTNESS] is not None:
                    turn_on_needed = True
                    domain = entity.domain
                    service = SERVICE_VOLUME_SET
                    # Convert 0-100 to 0.0-1.0
                    data[ATTR_MEDIA_VOLUME_LEVEL] = \
                        parsed[STATE_BRIGHTNESS] / 100.0

        # If the requested entity is a cover, convert to open_cover/close_cover
        elif entity.domain == "cover":
            domain = entity.domain
            if service == SERVICE_TURN_ON:
                service = SERVICE_OPEN_COVER
            else:
                service = SERVICE_CLOSE_COVER

        # If the requested entity is a fan, convert to speed
        elif entity.domain == "fan":
            if entity_features & SUPPORT_SET_SPEED:
                if parsed[STATE_BRIGHTNESS] is not None:
                    domain = entity.domain
                    # Convert 0-100 to a fan speed
                    brightness = parsed[STATE_BRIGHTNESS]
                    if brightness == 0:
                        data[ATTR_SPEED] = SPEED_OFF
                    elif brightness <= 33.3 and brightness > 0:
                        data[ATTR_SPEED] = SPEED_LOW
                    elif brightness <= 66.6 and brightness > 33.3:
                        data[ATTR_SPEED] = SPEED_MEDIUM
                    elif brightness <= 100 and brightness > 66.6:
                        data[ATTR_SPEED] = SPEED_HIGH

        if entity.domain in config.off_maps_to_on_domains:
            # Map the off command to on
            service = SERVICE_TURN_ON

            # Caching is required because things like scripts and scenes won't
            # report as "off" to Alexa if an "off" command is received, because
            # they'll map to "on". Thus, instead of reporting its actual
            # status, we report what Alexa will want to see, which is the same
            # as the actual requested command.
            config.cached_states[entity_id] = parsed

        # Separate call to turn on needed
        if turn_on_needed:
            hass.async_add_job(hass.services.async_call(
                core.DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id},
                blocking=True))

        hass.async_add_job(hass.services.async_call(
            domain, service, data, blocking=True))

        json_response = \
            [create_hue_success_response(entity_id, HUE_API_STATE_ON, parsed[STATE_ON])]

        if parsed[STATE_BRIGHTNESS] is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_BRI, parsed[STATE_BRIGHTNESS]))
        if parsed[STATE_HUE] is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_HUE, parsed[STATE_HUE]))
        if parsed[STATE_SATURATION] is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_SAT, parsed[STATE_SATURATION]))
        if parsed[STATE_TIME] is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_TIME, parsed[STATE_TIME]))

        return self.json(json_response)


def parse_hue_api_put_light_body(request_json, entity):
    data = {
        STATE_BRIGHTNESS: None,
        STATE_HUE: None,
        STATE_ON: False,
        STATE_TIME: None,
        STATE_SATURATION: None,
    }

    # Make sure the entity actually supports brightness
    entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

    _LOGGER.info("%s" % repr(request_json))

    """Parse the body of a request to change the state of a light."""
    if HUE_API_STATE_ON in request_json:
        if not isinstance(request_json[HUE_API_STATE_ON], bool):
            return None

        if request_json[HUE_API_STATE_ON]:
            # Echo requested device be turned on
            data[STATE_BRIGHTNESS] = None
            data[STATE_ON] = True
        else:
            # Echo requested device be turned off
            data[STATE_BRIGHTNESS] = None
            data[STATE_ON] = False

    if HUE_API_STATE_TIME in request_json:
        try:
            # Clamp brightness from 0 to 10
            data[STATE_TIME] = \
                max(0, min(int(request_json[HUE_API_STATE_TIME]), 10))
        except ValueError:
            return None

    if HUE_API_STATE_HUE in request_json:
        try:
            # Clamp brightness from 0 to 65535
            data[STATE_HUE] = \
                max(0, min(int(request_json[HUE_API_STATE_HUE]),
                    HUE_API_STATE_HUE_MAX))
        except ValueError:
            return None

    if HUE_API_STATE_SAT in request_json:
        try:
            # Clamp brightness from 0 to 254
            data[STATE_SATURATION] = \
                max(0, min(int(request_json[HUE_API_STATE_SAT]),
                    HUE_API_STATE_SAT_MAX))
        except ValueError:
            return None

    if HUE_API_STATE_BRI in request_json:
        try:
            # Clamp brightness from 0 to 255
            data[STATE_BRIGHTNESS] = \
                max(0, min(int(request_json[HUE_API_STATE_BRI]), 255))
        except ValueError:
            return None

        if entity.domain == "light":
            data[STATE_ON] = (data[STATE_BRIGHTNESS] > 0)
            if (entity_features & SUPPORT_BRIGHTNESS) == False:
                data[STATE_BRIGHTNESS] = None

        elif entity.domain == "scene":
            data[STATE_BRIGHTNESS] = None
            data[STATE_ON] = True

        elif (entity.domain == "script" or
              entity.domain == "media_player" or
              entity.domain == "fan"):
            # Convert 0-255 to 0-100
            level = data[STATE_BRIGHTNESS] / 255 * 100
            data[STATE_BRIGHTNESS] = round(level)
            data[STATE_ON] = True

    return data


def get_entity_state(config, entity):
    """Retrieve and convert state and brightness values for an entity."""
    cached_state = config.cached_states.get(entity.entity_id, None)
    data = {
        STATE_BRIGHTNESS: None,
        STATE_HUE: None,
        STATE_ON: False,
        STATE_TIME: None,
        STATE_SATURATION: None
    }

    if cached_state is None:
        data[STATE_ON] = entity.state != STATE_OFF
        data[STATE_BRIGHTNESS] = entity.attributes.get(
            ATTR_BRIGHTNESS, 255 if data[STATE_ON] else 0)
        rgb = entity.attributes.get(
            ATTR_RGB_COLOR, (255, 255, 255) if data[STATE_ON] else None)
        if rgb is not None:
            (hue, sat) = color_util.color_RGB_to_hs(*rgb)
            # convert hass hs values back to hue hs values
            data[STATE_HUE] = int((hue / 360) * HUE_API_STATE_HUE_MAX)
            data[STATE_SATURATION] = int((sat / 100) * HUE_API_STATE_SAT_MAX)

        data[STATE_TIME] = entity.attributes.get(ATTR_TRANSITION, 0)

        # Make sure the entity actually supports brightness
        entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

        if entity.domain == "light":
            if entity_features & SUPPORT_BRIGHTNESS:
                pass

        elif entity.domain == "media_player":
            level = entity.attributes.get(
                ATTR_MEDIA_VOLUME_LEVEL, 1.0 if data[STATE_ON] else 0.0)
            # Convert 0.0-1.0 to 0-255
            data[STATE_BRIGHTNESS] = round(min(1.0, level) * 255)
        elif entity.domain == "fan":
            speed = entity.attributes.get(ATTR_SPEED, 0)
            # Convert 0.0-1.0 to 0-255
            data[STATE_BRIGHTNESS] = 0
            if speed == SPEED_LOW:
                data[STATE_BRIGHTNESS] = 85
            elif speed == SPEED_MEDIUM:
                data[STATE_BRIGHTNESS] = 170
            elif speed == SPEED_HIGH:
                data[STATE_BRIGHTNESS] = 255
    else:
        data = cached_state
        # Make sure brightness is valid
        if data[STATE_BRIGHTNESS] is None:
            data[STATE_BRIGHTNESS] = 255 if data[STATE_ON] else 0
        # Make sure hue/saturation are valid
        if data[STATE_HUE] is None:
            data[STATE_HUE] = 0
        if data[STATE_SATURATION] is None:
            data[STATE_SATURATION] is 0

        # If the light is off, set the color to off
        if data[STATE_BRIGHTNESS] == 0:
            data[STATE_HUE] = 0
            data[STATE_SATURATION] = 0

    return data


def entity_to_json(config, entity, state):
    """Convert an entity to its Hue bridge JSON representation."""
    return {
        'state':
        {
            HUE_API_STATE_ON: state[STATE_ON],
            HUE_API_STATE_BRI: state[STATE_BRIGHTNESS],
            HUE_API_STATE_HUE: state[STATE_HUE],
            HUE_API_STATE_SAT: state[STATE_SATURATION],
            HUE_API_STATE_TIME: state[STATE_TIME],
            'reachable': True
        },
        'type': 'Dimmable light',
        'name': config.get_entity_name(entity),
        'modelid': 'HASS123',
        'uniqueid': entity.entity_id,
        'swversion': '123'
    }


def create_hue_success_response(entity_id, attr, value):
    """Create a success response for an attribute set on a light."""
    success_key = '/lights/{}/state/{}'.format(entity_id, attr)
    return {'success': {success_key: value}}
