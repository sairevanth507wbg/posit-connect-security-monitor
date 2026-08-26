"""Package schema."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Tuple

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class PackageType(str, Enum):
    PYTHON = "Python"
    R = "R"
    QUARTO = "Quarto"
    UNKNOWN = "Unknown"


LANGUAGE_MAP: Dict[str, PackageType] = {
    "python": PackageType.PYTHON,
    "py": PackageType.PYTHON,
    "pypi": PackageType.PYTHON,
    "r": PackageType.R,
    "cran": PackageType.R,
    "bioconductor": PackageType.R,
    "quarto": PackageType.QUARTO,
}


def normalise_package_type(value: Any) -> PackageType:
    if isinstance(value, PackageType):
        return value
    if not isinstance(value, str) or not value.strip():
        return PackageType.UNKNOWN
    return LANGUAGE_MAP.get(value.strip().lower(), PackageType.UNKNOWN)


class PackageSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    package_name: str = Field(
        validation_alias=AliasChoices("name", "package", "package_name"), min_length=1
    )
    package_version: str = Field(
        default="", validation_alias=AliasChoices("version", "package_version")
    )
    package_type: PackageType = Field(
        default=PackageType.UNKNOWN,
        validation_alias=AliasChoices("language", "type", "package_type", "runtime"),
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)

        # An unpinned package comes back as version: null.
        for key in ("version", "package_version"):
            if payload.get(key) is None:
                payload.pop(key, None)

        for key in ("language", "type", "package_type", "runtime"):
            if key in payload:
                payload[key] = normalise_package_type(payload[key])
                break

        return payload

    @property
    def identity(self) -> Tuple[str, str, str]:
        return (self.package_name, self.package_version, self.package_type.value)

    def __str__(self) -> str:
        return (self.package_name + " " + self.package_version).strip()
