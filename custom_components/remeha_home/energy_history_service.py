"""remeha_home.get_energy_history: read-only daily energy for a date range.

Ported from hugo-leij/remeha_home (energy_history_service.py). Nothing is
written to entities or statistics; the result is only the service response.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError, ClientResponseError
import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .energy_history import combine_history, history_batches, last_complete_day

_LOGGER = logging.getLogger(__name__)

SERVICE_GET_ENERGY_HISTORY = "get_energy_history"
ATTR_DEVICE_ID = "device_id"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_INCLUDE_DAYS = "include_days"

SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_INCLUDE_DAYS, default=True): cv.boolean,
    }
)


def _target(hass: HomeAssistant, device_id: str | None) -> tuple[str, dict]:
    """Return (appliance_id, entry data) for the device, or the only appliance."""
    found = []
    for data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(data, dict) or "coordinator" not in data:
            continue
        coordinator = data["coordinator"]
        for appliance in (coordinator.data or {}).get("appliances", []):
            appliance_id = appliance["applianceId"]
            if device_id is None or coordinator.appliance_device_ids.get(appliance_id) == device_id:
                found.append((appliance_id, data))
    if not found:
        raise ServiceValidationError("No Remeha appliance matches the device")
    if len(found) > 1:
        raise ServiceValidationError("Several Remeha appliances: choose one with device_id")
    return found[0]


def async_register_energy_history_service(hass: HomeAssistant) -> None:
    """Register the service once for the domain."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_ENERGY_HISTORY):
        return

    async def async_get_energy_history(call: ServiceCall) -> ServiceResponse:
        start = call.data[ATTR_START_DATE]
        last_day = last_complete_day(dt_util.now())
        if start > last_day:
            raise ServiceValidationError(
                f"The start date must be {last_day.isoformat()} or earlier: the "
                "cloud only has complete days up to then"
            )
        end = min(call.data[ATTR_END_DATE], last_day)
        try:
            batches = list(history_batches(start, end))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        appliance_id, data = _target(hass, call.data.get(ATTR_DEVICE_ID))
        responses = []
        for batch_start, batch_end in batches:
            try:
                async with asyncio.timeout(30):
                    responses.append(
                        await data["api"].async_get_consumption_data_for_range(
                            appliance_id, batch_start, batch_end
                        )
                    )
            except (ClientResponseError, ClientError, TimeoutError) as err:
                _LOGGER.warning("Energy history request failed: %s", err)
                raise HomeAssistantError(
                    "Remeha Home could not get the energy history; try again later"
                ) from err

        try:
            result = combine_history(start, end, responses)
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Unexpected energy history response: {err}") from err
        if not call.data[ATTR_INCLUDE_DAYS]:
            result.pop("days")
        return {"appliance_id": appliance_id, **result}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ENERGY_HISTORY,
        async_get_energy_history,
        schema=SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
