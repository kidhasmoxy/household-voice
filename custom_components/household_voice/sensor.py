from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SchedulesSensor(hass.data[DOMAIN][entry.entry_id], entry.entry_id)])


class SchedulesSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "Household Voice Schedules"
    _attr_icon = "mdi:timer-outline"
    _unrecorded_attributes = frozenset({"schedules", "events","assistants","area_pairings"})

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = entry_id + "_schedules"

    @property
    def native_value(self):
        return sum(row.get("state") in ("active", "paused", "ringing") for row in self.coordinator.data["schedules"])

    @property
    def extra_state_attributes(self):
        worker=getattr(self.coordinator,'ring_worker',None)
        return {"config_entry_id": self._entry_id, "mode": self.coordinator.data.get("mode", "unknown"),
                "ring_execution_enabled": self.coordinator.data.get("ring_execution_enabled",False),
                "ring_worker_enabled":bool(worker and worker.enabled()),
                "ring_worker_last_success":getattr(worker,'last_success',None),
                "ring_worker_error":getattr(worker,'last_error',None),
                "maintenance_error":self.coordinator.data.get("maintenance", {}).get("error"),
                "maintenance_healthy":self.coordinator.data.get("maintenance", {}).get("healthy"),
                "maintenance_last_success":self.coordinator.data.get("maintenance", {}).get("last_success"),
                "rooms": self.coordinator.data.get("rooms", []),
                "assistants":self.coordinator.data.get('assistants',[]),
                "area_pairings":self.coordinator.data.get('area_pairings',{}),
                "schedules": self.coordinator.data["schedules"], "events": self.coordinator.data["events"][-100:],
                "event_count": len(self.coordinator.data["events"])}
