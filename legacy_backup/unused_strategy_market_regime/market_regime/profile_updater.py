from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .profile_builder import ProfileBuildResult, ProfileBuilder


@dataclass(slots=True)
class ProfileUpdateResult:
    refreshed_symbols: list[str]
    skipped_symbols: dict[str, str]
    window_start: datetime
    window_end: datetime


class ProfileUpdater:
    def __init__(self, builder: ProfileBuilder) -> None:
        self.builder = builder

    def refresh_symbols(
        self,
        symbols: list[str],
        *,
        end_time: datetime | None = None,
        rolling_days: int | None = None,
        write_history: bool = True,
    ) -> ProfileUpdateResult:
        result = self.builder.build_and_persist(
            symbols=symbols,
            end_time=end_time,
            rolling_days=rolling_days,
            write_history=write_history,
        )
        return self._to_update_result(result)

    def refresh_all_active_symbols(
        self,
        *,
        end_time: datetime | None = None,
        rolling_days: int | None = None,
        write_history: bool = True,
    ) -> ProfileUpdateResult:
        result = self.builder.build_and_persist(
            symbols=None,
            end_time=end_time,
            rolling_days=rolling_days,
            write_history=write_history,
        )
        return self._to_update_result(result)

    @staticmethod
    def _to_update_result(result: ProfileBuildResult) -> ProfileUpdateResult:
        return ProfileUpdateResult(
            refreshed_symbols=sorted(result.profiles.keys()),
            skipped_symbols=dict(result.skipped_symbols),
            window_start=result.window_start,
            window_end=result.window_end,
        )
