"""Platform for retrieving meteorological data from Environment Canada."""
import datetime
import logging
import re

import voluptuous as vol

from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_CLOUDY,
    ATTR_CONDITION_FOG,
    ATTR_CONDITION_HAIL,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_PARTLYCLOUDY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_SUNNY,
    ATTR_CONDITION_WINDY,
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_PRECIPITATION_PROBABILITY,
    ATTR_FORECAST_TEMP,
    ATTR_FORECAST_TEMP_LOW,
    ATTR_FORECAST_TIME,
    PLATFORM_SCHEMA,
    WeatherEntity,
)
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, TEMP_CELSIUS
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt

from . import trigger_import
from .const import CONF_ATTRIBUTION, CONF_STATION, DOMAIN

CONF_FORECAST = "forecast"

_LOGGER = logging.getLogger(__name__)


def validate_station(station):
    """Check that the station ID is well-formed."""
    if station is None:
        return None
    if not re.fullmatch(r"[A-Z]{2}/s0000\d{3}", station):
        raise vol.Invalid('Station ID must be of the form "XX/s0000###"')
    return station


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_STATION): validate_station,
        vol.Inclusive(CONF_LATITUDE, "latlon"): cv.latitude,
        vol.Inclusive(CONF_LONGITUDE, "latlon"): cv.longitude,
        vol.Optional(CONF_FORECAST, default="daily"): vol.In(["daily", "hourly"]),
    }
)

# Icon codes from http://dd.weatheroffice.ec.gc.ca/citypage_weather/
# docs/current_conditions_icon_code_descriptions_e.csv
ICON_CONDITION_MAP = {
    ATTR_CONDITION_SUNNY: [0, 1],
    ATTR_CONDITION_CLEAR_NIGHT: [30, 31],
    ATTR_CONDITION_PARTLYCLOUDY: [2, 3, 4, 5, 22, 32, 33, 34, 35],
    ATTR_CONDITION_CLOUDY: [10],
    ATTR_CONDITION_RAINY: [6, 9, 11, 12, 28, 36],
    ATTR_CONDITION_LIGHTNING_RAINY: [19, 39, 46, 47],
    ATTR_CONDITION_POURING: [13],
    ATTR_CONDITION_SNOWY_RAINY: [7, 14, 15, 27, 37],
    ATTR_CONDITION_SNOWY: [8, 16, 17, 18, 25, 26, 38, 40],
    ATTR_CONDITION_WINDY: [43],
    ATTR_CONDITION_FOG: [20, 21, 23, 24, 44],
    ATTR_CONDITION_HAIL: [26, 27],
}


