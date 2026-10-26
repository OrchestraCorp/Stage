"""Application composition; no credentials or business identities enter Core."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings
from app.providers.administration import administration_plugin
from app.providers.assignments import assignments_plugin
from app.providers.identity import identity_plugin
from app.providers.native_auth import native_auth_plugin
from app.providers.storage import REDIS, STORAGE, DependencyHealth, database_plugin, redis_plugin
from app.providers.work import human_work_plugin
from app.runtime import CloseReport, PluginHandle, PluginSpec, RuntimeHost, ServiceKey


class ApplicationRuntime:
    def __init__(
        self, config: Settings, plugins: tuple[PluginSpec, ...] = (), *, optional_plugins: tuple[PluginSpec, ...] = ()
    ) -> None:
        self.config = config
        self.host = RuntimeHost()
        self.database_health = DependencyHealth()
        self.redis_health = DependencyHealth()
        self._plugins = plugins
        self._optional_plugins = optional_plugins
        self._started = False
        self._stopping = False
        self._start_lock = asyncio.Lock()
        self._start_error: BaseException | None = None
        self._required: list[ServiceKey[Any] | PluginHandle] = [STORAGE]
        if config.redis_required:
            self._required.append(REDIS)

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._stopping:
                raise RuntimeError("Stage runtime is stopping")
            if self._start_error is not None:
                raise RuntimeError("Stage runtime startup already failed") from self._start_error
            try:
                self.host.load(database_plugin(self.config, self.database_health))
                if self.config.redis_url is not None:
                    self.host.load(redis_plugin(self.config, self.redis_health))
                self._required.append(self.host.load(identity_plugin(self.config)))
                if self.config.native_auth_enabled:
                    self._required.append(self.host.load(native_auth_plugin(self.config)))
                self._required.append(self.host.load(assignments_plugin(self.config)))
                self._required.append(self.host.load(human_work_plugin(self.config)))
                self._required.append(self.host.load(administration_plugin(self.config)))
                for plugin in self._plugins:
                    self._required.append(self.host.load(plugin))
                for plugin in self._optional_plugins:
                    self.host.load(plugin)
                report = await self.host.await_ready(
                    self._required, timeout=self.config.runtime_startup_timeout_seconds
                )
                if report.outcome != "ready":
                    raise RuntimeError("Stage required components did not become ready")
                self._started = True
            except BaseException as error:
                self._start_error = error
                raise

    @property
    def ready(self) -> bool:
        return (
            self._started
            and not self._stopping
            and self.host.is_ready(self._required)
            and self.database_health.available
            and (not self.config.redis_required or self.redis_health.available)
        )

    def public_status(self) -> dict[str, str]:
        return {
            "database": (
                "ready"
                if self.database_health.available and not self._stopping and self.host.is_ready([STORAGE])
                else "unavailable"
            ),
            "redis": (
                "disabled"
                if self.config.redis_url is None
                else "ready"
                if self.redis_health.available and not self._stopping and self.host.is_ready([REDIS])
                else "unavailable"
            ),
        }

    async def close(self, timeout: float | None = None) -> CloseReport:
        async with self._start_lock:
            self._stopping = True
            report = await self.host.close(
                timeout=timeout if timeout is not None else self.config.runtime_shutdown_timeout_seconds
            )
            if report.failed or report.blocked or report.pending:
                raise RuntimeError("Stage runtime shutdown did not complete safely")
            return report
