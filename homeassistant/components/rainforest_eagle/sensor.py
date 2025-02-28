"""Support for the Rainforest Eagle energy monitor."""
from __future__ import annotations

from homeassistant.components.sensor import (
    DEVICE_CLASS_ENERGY,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    SensorEntity,
    SensorEntityDescription,
    StateType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    DEVICE_CLASS_POWER,
    ENERGY_KILO_WATT_HOUR,
    POWER_KILO_WATT,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .data import EagleDataCoordinator

SENSORS = (
    SensorEntityDescription(
        key="zigbee:InstantaneousDemand",
        # We can drop the "Eagle-200" part of the name in HA 2021.12
        name="Eagle-200 Meter Power Demand",
        native_unit_of_measurement=POWER_KILO_WATT,
        device_class=DEVICE_CLASS_POWER,
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    SensorEntityDescription(
        key="zigbee:CurrentSummationDelivered",
        name="Eagle-200 Total Meter Energy Delivered",
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        device_class=DEVICE_CLASS_ENERGY,
        state_class=STATE_CLASS_TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="zigbee:CurrentSummationReceived",
        name="Eagle-200 Total Meter Energy Received",
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        device_class=DEVICE_CLASS_ENERGY,
        state_class=STATE_CLASS_TOTAL_INCREASING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [EagleSensor(coordinator, description) for description in SENSORS]

    if coordinator.data.get("zigbee:Price") not in (None, "invalid"):
        entities.append(
            EagleSensor(
                coordinator,
                SensorEntityDescription(
                    key="zigbee:Price",
                    name="Meter Price",
                    native_unit_of_measurement=f"{coordinator.data['zigbee:PriceCurrency']}/{ENERGY_KILO_WATT_HOUR}",
                    state_class=STATE_CLASS_MEASUREMENT,
                ),
            )
        )

    async_add_entities(entities)


class EagleSensor(CoordinatorEntity, SensorEntity):
    """Implementation of the Rainforest Eagle sensor."""

    coordinator: EagleDataCoordinator

    def __init__(self, coordinator, entity_description):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = entity_description

    @property
    def unique_id(self) -> str | None:
        """Return unique ID of entity."""
        return f"{self.coordinator.cloud_id}-${self.coordinator.hardware_address}-{self.entity_description.key}"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.coordinator.is_connected

    @property
    def native_value(self) -> StateType:
        """Return native value of the sensor."""
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info."""
        return {
            "name": self.coordinator.model,
            "identifiers": {(DOMAIN, self.coordinator.cloud_id)},
            "manufacturer": "Rainforest Automation",
            "model": self.coordinator.model,
        }
