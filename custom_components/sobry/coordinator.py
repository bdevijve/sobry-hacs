from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SobryApiClient, SobryAuthError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Only keep today and tomorrow in the cache.
# Past days are useless: sensors only display slots for the current day.
_CACHE_MAX_DAYS = 2

# How long to wait between retry attempts when tomorrow's prices are not yet
# published (Sobry sometimes publishes them with a short delay after 13:00).
_RETRY_DELAY_SECONDS = 5 * 60  # 5 minutes

# Maximum number of automatic retries after the 13:00 trigger.
# 24 retries x 5 min = prices expected by 15:00 at the latest.
_MAX_RETRIES = 24


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

    - A daily trigger at 13:00 — pre-fetches tomorrow's prices, which Sobry
      publishes around that time.  If the response is empty (prices not yet
      published), the coordinator schedules an automatic retry every 5 minutes
      for up to 24 attempts (~15:00 at the latest).  The midnight rollover is
      handled naturally: at 00:00 the date changes, the next poll finds a cache
      miss, and today's prices are fetched automatically.

    The cache is a flat dict { slot_start_timestamp -> slot_data }.
    Loaded days are tracked in _loaded_days so that an empty API response
    (prices not yet published) does not cause an infinite retry loop during
    normal polling.  The retry logic (triggered only from the 13:00 event and
    its follow-up retries) uses a separate counter that resets each day.

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
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{contract['id']}",
        )
        self._entry = entry
        self._client = client
        self._token = token
        self.contract = contract
        self._price_cache: dict[int, dict] = {}
        # Track which days have been loaded so that an empty API response
        # (prices not yet published) does not cause an infinite retry loop.
        self._loaded_days: set[str] = set()
        # Retry counter for the daily 13:00 pre-fetch; reset each calendar day.
        self._retry_count: int = 0
        self._retry_day: str = ""

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

        # If HA starts after 13:00 and tomorrow's prices haven't been fetched
        # yet, do an immediate pre-fetch attempt.
        if dt_util.now().hour >= 13:
            await self._fetch_tomorrow(reset_retries=True)

        # Refresh sensors at every 15-min slot boundary.
        self._entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_slot_boundary,
                minute=[0, 15, 30, 45],
                second=0,
            )
        )

        # Trigger the daily pre-fetch of tomorrow's prices at 13:00.
        self._entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_fetch_tomorrow,
                hour=13,
                minute=0,
                second=0,
            )
        )

    # ------------------------------------------------------------------
    # Time-triggered callbacks
    # ------------------------------------------------------------------

    async def _handle_slot_boundary(self, _now=None) -> None:
        """Trigger a coordinator refresh at each 15-min slot boundary."""
        await self.async_refresh()

    @callback
    def _handle_fetch_tomorrow(self, _now=None) -> None:
        """Trigger a pre-fetch of tomorrow's prices at 13:00."""
        self.hass.async_create_task(self._fetch_tomorrow(reset_retries=True))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_tomorrow(self, *, reset_retries: bool = False) -> None:
        """Fetch and cache tomorrow's price slots, with automatic retry.

        If Sobry has not yet published tomorrow's prices (empty response),
        a retry is scheduled _RETRY_DELAY_SECONDS later, up to _MAX_RETRIES
        times.  Once the prices arrive, no further retries are attempted.

        reset_retries=True must be passed when calling from the 13:00 trigger
        (or on startup) so that the per-day counter is reset correctly.

        On any failure we log a warning without raising: pre-fetching is a
        bonus, not a requirement.  Sensors will keep displaying today's prices.
        """
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        if reset_retries:
            # New trigger for a new day — reset the per-day retry counter.
            self._retry_count = 0
            self._retry_day = tomorrow

        # If we already have prices for tomorrow, nothing to do.
        if not self._is_stale(tomorrow):
            return

        try:
            slots = await self._client.get_daily_prices(
                self._token, self.contract["id"], tomorrow
            )
            if slots:
                # Prices are available — cache them and we're done.
                self._set_cache(tomorrow, slots)
                _LOGGER.debug(
                    "Pre-fetched %d price slots for %s", len(slots), tomorrow
                )
            else:
                # Prices not yet published.  Schedule a retry if we haven't
                # exhausted the retry budget for today.
                if (
                    self._retry_day == tomorrow
                    and self._retry_count < _MAX_RETRIES
                ):
                    self._retry_count += 1
                    _LOGGER.info(
                        "Tomorrow's prices (%s) not yet published; "
                        "retry %d/%d in %d min.",
                        tomorrow,
                        self._retry_count,
                        _MAX_RETRIES,
                        _RETRY_DELAY_SECONDS // 60,
                    )
                    self._entry.async_on_unload(
                        async_call_later(
                            self.hass,
                            _RETRY_DELAY_SECONDS,
                            self._handle_retry_fetch_tomorrow,
                        )
                    )
                else:
                    _LOGGER.warning(
                        "Tomorrow's prices (%s) still unavailable after "
                        "%d retries; giving up until next 13:00 trigger.",
                        tomorrow,
                        _MAX_RETRIES,
                    )
        except SobryAuthError as err:
            _LOGGER.warning("Failed to pre-fetch prices for %s: %s", tomorrow, err)

    @callback
    def _handle_retry_fetch_tomorrow(self, _now=None) -> None:
        """Callback scheduled by async_call_later to retry the pre-fetch."""
        self.hass.async_create_task(self._fetch_tomorrow(reset_retries=False))

    @staticmethod
    def _day_ts(day: str, slot_time: str) -> int:
        """Return the Unix timestamp for a given ISO date and HH:MM slot time.

        Uses the Home Assistant local timezone so that timestamps match the
        user's local time regardless of the server's system timezone (which
        is often UTC inside Docker containers).
        """
        hour = int(int(slot_time.split(":")[0]))
        minute = int(int(slot_time.split(":")[1]))
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
        cutoff_day = (date.today() - timedelta(days=_CACHE_MAX_DAYS)).isoformat()
        cutoff_ts = SobryContractCoordinator._day_ts(cutoff_day, "00:00")
        stale = [ts for ts in self._price_cache if ts < cutoff_ts]
        for ts in stale:
            del self._price_cache[ts]
        self._loaded_days = {d for d in self._loaded_days if d >= cutoff_day}
