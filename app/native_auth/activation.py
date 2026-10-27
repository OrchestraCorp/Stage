"""One-time employee enrollment, deliberately independent of OAuth sessions."""

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import ExternalIdentityTable, OrganizationMembershipTable, TrustStateTable
from app.models.native_auth import NativeAccountActivationTable, NativeAccountTable
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials, authentication_event, commit_authentication
from app.native_auth.crypto import digest_secret, hash_password, recovery_codes
from app.native_auth.errors import NativeCredentialError
from app.native_auth.onboarding_source import load_onboarding_source
from app.native_auth.protocol import valid_text
from app.storage.coordination import LockMode, assert_organization_lock, lock_organization

MAX_VERSION = 2**31 - 1


@dataclass(frozen=True)
class ActivationProgress:
    activation_ref: str
    status: str
    expires_at: datetime
    totp_secret: str | None = field(default=None, repr=False)
    totp_uri: str | None = field(default=None, repr=False)
    recovery_codes: tuple[str, ...] = field(default=(), repr=False)


def clear_activation_secrets(row: NativeAccountActivationTable, *, browser: bool = True) -> None:
    row.code_hash = None
    row.pending_password_hash = None
    row.pending_totp = None
    if browser:
        row.browser_hash = None
        row.csrf_hash = None
        row.ceremony_expires_at = None


def activation_deadline(row: NativeAccountActivationTable) -> datetime:
    return min(row.expires_at, row.ceremony_expires_at or row.expires_at)


async def pending_activation_facts(
    session: AsyncSession, row: NativeAccountActivationTable
) -> tuple[PrincipalTable, OrganizationMembershipTable, ExternalIdentityTable, NativeAccountTable]:
    """Caller holds target Principal X and organization X; do not reacquire upstream locks."""
    assert_organization_lock(session, row.organization_ref, mode=LockMode.EXCLUSIVE)
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == row.principal_ref)
        .execution_options(populate_existing=True)
    )
    member = await session.get(
        OrganizationMembershipTable, (row.organization_ref, row.membership_ref), populate_existing=True
    )
    external = await session.get(ExternalIdentityTable, row.external_identity_ref, populate_existing=True)
    account = await session.get(NativeAccountTable, row.account_ref, populate_existing=True)
    now = datetime.now(UTC)
    if (
        row.status not in {"issued", "enrolling"}
        or principal is None
        or principal.principal_type != "human"
        or principal.status != "suspended"
        or principal.version != 1
        or principal.identity_epoch != 0
        or principal.recovery_epoch != 0
        or principal.disabled_at is not None
        or member is None
        or member.principal_ref != row.principal_ref
        or member.principal_type != "human"
        or member.version != 1
        or member.status != "suspended"
        or member.ended_at is not None
        or member.source_ref != row.activation_ref
        or member.valid_from > now
        or (member.expires_at is not None and member.expires_at <= now)
        or external is None
        or external.principal_ref != row.principal_ref
        or external.principal_type != "human"
        or external.status != "suspended"
        or external.version != 1
        or external.provider_ref != "provider:native-auth"
        or external.subject_namespace != "stage-native-human-v1"
        or external.subject_exact != row.account_ref
        or external.link_evidence_ref != row.activation_ref
        or account is None
        or account.principal_ref != row.principal_ref
        or account.principal_type != "human"
        or account.status != "pending"
        or account.credential_version != 1
        or account.password_hash is not None
        or account.totp_secret is not None
        or account.totp_bootstrap_allowed
        or account.recovery_hashes
        or account.last_totp_step != -1
        or account.failures != 0
        or account.locked_until is not None
    ):
        raise NativeCredentialError("activation_unavailable")
    return principal, member, external, account


