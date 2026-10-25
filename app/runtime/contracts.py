"""Process-local component contracts, independent of application frameworks."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeClosed(RuntimeError):
    pass


class MissingService(RuntimeError):
    pass


class DuplicateProvider(RuntimeError):
    pass


class LifecycleWaitCycle(RuntimeError):
    pass


class ComponentState(StrEnum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    UNLOADING = "unloading"
    FAILED = "failed"
    DISPOSED = "disposed"


@dataclass(frozen=True)
class ServiceKey[T]:
    namespace: str
    name: str
    version: str = "1"

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() for v in (self.namespace, self.name, self.version)):
            raise ValueError("service keys require namespace, name and interface version")

    def __str__(self) -> str:
        return f"{self.namespace}.{self.name}@{self.version}"


@dataclass(frozen=True)
class BindingIdentity:
    key: ServiceKey[Any]
    realm: str
    provider_id: str
    generation: int
    registration_id: str


@dataclass(frozen=True)
class PluginSpec:
    name: str
    setup: Callable[[Any], Any]
    requires: tuple[ServiceKey[Any], ...] = ()
    provides: tuple[ServiceKey[Any], ...] = ()
    version: str = "1"
    config: Mapping[str, Any] = field(default_factory=dict)
    inject: Mapping[ServiceKey[Any], Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not callable(self.setup):
            raise ValueError("plugins require a name and a trusted setup callback")
        object.__setattr__(self, "requires", tuple(self.requires))
        object.__setattr__(self, "provides", tuple(self.provides))
        if any(key not in self.requires for key in self.inject):
            raise ValueError("inject configuration requires a declared dependency")
        if len(set(self.provides)) != len(self.provides):
            raise ValueError("duplicate provided service declaration")


@dataclass(frozen=True)
class RuntimeFailure:
    owner_id: str
    name: str
    phase: str
    error_type: str
    message: str


@dataclass(frozen=True)
class ReadyReport:
    outcome: str
    missing: tuple[str, ...] = ()
    failures: tuple[RuntimeFailure, ...] = ()
    generations: tuple[BindingIdentity, ...] = ()

    @property
    def ready(self) -> bool:
        return self.outcome == "ready"


@dataclass(frozen=True)
class CloseReport:
    completed: tuple[str, ...] = ()
    failed: tuple[RuntimeFailure, ...] = ()
    blocked: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return not (self.failed or self.blocked or self.pending)
