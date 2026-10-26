"""Historical key wrapper; command identity and replay are owned by app.execution.

This value object alone does not provide durable deduplication or authorization.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class IdempotencyKey:
    value: str

    def __post_init__(self) -> None:
        if not self.value or len(self.value) > 128:
            raise ValueError("idempotency key must be 1..128 characters")