class NativeActivation:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig, *, credentials: NativeCredentials
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.credentials = credentials

    @asynccontextmanager
    async def _locked(self, activation_ref: str) -> AsyncIterator[tuple[AsyncSession, NativeAccountActivationTable]]:
        if not valid_text(activation_ref, 128):
            raise NativeCredentialError("activation_unavailable")
        async with self.sessions() as session:
            target = await session.scalar(
                select(NativeAccountActivationTable.principal_ref).where(
                    NativeAccountActivationTable.activation_ref == activation_ref,
                    NativeAccountActivationTable.organization_ref == self.config.organization_ref,
                )
            )
            if target is None:
                raise NativeCredentialError("activation_unavailable")
            trust = await session.scalar(
                select(TrustStateTable)
                .where(TrustStateTable.trust_ref == self.config.trust_ref)
                .with_for_update(read=True)
            )
            if trust is None:
                raise NativeCredentialError("activation_unavailable")
            await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == target).with_for_update())
            await lock_organization(session, self.config.organization_ref, mode=LockMode.EXCLUSIVE)
            row = await session.get(NativeAccountActivationTable, activation_ref, populate_existing=True)
            if row is None or row.principal_ref != target or row.organization_ref != self.config.organization_ref:
                raise NativeCredentialError("activation_unavailable")
            yield session, row

    async def _pending(self, session: AsyncSession, row: NativeAccountActivationTable) -> NativeAccountTable:
        if datetime.now(UTC) >= activation_deadline(row) or row.version >= MAX_VERSION:
            if row.status in {"issued", "enrolling"}:
                clear_activation_secrets(row)
                await commit_authentication(session)
            raise NativeCredentialError("activation_unavailable")
        try:
            await load_onboarding_source(session, self.config, expected=row.source_pin)
            return (await pending_activation_facts(session, row))[3]
        except NativeCredentialError:
            if row.status in {"issued", "enrolling"}:
                # A revoked/changed dependency cannot leave a usable enrollment
                # proof behind to become valid again after configuration repair.
                clear_activation_secrets(row)
                await commit_authentication(session)
            raise

    @staticmethod
    async def _completed(session: AsyncSession, row: NativeAccountActivationTable) -> ActivationProgress:
        if row.completed_at is None or datetime.now(UTC) >= activation_deadline(row):
            # Retain only a bounded outcome-recovery proof after completion.
            clear_activation_secrets(row)
            await commit_authentication(session)
            raise NativeCredentialError("activation_unavailable")
        return ActivationProgress(row.activation_ref, row.status, activation_deadline(row))

    @staticmethod
    def _browser(row: NativeAccountActivationTable, browser_secret: str, csrf_token: str | None = None) -> None:
        if (
            not valid_text(browser_secret, 256, minimum=20)
            or row.browser_hash is None
            or not hmac.compare_digest(row.browser_hash, digest_secret(browser_secret))
            or (
                csrf_token is not None
                and (
                    not valid_text(csrf_token, 256, minimum=20)
                    or row.csrf_hash is None
                    or not hmac.compare_digest(row.csrf_hash, digest_secret(csrf_token))
                )
            )
        ):
            raise NativeCredentialError("activation_unavailable")

    def _progress(
        self,
        row: NativeAccountActivationTable,
        account: NativeAccountTable | None = None,
        *,
        codes: tuple[str, ...] = (),
    ) -> ActivationProgress:
        secret = (
            self.credentials.cipher.decrypt(row.pending_totp)
            if row.status == "enrolling" and row.pending_totp
            else None
        )
        return ActivationProgress(
            row.activation_ref,
            row.status,
            activation_deadline(row),
            secret,
            self.credentials.cipher.uri(secret, account.username, self.credentials.issuer_label)
            if secret and account
            else None,
            codes,
        )

    async def begin(
        self, *, activation_ref: str, code: str, password: str, browser_secret: str, csrf_token: str, peer_key: str
    ) -> ActivationProgress:
        await self.credentials.throttle_activation(peer_key)
        if not all(valid_text(value, 256, minimum=20) for value in (code, browser_secret, csrf_token)):
            raise NativeCredentialError("activation_unavailable")
        # Verify the high-entropy code before spending password memory. Recheck after hashing.
        async with self._locked(activation_ref) as (session, row):
            await self._pending(session, row)
            if (
                row.status != "issued"
                or row.code_hash is None
                or not hmac.compare_digest(row.code_hash, digest_secret(code))
            ):
                raise NativeCredentialError("activation_unavailable")
            version = row.version
        password_hash = await hash_password(password)
        async with self._locked(activation_ref) as (session, row):
            account = await self._pending(session, row)
            if (
                row.status != "issued"
                or row.version != version
                or row.code_hash is None
                or not hmac.compare_digest(row.code_hash, digest_secret(code))
            ):
                raise NativeCredentialError("activation_unavailable")
            row.status = "enrolling"
            row.version += 1
            row.code_hash = None
            row.browser_hash = digest_secret(browser_secret)
            row.csrf_hash = digest_secret(csrf_token)
            row.ceremony_expires_at = min(row.expires_at, datetime.now(UTC) + timedelta(minutes=5))
            row.pending_password_hash = password_hash
            row.pending_totp = self.credentials.cipher.encrypt(self.credentials.cipher.new_secret())
            authentication_event(session, "activation.started", account_ref=account.account_ref)
            progress = self._progress(row, account)
            await session.flush()
            await load_onboarding_source(session, self.config, expected=row.source_pin)
            if datetime.now(UTC) >= activation_deadline(row):
                raise NativeCredentialError("activation_unavailable")
            await commit_authentication(session)
            return progress

    async def progress(self, *, activation_ref: str, browser_secret: str) -> ActivationProgress:
        async with self._locked(activation_ref) as (session, row):
            self._browser(row, browser_secret)
            if row.status == "completed":
                return await self._completed(session, row)
            account = await self._pending(session, row)
            if row.status != "enrolling":
                raise NativeCredentialError("activation_unavailable")
            result = self._progress(row, account)
            if datetime.now(UTC) >= activation_deadline(row):
                raise NativeCredentialError("activation_unavailable")
            return result

    async def complete(
        self, *, activation_ref: str, browser_secret: str, csrf_token: str, code: str, peer_key: str
    ) -> ActivationProgress:
        await self.credentials.throttle_activation(peer_key)
        async with self._locked(activation_ref) as (session, row):
            self._browser(row, browser_secret, csrf_token)
            if row.status == "completed":
                return await self._completed(session, row)
            await self._pending(session, row)
            if (
                row.status != "enrolling"
                or row.pending_totp is None
                or row.pending_password_hash is None
                or row.failures >= 5
            ):
                raise NativeCredentialError("activation_unavailable")
            now = datetime.now(UTC)
            step = self.credentials.cipher.verified_step(
                self.credentials.cipher.decrypt(row.pending_totp), code, now=now, last_step=-1
            )
            if step is None:
                row.failures += 1
                authentication_event(session, "activation.factor_failed", account_ref=row.account_ref)
                if row.failures >= 5:
                    clear_activation_secrets(row)
                await commit_authentication(session)
                raise NativeCredentialError("invalid_credentials")
            principal, member, external, account = await pending_activation_facts(session, row)
            codes = recovery_codes()
            account.password_hash = row.pending_password_hash
            account.totp_secret = row.pending_totp
            account.last_totp_step = step
            account.recovery_hashes = [digest_secret(value) for value in codes]
            account.status = "active"
            account.credential_version += 1
            principal.status = member.status = external.status = "active"
            principal.version += 1
            member.version += 1
            external.version += 1
            row.status = "completed"
            row.version += 1
            row.completed_at = now
            clear_activation_secrets(row, browser=False)
            authentication_event(
                session, "activation.completed", account_ref=account.account_ref, version=account.credential_version
            )
            guard = await session.get(TenantGuardTable, self.config.organization_ref)
            if guard is None or guard.epoch >= MAX_VERSION:
                raise NativeCredentialError("activation_unavailable")
            guard.epoch += 1
            await session.flush()
            await load_onboarding_source(session, self.config, expected=row.source_pin)
            if datetime.now(UTC) >= activation_deadline(row) or (
                member.expires_at is not None and datetime.now(UTC) >= member.expires_at
            ):
                raise NativeCredentialError("activation_unavailable")
            result = self._progress(row, codes=codes)
            await commit_authentication(session)
            return result
