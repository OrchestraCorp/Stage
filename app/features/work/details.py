"""Trusted metadata-only command for an existing, currently authorized WorkItem.

The command kernel owns identity verification, authorization and the root
transaction. This handler cannot change lifecycle, scope, classification or
authorization facts, and creates no external side effect.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select

from app.authz.permissions import Permission
from app.execution.contracts import CommandEffect, CommandEnvelope, CommandSpec, ExecutionContext
from app.execution.errors import ExecutionError, InvalidCommand
from app.execution.storage import UnitOfWork
from app.models.work import WorkItemTable

MAX_DESCRIPTION_LENGTH = 20_000
MAX_WORK_VERSION = 2**31 - 1


class WorkDetailsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    description: str | None = Field(
        max_length=MAX_DESCRIPTION_LENGTH,
        description="Explicit text or null; the normalized title/description JSON also has a 64 KiB command budget.",
    )

    @field_validator("title", "description")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        # PostgreSQL text cannot contain NUL or isolated Unicode surrogates.
        # Reject them at the typed boundary instead of failing during commit.
        if value is not None and ("\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise ValueError("invalid_text_character")
        return value


class WorkDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resource_ref: str
    title: str
    description: str | None
    lifecycle: Literal["draft", "open"]
    classification: str
    version: int = Field(ge=1, le=MAX_WORK_VERSION)


class WorkDetailsConflict(ExecutionError):
    code = "WORK_DETAILS_CONFLICT"


def _work_id(target_ref: str) -> int:
    suffix = target_ref.removeprefix("work_item:")
    # Bound the text before int() so malformed references cannot hit Python's
    # integer conversion limit. Alternate spellings must not split idempotency.
    if not 1 <= len(suffix) <= 10 or not suffix.isascii() or not suffix.isdigit():
        raise InvalidCommand()
    value = int(suffix)
    if not 1 <= value <= MAX_WORK_VERSION or target_ref != f"work_item:{value}":
        raise InvalidCommand()
    return value


def _projection(work: WorkItemTable) -> dict[str, str | int | None]:
    return {
        "resource_ref": f"work_item:{work.id}",
        "title": work.title,
        "description": work.description,
        "lifecycle": work.lifecycle,
        "classification": work.classification,
        "version": work.version,
    }


async def update_work_details(
    uow: UnitOfWork,
    _context: ExecutionContext,
    command: CommandEnvelope,
    payload: BaseModel,
) -> CommandEffect:
    if not isinstance(payload, WorkDetailsInput):
        raise InvalidCommand()
    uow.assert_owned()
    work = await uow.session.scalar(
        select(WorkItemTable)
        .where(WorkItemTable.id == _work_id(command.target_ref))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # The loader already locks this row before authorizing it. Keep domain
    # invariants local as well, even if a future caller changes that adapter.
    if (
        work is None
        or work.version != command.expected_version
        or work.version >= MAX_WORK_VERSION
        or work.lifecycle not in {"draft", "open"}
    ):
        raise WorkDetailsConflict()
    before = _projection(work)
    work.title = payload.title
    work.description = payload.description
    work.version += 1
    after = _projection(work)
    return CommandEffect(result=after, before=before, after=after)


WORK_DETAILS_COMMAND = CommandSpec(
    name="work.details.update",
    version="1",
    execute_action=Permission.TASK_METADATA_UPDATE,
    read_action=Permission.TASK_VIEW,
    input_model=WorkDetailsInput,
    result_model=WorkDetailsResult,
    handler=update_work_details,
)
