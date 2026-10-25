"""Trusted, process-local component runtime."""

from app.runtime.contracts import (
    BindingIdentity,
    CloseReport,
    ComponentState,
    DuplicateProvider,
    LifecycleWaitCycle,
    MissingService,
    PluginSpec,
    ReadyReport,
    RuntimeClosed,
    RuntimeFailure,
    ServiceKey,
)
from app.runtime.host import CallScope, ComponentContext, PluginHandle, ProviderHandle, RuntimeHost

__all__ = [
    "BindingIdentity",
    "CallScope",
    "CloseReport",
    "ComponentContext",
    "ComponentState",
    "DuplicateProvider",
    "LifecycleWaitCycle",
    "MissingService",
    "PluginHandle",
    "PluginSpec",
    "ProviderHandle",
    "ReadyReport",
    "RuntimeClosed",
    "RuntimeFailure",
    "RuntimeHost",
    "ServiceKey",
]
