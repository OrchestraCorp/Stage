"""Process-local invocation leases, unrelated to durable business fencing."""

from app.runtime.host import CallScope

ServiceLease = CallScope
__all__ = ["CallScope", "ServiceLease"]
