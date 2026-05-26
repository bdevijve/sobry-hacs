from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SobryApiClient, SobryAuthError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Only keep today and tomorrow in the cache.
# Past days are useless: sensors only display slots for the current day.
_CACHE_MAX_DAYS = 2


class SobryContractCoordinator(DataUpdateCoordinator[dict[int, dict]]):
    """Price data coordinator for a single Sobry electricity contract.

    Overview
    --------
    Prices change once a day and are served in 15-min slots. The coordinator
    caches them keyed by Unix timestamp of each slot's start time and relies
    on two mechanisms to stay current:

    - HA polling every 15 min (update_interval) — triggers sensor recalculation
      at each slot boundary; the cache absorbs all calls except the first of
      the day, so no redundant network traffic.
    - A single time trigger at 14:00 — pre-fetches tomorrow's prices, which
      Sobry publishes around 13:30. The midnight rollover is handled naturally:
      at 00:00 the date changes, the next poll finds a cache miss, and today's
      prices are fetched automatically.

    The cache is a flat dict { slot_start_timestamp -> slot_data }.
    Loaded days are tracked in _loaded_days so that an empty API response
    (prices not yet published) does not cause an infinite retry loop.

    Example slot: {"time": "14:00", "price": 0.1842, "color": "green", "colorLabel": "Off-peak"}
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SobryApiClient,
        token: str,
        contract: dict,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{contract['id']}")
        self._entry = entry
        self._client = client
        self._token = token
        self.contract = contract
        self._price_cache: dict[int, dict] = {}
        # Track which days have been loaded so that an empty API response
        # (prices not yet published) does not cause an infinite retry loop.
        self._loaded_days: set[str] = set()

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[int, dict]:
        """Load today's prices if not cached, then return the full cache.

        Called by HA every 15 min. The API is only queried on a cache miss.
        When the token expires (SobryAuthError), UpdateFailed is raised so
        that HA marks the entry as unavailable and logs the error.
        """
        today = date.today().isoformat()
        if self._is_stale(today):
            try:
                slots = await self._client.get_daily_prices(
                    self._token, self.contract["id"], today
                )
                self._set_cache(today, slots)
            except SobryAuthError as err:
                raise UpdateFailed(str(err)) from err

        # Refresh dashboard data (monthly consumption, subscribed power) on
        # every poll so that those sensors stay up-to-date.
        try:
            dashboard = await self._client.get_dashboard(
                self._token, self.contract["id"]
            )
            self.contract["meter"] = dashboard.get("meter", {})
            self.contract["consumption"] = dashboard.get("consumption", {})
        except SobryAuthError as err:
            raise UpdateFailed(str(err)) from err

        # Clean up on every poll so past days do not accumulate in memory.
        self._purge_old_cache()
        return self._price_cache

    async def async_setup(self) -> None:
        """Perform the initial data fetch and register time triggers."""
        await self.async_refresh()

        if dt_util.now().hour >= 14:
            await self._fetch_tomorrow()

        self._entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_slot_boundary,
                minute=[0, 15, 30, 45],
                second=0,
            )
        )

        self._entry.async_on_unload(
            async_track_time_change(
                self.hass, self._handle_fetch_tomorrow, hour=14, minute=0, second=0
            )
        )

    # ------------------------------------------------------------------
    # Time-triggered callbacks
    # ------------------------------------------------------------------

    async def _handle_slot_boundary(self, _) -> None:
        """Trigger a coordinator refresh at each 15-min slot boundary."""
        await self.async_refresh()

    @callback
    def _handle_fetch_tomorrow(self, _) -> None:
        """Trigger a pre-fetch of tomorrow's prices at 14:00."""
        self.hass.async_create_task(self._fetch_tomorrow())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_tomorrow(self) -> None:
        """Fetch and cache tomorrow's price slots.

        On failure we log a warning without raising: pre-fetching is a bonus,
        not a requirement. Sensors will keep displaying today's prices normally.
        """
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        if not self._is_stale(tomorrow):
            return
        try:
            slots = await self._client.get_daily_prices(
                self._token, self.contract["id"], tomorrow
            )
            self._set_cache(tomorrow, slots)
        except SobryAuthError:
            _LOGGER.warning("Failed to pre-fetch prices for %s", tomorrow)

    @staticmethod
    def _day_ts(day: str, slot_time: str) -> int:
        """Return the Unix timestamp for a given ISO date and HH:MM slot time.

        Uses the Home Assistant local timezone so that timestamps match the
        user's local time regardless of the server's system timezone (which
        is often UTC inside Docker containers).
        """
        hour: int = int(slot_time.split(":")[0])
        minute: int = int(slot_time.split(":")[1])
        tz = dt_util.DEFAULT_TIME_ZONE
        local_dt = datetime(
            *date.fromisoformat(day).timetuple()[:3],
            hour,
            minute,
            tzinfo=tz,
        )
        return int(local_dt.timestamp())

    def _is_stale(self, day: str) -> bool:
        """Return True if the given day has not been loaded into the cache yet.

        Uses a dedicated set rather than checking for a sentinel slot (00:00)
        so that an empty API response does not cause an infinite retry loop.
        """
        return day not in self._loaded_days

    def _set_cache(self, day: str, slots: list) -> None:
        """Store each slot keyed by its start timestamp and mark the day as loaded."""
        for slot in slots:
            self._price_cache[SobryContractCoordinator._day_ts(day, slot["time"])] = slot
        # Mark the day as loaded even when slots is empty (prices not yet
        # published) so we do not retry until the next scheduled trigger.
        self._loaded_days.add(day)

    def _purge_old_cache(self) -> None:
        """Remove cache entries and day markers older than _CACHE_MAX_DAYS days."""
        cutoff_day = (date.today() - timedelta(days=_CACHE_MAX_DAYS - 1)).isoformat()
        cutoff_ts = SobryContractCoordinator._day_ts(cutoff_day, "00:00")
        for ts in [ts for ts in self._price_cache if ts < cutoff_ts]:
            del self._price_cache[ts]
        self._loaded_days = {d for d in self._loaded_days if d >= cutoff_day}
