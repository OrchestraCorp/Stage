"""Stable errors without credentials or account-existence disclosures."""


class NativeCredentialError(Exception):
    def __init__(self, code: str = "invalid_credentials") -> None:
        self.code = code
        super().__init__(code)


class NativeCommitUnknown(Exception):
    """A credential change may have committed; never claim the old credential remains usable."""
