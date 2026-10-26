"""Password/TOTP state machines; account changes serialize with business commits.

Browser passwords and OTPs are never accepted as OAuth grants. Login-request
proofs bind every factor to one short-lived client authorization. The trusted
provider supplies session references only after verifying the access token.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.storage import lock_tenant
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationRootTable, TrustStateTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeLoginThrottleTable,
    NativeSessionTable,
)
from app.native_auth.crypto import (
    TotpCipher,
    digest_secret,
    hash_password,
    normalize_username,
    random_secret,
    recovery_codes,
    verify_password,
)
from app.native_auth.current_session import validate_native_session
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError
from app.native_auth.freshness import current_authentication_deadline
from app.native_auth.protocol import OAuthError


@dataclass(frozen=True, slots=True)
class LoginProgress:
    status: str
    account_ref: str | None = None
    recovery_codes: tuple[str, ...] = field(default=(), repr=False)
    enrollment_secret: str | None = field(default=None, repr=False)
    enrollment_uri: str | None = field(default=None, repr=False)
    redirect_uri: str | None = None


@dataclass(frozen=True, slots=True)
class FactorResetResult:
    credential_version: int
    enrollment_code: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AdminResetResult:
    credential_version: int
    enrollment_code: str | None = field(default=None, repr=False)


async def acquire_account_lock(
    session: AsyncSession, account_ref: str, *, organization_ref: str, trust_ref: str
) -> NativeAccountTable:
    """Same trust -> principal -> organization order as human business commands."""
    principal_ref = await session.scalar(
        select(NativeAccountTable.principal_ref).where(NativeAccountTable.account_ref == account_ref)
    )
    if principal_ref is None:
        raise NativeCredentialError()
    trust = await session.scalar(
        select(TrustStateTable).where(TrustStateTable.trust_ref == trust_ref).with_for_update(read=True)
    )
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == principal_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if trust is None or trust.purpose != "authentication" or principal is None or principal.principal_type != "human":
        raise NativeCredentialError()
    await lock_tenant(session, organization_ref)
    root = await session.get(OrganizationRootTable, organization_ref, populate_existing=True)
    account = await session.scalar(
        select(NativeAccountTable)
        .where(NativeAccountTable.account_ref == account_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        account is None
        or root is None
        or root.status != "active"
        or principal.status != "active"
        or principal.disabled_at is not None
    ):
        raise NativeCredentialError()
    return account


def authentication_event(
    session: AsyncSession,
    event: str,
    *,
    account_ref: str | None = None,
    client_id: str | None = None,
    version: int | None = None,
    actor_ref: str | None = None,
) -> None:
    """A deliberately tiny audit whitelist excludes tokens, factors and login data."""
    detail: dict[str, Any] = {} if version is None else {"credential_version": version}
    if actor_ref is not None:
        if not 1 <= len(actor_ref) <= 128 or any(
            ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in actor_ref
        ):
            raise NativeCredentialError("invalid_operator")
        detail["actor_ref"] = actor_ref
    session.add(NativeAuthEventTable(event=event, account_ref=account_ref, client_id=client_id, detail=detail))


async def commit_authentication(session: AsyncSession) -> None:
    try:
        await session.commit()
    except BaseException as exc:
        # Cancellation during COMMIT cannot establish whether a single-use secret
        # was consumed. Caller must not automatically retry the old credential.
        raise NativeCommitUnknown() from exc


class NativeCredentials:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        fernet_key: bytes | str,
        organization_ref: str,
        trust_ref: str,
        issuer_label: str = "OrchestraCorp",
        peer_attempt_limit: int = 30,
        authorization_attempt_limit: int = 30,
        rate_window_seconds: int = 600,
    ) -> None:
        self.sessions = sessions
        self.cipher = TotpCipher(fernet_key)
        self.organization_ref = organization_ref
        self.trust_ref = trust_ref
        self.issuer_label = issuer_label
        if (
            any(
                type(value) is not int or not 1 <= value <= 10000
                for value in (peer_attempt_limit, authorization_attempt_limit)
            )
            or type(rate_window_seconds) is not int
            or not 60 <= rate_window_seconds <= 3600
        ):
            raise ValueError("native_rate_limits_invalid")
        self.peer_attempt_limit = peer_attempt_limit
        self.authorization_attempt_limit = authorization_attempt_limit
        self.rate_window_seconds = rate_window_seconds

    async def lock_account(self, session: AsyncSession, account_ref: str) -> NativeAccountTable:
        return await acquire_account_lock(
            session,
            account_ref,
            organization_ref=self.organization_ref,
            trust_ref=self.trust_ref,
        )

    @staticmethod
    async def validate_browser_request(
        session: AsyncSession,
        request_ref: str,
        browser_secret: str,
        csrf_token: str | None,
        *,
        lock: bool = False,
    ) -> NativeLoginRequestTable:
        query = select(NativeLoginRequestTable).where(NativeLoginRequestTable.request_ref == request_ref)
        if lock:
            query = query.with_for_update()
        request = await session.scalar(query.execution_options(populate_existing=True))
        now = datetime.now(UTC)
        if (
            request is None
            or request.expires_at <= now
            or not browser_secret
            or not hmac.compare_digest(request.browser_hash, digest_secret(browser_secret))
            or (csrf_token is not None and not hmac.compare_digest(request.csrf_hash, digest_secret(csrf_token)))
        ):
            raise NativeCredentialError("invalid_request")
        client = await session.get(NativeClientTable, request.client_id, populate_existing=True)
        if client is None or not client.enabled or client.version != request.client_version:
            raise NativeCredentialError("invalid_request")
        return request

    async def _throttle(self, client_address: str, *, limit: int | None = None) -> None:
        """Bound work before Argon2; source address is transport peer, never forwarded headers.

        Counters intentionally count attempts, including unknown usernames. The
        per-account failure budget below additionally limits distributed guessing.
        """
        now = datetime.now(UTC)
        bucket = digest_secret("native-login-peer:" + client_address)
        async with self.sessions() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert  # type: ignore[assignment]
            await session.execute(
                insert(NativeLoginThrottleTable)
                .values(bucket_hash=bucket, failures=0, window_start=now)
                .on_conflict_do_nothing(index_elements=["bucket_hash"])
            )
            throttle = await session.scalar(
                select(NativeLoginThrottleTable).where(NativeLoginThrottleTable.bucket_hash == bucket).with_for_update()
            )
            assert throttle is not None
            denied = throttle.blocked_until is not None and throttle.blocked_until > now
            if not denied:
                if now - throttle.window_start >= timedelta(seconds=self.rate_window_seconds):
                    throttle.window_start = now
                    throttle.failures = 0
                    throttle.blocked_until = None
                throttle.failures += 1
                if throttle.failures > (limit if limit is not None else self.peer_attempt_limit):
                    throttle.blocked_until = now + timedelta(seconds=self.rate_window_seconds)
                    denied = True
            await commit_authentication(session)
        if denied:
            raise NativeCredentialError("rate_limited")

    async def throttle_authorization(self, client_address: str) -> None:
        """Separate bound for persistent anonymous browser authorization requests."""
        await self._throttle("authorization:" + client_address, limit=self.authorization_attempt_limit)

    async def throttle_logout(self, client_address: str) -> None:
        """Bound anonymous logout requests separately from login ceremonies."""
        await self._throttle("logout:" + client_address, limit=self.authorization_attempt_limit)

    @staticmethod
    def _active(account: NativeAccountTable, now: datetime) -> bool:
        return account.status == "active" and (account.locked_until is None or account.locked_until <= now)

    @staticmethod
    def _failed(session: AsyncSession, account: NativeAccountTable | None, client_id: str, now: datetime) -> None:
        if account is not None:
            account.failures += 1
            if account.failures >= 5:
                account.locked_until = now + timedelta(minutes=1)
                account.failures = 0
        authentication_event(
            session, "login.failed", account_ref=account.account_ref if account else None, client_id=client_id
        )

    async def _revoke_sessions(self, session: AsyncSession, account_ref: str, now: datetime) -> None:
        await session.execute(
            update(NativeSessionTable)
            .where(
                NativeSessionTable.account_ref == account_ref,
                NativeSessionTable.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.execute(
            update(NativeBrowserSessionTable)
            .where(NativeBrowserSessionTable.account_ref == account_ref, NativeBrowserSessionTable.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def _progress(self, request: NativeLoginRequestTable, account: NativeAccountTable | None = None) -> LoginProgress:
        secret = None
        uri = None
        if request.status == "enroll" and request.pending_totp is not None and account is not None:
            secret = self.cipher.decrypt(request.pending_totp)
            uri = self.cipher.uri(secret, account.username, self.issuer_label)
        return LoginProgress(
            request.status,
            request.account_ref,
            enrollment_secret=secret,
            enrollment_uri=uri,
            redirect_uri=request.redirect_uri,
        )

    async def progress(self, request_ref: str, browser_secret: str) -> LoginProgress:
        async with self.sessions() as session:
            request = await self.validate_browser_request(session, request_ref, browser_secret, None)
            if request.account_ref is None:
                return self._progress(request)
            account = await session.get(NativeAccountTable, request.account_ref)
            if (
                account is None
                or account.status != "active"
                or account.credential_version != request.credential_version
            ):
                raise NativeCredentialError("invalid_request")
            return self._progress(request, account)

    async def password(
        self,
        request_ref: str,
        browser_secret: str,
        csrf_token: str,
        username: str,
        password: str,
        *,
        client_address: str = "local",
    ) -> LoginProgress:
        await self._throttle(client_address)
        try:
            normalized = normalize_username(username)
        except NativeCredentialError:
            normalized = ""
        async with self.sessions() as session:
            request = await self.validate_browser_request(session, request_ref, browser_secret, csrf_token)
            if request.status != "password":
                raise NativeCredentialError("invalid_request")
            found = await session.scalar(select(NativeAccountTable).where(NativeAccountTable.username == normalized))
            account_ref = found.account_ref if found else None
            stored_hash = found.password_hash if found else None
            version = found.credential_version if found else None
        valid = await verify_password(stored_hash, password)
        denied = False
        async with self.sessions() as session:
            account = await self.lock_account(session, account_ref) if account_ref else None
            request = await self.validate_browser_request(session, request_ref, browser_secret, csrf_token, lock=True)
            if request.status != "password":
                raise NativeCredentialError("invalid_request")
            now = datetime.now(UTC)
            if (
                not valid
                or account is None
                or not self._active(account, now)
                or account.credential_version != version
                or account.password_hash != stored_hash
            ):
                self._failed(session, account, request.client_id, now)
                denied = True
                result = LoginProgress("password")
            else:
                request.account_ref = account.account_ref
                request.credential_version = account.credential_version
                request.password_verified_at = now
                request.status = "enroll" if account.totp_secret is None and account.totp_bootstrap_allowed else "mfa"
                if request.status == "enroll":
                    request.pending_totp = self.cipher.encrypt(self.cipher.new_secret())
                # Do not reset the shared account budget here: alternating a valid
                # password with guessed OTPs must not reset MFA brute-force limits.
                authentication_event(
                    session, "login.password_verified", account_ref=account.account_ref, client_id=request.client_id
                )
                result = self._progress(request, account)
            await commit_authentication(session)
        if denied:
            raise NativeCredentialError()
        return result

    async def _factor_context(
        self,
        session: AsyncSession,
        request_ref: str,
        browser_secret: str,
        csrf_token: str,
    ) -> tuple[NativeLoginRequestTable, NativeAccountTable]:
        account_ref = await session.scalar(
            select(NativeLoginRequestTable.account_ref).where(NativeLoginRequestTable.request_ref == request_ref)
        )
        if account_ref is None:
            raise NativeCredentialError("invalid_request")
        account = await self.lock_account(session, account_ref)
        request = await self.validate_browser_request(session, request_ref, browser_secret, csrf_token, lock=True)
        now = datetime.now(UTC)
        if (
            request.account_ref != account_ref
            or request.credential_version != account.credential_version
            or request.password_verified_at is None
            or request.password_verified_at > now
            or now - request.password_verified_at >= timedelta(minutes=5)
            or not self._active(account, now)
        ):
            raise NativeCredentialError()
        return request, account

    @staticmethod
    def _factor_deadline(request: NativeLoginRequestTable) -> None:
        now = datetime.now(UTC)
        if (
            request.expires_at <= now
            or request.password_verified_at is None
            or request.password_verified_at > now
            or request.password_verified_at + timedelta(minutes=5) <= now
        ):
            raise NativeCredentialError("invalid_request")

    async def otp(
        self,
        request_ref: str,
        browser_secret: str,
        csrf_token: str,
        code: str,
        *,
        client_address: str = "local",
    ) -> LoginProgress:
        await self._throttle(client_address)
        denied = False
        async with self.sessions() as session:
            request, account = await self._factor_context(session, request_ref, browser_secret, csrf_token)
            if request.status not in {"mfa", "enroll"}:
                raise NativeCredentialError("invalid_request")
            encrypted = request.pending_totp if request.status == "enroll" else account.totp_secret
            if encrypted is None:
                raise NativeCredentialError("invalid_request")
            now = datetime.now(UTC)
            step = self.cipher.verified_step(
                self.cipher.decrypt(encrypted), code, now=now, last_step=account.last_totp_step
            )
            codes: tuple[str, ...] = ()
            if step is None:
                self._failed(session, account, request.client_id, now)
                denied = True
            else:
                if request.status == "enroll":
                    account.totp_secret = encrypted
                    account.totp_bootstrap_allowed = False
                    account.credential_version += 1
                    request.credential_version = account.credential_version
                    codes = recovery_codes()
                    account.recovery_hashes = [digest_secret(code) for code in codes]
                    await self._revoke_sessions(session, account.account_ref, now)
                    authentication_event(
                        session, "factor.enrolled", account_ref=account.account_ref, version=account.credential_version
                    )
                account.last_totp_step = step
                account.failures = 0
                account.locked_until = None
                request.pending_totp = None
                request.status = "authenticated"
                authentication_event(
                    session, "login.mfa_verified", account_ref=account.account_ref, client_id=request.client_id
                )
            result = LoginProgress(
                request.status, account.account_ref, recovery_codes=codes, redirect_uri=request.redirect_uri
            )
            await session.flush()
            self._factor_deadline(request)
            await commit_authentication(session)
        if denied:
            raise NativeCredentialError()
        return result

    async def recover(
        self,
        request_ref: str,
        browser_secret: str,
        csrf_token: str,
        recovery_code: str,
        *,
        client_address: str = "local",
    ) -> LoginProgress:
        await self._throttle(client_address)
        denied = False
        async with self.sessions() as session:
            request, account = await self._factor_context(session, request_ref, browser_secret, csrf_token)
            if request.status != "mfa":
                raise NativeCredentialError("invalid_request")
            now = datetime.now(UTC)
            digest = digest_secret(recovery_code)
            matches = [hmac.compare_digest(stored, digest) for stored in account.recovery_hashes]
            if not any(matches):
                self._failed(session, account, request.client_id, now)
                denied = True
            else:
                account.credential_version += 1
                account.totp_secret = None
                account.totp_bootstrap_allowed = False
                # Keep unused proofs so an interrupted browser ceremony can be
                # restarted. Only the successful replacement generates new codes.
                account.recovery_hashes = [
                    stored for stored, matched in zip(account.recovery_hashes, matches, strict=True) if not matched
                ]
                account.last_totp_step = -1
                request.credential_version = account.credential_version
                request.status = "enroll"
                request.pending_totp = self.cipher.encrypt(self.cipher.new_secret())
                await self._revoke_sessions(session, account.account_ref, now)
                authentication_event(
                    session,
                    "factor.recovery_started",
                    account_ref=account.account_ref,
                    version=account.credential_version,
                )
            result = self._progress(request, account)
            await session.flush()
            self._factor_deadline(request)
            await commit_authentication(session)
        if denied:
            raise NativeCredentialError()
        return result

    async def create_account(self, account_ref: str, principal_ref: str, username: str, password: str) -> str:
        """Trusted local administration only; never creates a Principal or membership."""
        normalize_username(username)
        try:
            if len(account_ref.encode("utf-8")) > 128:
                raise NativeCredentialError("invalid_account")
        except UnicodeError:
            raise NativeCredentialError("invalid_account") from None
        for value in (account_ref, principal_ref):
            if not 1 <= len(value) <= 128 or any(ord(char) < 33 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise NativeCredentialError("invalid_account")
        password_hash = await hash_password(password)
        async with self.sessions() as session:
            trust = await session.scalar(
                select(TrustStateTable).where(TrustStateTable.trust_ref == self.trust_ref).with_for_update(read=True)
            )
            principal = await session.scalar(
                select(PrincipalTable).where(PrincipalTable.principal_ref == principal_ref).with_for_update()
            )
            if (
                trust is None
                or trust.purpose != "authentication"
                or principal is None
                or principal.principal_type != "human"
                or principal.status != "active"
                or principal.disabled_at is not None
            ):
                raise NativeCredentialError("invalid_account")
            await lock_tenant(session, self.organization_ref)
            existing = await session.scalar(
                select(NativeAccountTable).where(
                    (NativeAccountTable.account_ref == account_ref)
                    | (NativeAccountTable.username == username)
                    | (NativeAccountTable.principal_ref == principal_ref)
                )
            )
            if existing is not None:
                raise NativeCredentialError("account_exists")
            account = NativeAccountTable(
                account_ref=account_ref, principal_ref=principal_ref, username=username, password_hash=password_hash
            )
            session.add(account)
            await session.flush()
            authentication_event(session, "account.created", account_ref=account_ref, version=1)
            await commit_authentication(session)
        return account_ref

    async def _session_snapshot(self, session_ref: str) -> tuple[str, str, int]:
        async with self.sessions() as session:
            login = await session.get(NativeSessionTable, session_ref)
            account = await session.get(NativeAccountTable, login.account_ref) if login is not None else None
            if account is None:
                raise NativeCredentialError()
            return account.account_ref, account.password_hash, account.credential_version

    async def _strong_session(
        self,
        session: AsyncSession,
        session_ref: str,
        account: NativeAccountTable,
        *,
        version: int,
    ) -> tuple[NativeSessionTable, datetime]:
        login = await session.scalar(
            select(NativeSessionTable)
            .where(NativeSessionTable.session_ref == session_ref)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if (
            login is None
            or login.account_ref != account.account_ref
            or login.revoked_at is not None
            or login.expires_at <= now
            or login.auth_time > now
            or login.credential_version != version
            or account.credential_version != version
            or account.status != "active"
            or login.acr != "urn:orchestra:acr:pwd-totp"
            or login.amr != ["pwd", "otp"]
        ):
            raise NativeCredentialError()
        deadline = await current_authentication_deadline(
            session, self.organization_ref, login.auth_time, default_seconds=300
        )
        self._final_deadline(login, None, deadline)
        return login, deadline

    async def _access_proof(
        self,
        session: AsyncSession,
        claims: dict[str, Any] | None,
        login: NativeSessionTable,
    ) -> None:
        if claims is None:
            return  # trusted internal callers; public provider always supplies verified claims
        try:
            _, current = await validate_native_session(session, claims, now=datetime.now(UTC))
        except OAuthError:
            raise NativeCredentialError() from None
        if current.session_ref != login.session_ref:
            raise NativeCredentialError()

    @staticmethod
    def _final_deadline(
        login: NativeSessionTable, claims: dict[str, Any] | None, authentication_deadline: datetime
    ) -> None:
        now = datetime.now(UTC)
        if login.expires_at <= now or authentication_deadline <= now:
            raise NativeCredentialError()
        if claims is not None and claims["exp"] <= now.timestamp():
            raise NativeCredentialError()

    async def change_password(
        self,
        session_ref: str,
        old_password: str,
        new_password: str,
        *,
        access_claims: dict[str, Any] | None = None,
    ) -> int:
        account_ref, stored, version = await self._session_snapshot(session_ref)
        valid = await verify_password(stored, old_password)
        new_hash = await hash_password(new_password) if valid else None
        async with self.sessions() as session:
            account = await self.lock_account(session, account_ref)
            login, authentication_deadline = await self._strong_session(session, session_ref, account, version=version)
            await self._access_proof(session, access_claims, login)
            self._final_deadline(login, access_claims, authentication_deadline)
            if not self._active(account, datetime.now(UTC)):
                raise NativeCredentialError()
            if not valid or new_hash is None or account.password_hash != stored:
                self._failed(session, account, login.client_id, datetime.now(UTC))
                await commit_authentication(session)
                raise NativeCredentialError()
            account.password_hash = new_hash
            account.credential_version += 1
            account.failures = 0
            await self._revoke_sessions(session, account_ref, datetime.now(UTC))
            authentication_event(
                session, "password.changed", account_ref=account_ref, version=account.credential_version
            )
            result = account.credential_version
            await session.flush()
            self._final_deadline(login, access_claims, authentication_deadline)
            await commit_authentication(session)
        return result

    async def reset_totp(
        self,
        session_ref: str,
        password: str,
        otp: str,
        *,
        access_claims: dict[str, Any] | None = None,
    ) -> FactorResetResult:
        account_ref, stored, version = await self._session_snapshot(session_ref)
        valid = await verify_password(stored, password)
        async with self.sessions() as session:
            account = await self.lock_account(session, account_ref)
            login, authentication_deadline = await self._strong_session(session, session_ref, account, version=version)
            await self._access_proof(session, access_claims, login)
            self._final_deadline(login, access_claims, authentication_deadline)
            now = datetime.now(UTC)
            if not self._active(account, now):
                raise NativeCredentialError()
            step = (
                self.cipher.verified_step(
                    self.cipher.decrypt(account.totp_secret), otp, now=now, last_step=account.last_totp_step
                )
                if account.totp_secret
                else None
            )
            if not valid or account.password_hash != stored or step is None:
                self._failed(session, account, login.client_id, now)
                await commit_authentication(session)
                raise NativeCredentialError()
            account.credential_version += 1
            account.totp_secret = None
            account.totp_bootstrap_allowed = False
            account.last_totp_step = -1
            enrollment_code = random_secret()
            account.recovery_hashes = [digest_secret(enrollment_code)]
            account.failures = 0
            await self._revoke_sessions(session, account_ref, now)
            authentication_event(session, "factor.reset", account_ref=account_ref, version=account.credential_version)
            result = FactorResetResult(account.credential_version, enrollment_code)
            await session.flush()
            self._final_deadline(login, access_claims, authentication_deadline)
            await commit_authentication(session)
        return result

    async def disable_account(self, account_ref: str, expected_version: int, *, actor_ref: str | None = None) -> int:
        """Explicit OS operator operation; never a caller-controlled public endpoint."""
        async with self.sessions() as session:
            account = await self.lock_account(session, account_ref)
            if account.credential_version != expected_version:
                raise NativeCredentialError("version_conflict")
            account.status = "disabled"
            account.credential_version += 1
            await self._revoke_sessions(session, account_ref, datetime.now(UTC))
            authentication_event(
                session,
                "account.disabled",
                account_ref=account_ref,
                version=account.credential_version,
                actor_ref=actor_ref,
            )
            result = account.credential_version
            await commit_authentication(session)
        return result

    async def admin_reset(
        self,
        account_ref: str,
        expected_version: int,
        *,
        new_password: str | None = None,
        reset_totp: bool = False,
        actor_ref: str | None = None,
    ) -> AdminResetResult:
        """Trusted explicit OS recovery; no mailbox lookup or automatic identity linking.

        Expected credential version prevents blind repeated changes after unknown
        commit. CLI must inspect the current version/event before another attempt.
        """
        if new_password is None and not reset_totp:
            raise NativeCredentialError("invalid_operation")
        new_hash = await hash_password(new_password) if new_password is not None else None
        enrollment_code = random_secret() if reset_totp else None
        async with self.sessions() as session:
            account = await self.lock_account(session, account_ref)
            if account.credential_version != expected_version:
                raise NativeCredentialError("version_conflict")
            if new_hash is not None:
                account.password_hash = new_hash
            if reset_totp:
                account.totp_secret = None
                account.totp_bootstrap_allowed = False
                account.last_totp_step = -1
                assert enrollment_code is not None
                account.recovery_hashes = [digest_secret(enrollment_code)]
            account.credential_version += 1
            account.failures = 0
            account.locked_until = None
            await self._revoke_sessions(session, account_ref, datetime.now(UTC))
            authentication_event(
                session,
                "account.operator_recovery",
                account_ref=account_ref,
                version=account.credential_version,
                actor_ref=actor_ref,
            )
            result = AdminResetResult(account.credential_version, enrollment_code)
            await commit_authentication(session)
        return result
