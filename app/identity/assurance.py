"""Issuer-profile-specific login assurance, separate from business approval evidence."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _valid_unicode(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid_assurance_reference_unicode") from None
    return value


AssuranceReference = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$"),
    AfterValidator(_valid_unicode),
]


class HumanAssuranceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_ref: AssuranceReference
    profile_version: int = Field(strict=True, ge=1, le=2**63 - 1)
    required_acr: AssuranceReference
    required_amr: tuple[AssuranceReference, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique_methods(self) -> Self:
        if len(set(self.required_amr)) != len(self.required_amr):
            raise ValueError("duplicate_assurance_method")
        return self


class HumanAssurancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["disabled", "required"] = "disabled"
    rules: tuple[HumanAssuranceRule, ...] = Field(default=(), max_length=32)
    max_auth_age_seconds: int | None = Field(default=None, strict=True, ge=1, le=86400)

    @model_validator(mode="after")
    def coherent_policy(self) -> Self:
        if self.mode == "disabled":
            if self.rules or self.max_auth_age_seconds is not None:
                raise ValueError("disabled_assurance_has_constraints")
        elif not self.rules or self.max_auth_age_seconds is None:
            raise ValueError("required_assurance_needs_profiles_and_freshness")
        if len({(rule.profile_ref, rule.profile_version) for rule in self.rules}) != len(self.rules):
            raise ValueError("duplicate_assurance_profile")
        return self
