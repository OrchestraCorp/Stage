"""Shared synthetic builders for native credentials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pyotp
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationRootTable, TrustStateTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeSessionTable,
)
from app.native_auth.credentials import LoginProgress, NativeCredentials
from app.native_auth.crypto import digest_secret, random_secret

PASSWORD = "fictional-native-password-2026"


async def credentials(sessions: async_sessionmaker[AsyncSession]) -> NativeCredentials:
    async with sessions.begin() as session:
        session.add_all(
            [
                PrincipalTable(principal_ref="human-a", principal_type="human"),
                OrganizationRootTable(organization_ref="org-a", display_name="Fictional"),
                TenantGuardTable(tenant_ref="org-a", epoch=0),
                TrustStateTable(trust_ref="native-trust", purpose="authentication", configuration_digest="a" * 64),
                NativeClientTable(client_id="maestro", redirect_uris=["http://127.0.0.1:4000/callback"]),
            ]
        )
    service = NativeCredentials(
        sessions, fernet_key=Fernet.generate_key(), organization_ref="org-a", trust_ref="native-trust"
    )
    await service.create_account("account-a", "human-a", "alice", PASSWORD)
    return service


async def request(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    reference, browser, csrf = str(uuid4()), random_secret(), random_secret()
    async with sessions.begin() as session:
        session.add(
            NativeLoginRequestTable(
                request_ref=reference,
                client_id="maestro",
                redirect_uri="http://127.0.0.1:4000/callback",
                state="state",
                nonce="nonce",
                code_challenge="c" * 43,
                browser_hash=digest_secret(browser),
                csrf_hash=digest_secret(csrf),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    return reference, browser, csrf


async def enroll(service: NativeCredentials, sessions: async_sessionmaker[AsyncSession]) -> tuple[str, LoginProgress]:
    ref, browser, csrf = await request(sessions)
    progress = await service.password(ref, browser, csrf, "alice", PASSWORD)
    assert progress.enrollment_secret is not None
    final = await service.otp(ref, browser, csrf, pyotp.TOTP(progress.enrollment_secret).now())
    assert final.status == "authenticated"
    return progress.enrollment_secret, final


async def strong_session(sessions: async_sessionmaker[AsyncSession], *, age: int = 0) -> str:
    reference = str(uuid4())
    async with sessions.begin() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None
        session.add(
            NativeSessionTable(
                session_ref=reference,
                account_ref=account.account_ref,
                client_id="maestro",
                credential_version=account.credential_version,
                auth_time=datetime.now(UTC) - timedelta(seconds=age),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                acr="urn:orchestra:acr:pwd-totp",
                amr=["pwd", "otp"],
            )
        )
    return reference
