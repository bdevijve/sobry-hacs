from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SobryApiClient, SobryAuthError
from .const import CONF_TOKEN, DOMAIN
from .coordinator import SobryContractCoordinator

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sobry from a config entry.

    Creates one coordinator per contract, performs the initial price fetch,
    then forwards setup to the sensor platform.

    Raises ConfigEntryNotReady if the API is unreachable at startup so that
    HA retries automatically instead of failing silently.
    """
    hass.data.setdefault(DOMAIN, {})

    client = SobryApiClient(async_get_clientsession(hass))

    try:
        contracts = await client.get_contracts(entry.data[CONF_TOKEN])
    except SobryAuthError as err:
        raise ConfigEntryNotReady(f"Sobry authentication failed: {err}") from err
    except aiohttp.ClientError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Sobry API: {err}") from err

    coordinators = []
    for contract in contracts:
        try:
            dashboard = await client.get_dashboard(entry.data[CONF_TOKEN], contract["id"])
        except (SobryAuthError, aiohttp.ClientError) as err:
            raise ConfigEntryNotReady(f"Cannot fetch dashboard for contract {contract.get('id')}: {err}") from err

        contract["meter"] = dashboard.get("meter", {})
        contract["consumption"] = dashboard.get("consumption", {})
        coordinator = SobryContractCoordinator(hass, entry, client, entry.data[CONF_TOKEN], contract)
        await coordinator.async_setup()
        coordinators.append(coordinator)

    hass.data[DOMAIN][entry.entry_id] = {
        **entry.data,
        "contracts": contracts,
        "coordinators": coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and clean up its data."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