async def async_setup_platform(hass, config, async_add_entries, discovery_info=None):
    """Set up the Environment Canada weather."""
    trigger_import(hass, config)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add a weather entity from a config_entry."""
    weather_data = hass.data[DOMAIN][config_entry.entry_id]["weather_data"]

    async_add_entities(
        [
            ECWeather(
                weather_data,
                f"{config_entry.title}",
                config_entry.data,
                "daily",
                f"{config_entry.unique_id}-daily",
            ),
            ECWeather(
                weather_data,
                f"{config_entry.title} Hourly",
                config_entry.data,
                "hourly",
                f"{config_entry.unique_id}-hourly",
            ),
        ]
    )


class ECWeather(WeatherEntity):
    """Representation of a weather condition."""

    def __init__(self, ec_data, name, config, forecast_type, unique_id):
        """Initialize Environment Canada weather."""
        self.ec_data = ec_data
        self.config = config
        self._attr_name = name
        self._attr_unique_id = unique_id
        self.forecast_type = forecast_type

    @property
    def attribution(self):
        """Return the attribution."""
        return CONF_ATTRIBUTION

    @property
    def temperature(self):
        """Return the temperature."""
        if self.ec_data.conditions.get("temperature", {}).get("value"):
            return float(self.ec_data.conditions["temperature"]["value"])
        if self.ec_data.hourly_forecasts and self.ec_data.hourly_forecasts[0].get(
            "temperature"
        ):
            return float(self.ec_data.hourly_forecasts[0]["temperature"])
        return None

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return TEMP_CELSIUS

    @property
    def humidity(self):
        """Return the humidity."""
        if self.ec_data.conditions.get("humidity", {}).get("value"):
            return float(self.ec_data.conditions["humidity"]["value"])
        return None

    @property
    def wind_speed(self):
        """Return the wind speed."""
        if self.ec_data.conditions.get("wind_speed", {}).get("value"):
            return float(self.ec_data.conditions["wind_speed"]["value"])
        return None

    @property
    def wind_bearing(self):
        """Return the wind bearing."""
        if self.ec_data.conditions.get("wind_bearing", {}).get("value"):
            return float(self.ec_data.conditions["wind_bearing"]["value"])
        return None

    @property
    def pressure(self):
        """Return the pressure."""
        if self.ec_data.conditions.get("pressure", {}).get("value"):
            return 10 * float(self.ec_data.conditions["pressure"]["value"])
        return None

    @property
    def visibility(self):
        """Return the visibility."""
        if self.ec_data.conditions.get("visibility", {}).get("value"):
            return float(self.ec_data.conditions["visibility"]["value"])
        return None

    @property
    def condition(self):
        """Return the weather condition."""
        icon_code = None

        if self.ec_data.conditions.get("icon_code", {}).get("value"):
            icon_code = self.ec_data.conditions["icon_code"]["value"]
        elif self.ec_data.hourly_forecasts and self.ec_data.hourly_forecasts[0].get(
            "icon_code"
        ):
            icon_code = self.ec_data.hourly_forecasts[0]["icon_code"]

        if icon_code:
            return icon_code_to_condition(int(icon_code))
        return ""

    @property
    def forecast(self):
        """Return the forecast array."""
        return get_forecast(self.ec_data, self.forecast_type)

    def update(self):
        """Get the latest data from Environment Canada."""
        self.ec_data.update()


def get_forecast(ec_data, forecast_type):
    """Build the forecast array."""
    forecast_array = []

    if forecast_type == "daily":
        half_days = ec_data.daily_forecasts
        if not half_days:
            return None

        today = {
            ATTR_FORECAST_TIME: dt.now().isoformat(),
            ATTR_FORECAST_CONDITION: icon_code_to_condition(
                int(half_days[0]["icon_code"])
            ),
            ATTR_FORECAST_PRECIPITATION_PROBABILITY: int(
                half_days[0]["precip_probability"]
            ),
        }

        if half_days[0]["temperature_class"] == "high":
            today.update(
                {
                    ATTR_FORECAST_TEMP: int(half_days[0]["temperature"]),
                    ATTR_FORECAST_TEMP_LOW: int(half_days[1]["temperature"]),
                }
            )
            half_days = half_days[2:]
        else:
            today.update(
                {
                    ATTR_FORECAST_TEMP: None,
                    ATTR_FORECAST_TEMP_LOW: int(half_days[0]["temperature"]),
                }
            )
            half_days = half_days[1:]

        forecast_array.append(today)

        for day, high, low in zip(range(1, 6), range(0, 9, 2), range(1, 10, 2)):
            forecast_array.append(
                {
                    ATTR_FORECAST_TIME: (
                        dt.now() + datetime.timedelta(days=day)
                    ).isoformat(),
                    ATTR_FORECAST_TEMP: int(half_days[high]["temperature"]),
                    ATTR_FORECAST_TEMP_LOW: int(half_days[low]["temperature"]),
                    ATTR_FORECAST_CONDITION: icon_code_to_condition(
                        int(half_days[high]["icon_code"])
                    ),
                    ATTR_FORECAST_PRECIPITATION_PROBABILITY: int(
                        half_days[high]["precip_probability"]
                    ),
                }
            )

    elif forecast_type == "hourly":
        for hour in ec_data.hourly_forecasts:
            forecast_array.append(
                {
                    ATTR_FORECAST_TIME: datetime.datetime.strptime(
                        hour["period"], "%Y%m%d%H%M%S"
                    )
                    .replace(tzinfo=dt.UTC)
                    .isoformat(),
                    ATTR_FORECAST_TEMP: int(hour["temperature"]),
                    ATTR_FORECAST_CONDITION: icon_code_to_condition(
                        int(hour["icon_code"])
                    ),
                    ATTR_FORECAST_PRECIPITATION_PROBABILITY: int(
                        hour["precip_probability"]
                    ),
                }
            )

    return forecast_array


def icon_code_to_condition(icon_code):
    """Return the condition corresponding to an icon code."""
    for condition, codes in ICON_CONDITION_MAP.items():
        if icon_code in codes:
            return condition
    return None
