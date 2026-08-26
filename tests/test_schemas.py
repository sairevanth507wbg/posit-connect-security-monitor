"""Tests for the Pydantic schemas that normalise Connect API payloads."""

from __future__ import annotations

from schemas.application import ApplicationSchema, UserSchema
from schemas.package import PackageSchema, PackageType, normalise_package_type


class TestPackageType:
    def test_python_aliases_map_to_python(self):
        for value in ("python", "Python", "PY", "pypi"):
            assert normalise_package_type(value) is PackageType.PYTHON

    def test_r_aliases_map_to_r(self):
        for value in ("r", "R", "CRAN", "bioconductor"):
            assert normalise_package_type(value) is PackageType.R

    def test_unknown_and_empty_map_to_unknown(self):
        for value in ("", None, "cobol", 42):
            assert normalise_package_type(value) is PackageType.UNKNOWN


class TestPackageSchema:
    def test_parses_connect_payload(self):
        pkg = PackageSchema.model_validate(
            {"language": "python", "name": "pandas", "version": "2.2.2", "hash": None}
        )
        assert pkg.package_name == "pandas"
        assert pkg.package_version == "2.2.2"
        assert pkg.package_type is PackageType.PYTHON

    def test_null_version_becomes_empty_string(self):
        """An unpinned package must not fail validation."""
        pkg = PackageSchema.model_validate(
            {"language": "python", "name": "requests", "version": None}
        )
        assert pkg.package_version == ""

    def test_missing_version_becomes_empty_string(self):
        pkg = PackageSchema.model_validate({"language": "r", "name": "dplyr"})
        assert pkg.package_version == ""

    def test_alternate_field_names(self):
        pkg = PackageSchema.model_validate(
            {"package_name": "numpy", "package_version": "2.1.0", "package_type": "Python"}
        )
        assert (pkg.package_name, pkg.package_version) == ("numpy", "2.1.0")

    def test_unknown_keys_ignored(self):
        pkg = PackageSchema.model_validate(
            {"name": "flask", "version": "3.0.0", "language": "python",
             "some_future_field": "whatever"}
        )
        assert pkg.package_name == "flask"

    def test_identity_matches_unique_constraint(self):
        pkg = PackageSchema.model_validate(
            {"name": "pandas", "version": "2.2.2", "language": "python"}
        )
        assert pkg.identity == ("pandas", "2.2.2", "Python")


class TestApplicationSchema:
    BASE = {
        "guid": "c-1", "name": "treasury-dashboard", "title": "Treasury Dashboard",
        "owner_guid": "u-1", "bundle_id": 4271,
        "created_time": "2024-01-15T10:30:00Z",
        "last_deployed_time": "2025-06-02T14:05:11Z",
    }

    def test_prefers_title_over_slug(self):
        app = ApplicationSchema.model_validate(self.BASE)
        assert app.app_name == "Treasury Dashboard"

    def test_falls_back_to_slug_then_guid(self):
        app = ApplicationSchema.model_validate({**self.BASE, "title": ""})
        assert app.app_name == "treasury-dashboard"
        app = ApplicationSchema.model_validate({"guid": "c-9", "title": "", "name": ""})
        assert app.app_name == "c-9"

    def test_bundle_id_coerced_to_string(self):
        """Connect returns bundle_id as an int on some releases."""
        app = ApplicationSchema.model_validate(self.BASE)
        assert app.bundle_id == "4271"

    def test_null_bundle_id_stays_none(self):
        app = ApplicationSchema.model_validate({**self.BASE, "bundle_id": None})
        assert app.bundle_id is None

    def test_timestamps_parsed(self):
        app = ApplicationSchema.model_validate(self.BASE)
        assert app.created_at is not None and app.created_at.year == 2024
        assert app.updated_at is not None and app.updated_at.year == 2025

    def test_updated_at_falls_back_to_updated_time(self):
        payload = {k: v for k, v in self.BASE.items() if k != "last_deployed_time"}
        payload["updated_time"] = "2025-01-02T03:04:05Z"
        app = ApplicationSchema.model_validate(payload)
        assert app.updated_at is not None and app.updated_at.year == 2025

    def test_embedded_owner_flattened(self):
        """Newer Connect embeds the owner object via ?include=owner."""
        app = ApplicationSchema.model_validate(
            {**self.BASE, "owner": {"guid": "u-1", "username": "srevanth",
                                    "first_name": "Sai", "last_name": "Revanth",
                                    "email": "sai@example.com"}}
        )
        assert app.resolved_owner() == "Sai Revanth"
        assert app.owner_email == "sai@example.com"

    def test_owner_falls_back_to_username_then_guid(self):
        app = ApplicationSchema.model_validate(
            {**self.BASE, "owner": {"guid": "u-1", "username": "srevanth"}}
        )
        assert app.resolved_owner() == "srevanth"
        app = ApplicationSchema.model_validate({**self.BASE, "owner_guid": "u-9"})
        assert app.resolved_owner() == "u-9"

    def test_content_url_derived_when_absent(self):
        app = ApplicationSchema.model_validate(self.BASE)
        assert app.content_url is None
        url = app.fallback_content_url("https://connect.example.com/")
        assert url == "https://connect.example.com/connect/#/apps/c-1"

    def test_existing_content_url_preserved(self):
        app = ApplicationSchema.model_validate(
            {**self.BASE, "content_url": "https://connect.example.com/content/c-1/"}
        )
        assert app.fallback_content_url("https://x").endswith("/content/c-1/")


class TestUserSchema:
    def test_display_name_precedence(self):
        assert UserSchema(first_name="Sai", last_name="Revanth",
                          username="s").display_name() == "Sai Revanth"
        assert UserSchema(username="srevanth").display_name() == "srevanth"
        assert UserSchema(guid="u-1").display_name() == "u-1"
