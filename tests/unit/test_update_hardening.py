"""Self-update hardening (security review 2026-11-07, planned for 0.4.0).

The attestation tests use the real GitHub attestation API response for the
v0.3.0 Linux binary (tests/fixtures), so they check real Sigstore data.
"""

from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import responses

from pfsentinel.services import attestation
from pfsentinel.services.attestation import AttestationError
from pfsentinel.services.updater import UpdateError, UpdateService, _newest_release

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "attestations_pfs_v0.3.0.json").read_text()
)
SHA = FIXTURE["sha256"]
GH = "https://github.com/NX1X/pfSentinel/releases/download/v0.4.0/"


def _slsa_bundle() -> dict:
    return copy.deepcopy(
        next(
            a["bundle"]
            for a in FIXTURE["attestations"]
            if "slsa.dev"
            in json.loads(base64.b64decode(a["bundle"]["dsseEnvelope"]["payload"]))["predicateType"]
        )
    )


class TestAttestation:
    def test_the_real_release_attestation_verifies(self) -> None:
        attestation.verify_bundle(_slsa_bundle(), "pfs", SHA)

    def test_another_file_hash_is_refused(self) -> None:
        with pytest.raises(AttestationError, match="does not cover"):
            attestation.verify_bundle(_slsa_bundle(), "pfs", "0" * 64)

    def test_another_asset_name_is_refused(self) -> None:
        with pytest.raises(AttestationError, match="does not cover"):
            attestation.verify_bundle(_slsa_bundle(), "evil", SHA)

    def test_a_changed_statement_breaks_the_signature(self) -> None:
        bundle = _slsa_bundle()
        statement = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
        statement["subject"].append({"name": "pfs", "digest": {"sha256": "f" * 64}})
        bundle["dsseEnvelope"]["payload"] = base64.b64encode(
            json.dumps(statement).encode()
        ).decode()
        with pytest.raises(AttestationError, match="signature does not verify"):
            attestation.verify_bundle(bundle, "pfs", "f" * 64)

    def test_githubs_release_attestation_is_not_build_provenance(self) -> None:
        other = next(a["bundle"] for a in FIXTURE["attestations"] if a["bundle"] is not None)
        if (
            "slsa.dev"
            in json.loads(base64.b64decode(other["dsseEnvelope"]["payload"]))["predicateType"]
        ):
            pytest.skip("fixture order changed")
        with pytest.raises(AttestationError, match="not a SLSA"):
            attestation.verify_bundle(other, "pfs", SHA)

    def test_a_changed_log_time_fails_the_log_promise(self) -> None:
        bundle = _slsa_bundle()
        bundle["verificationMaterial"]["tlogEntries"][0]["integratedTime"] = "1"
        with pytest.raises(AttestationError, match="log promise does not verify"):
            attestation.verify_bundle(bundle, "pfs", SHA)

    def test_a_certificate_from_another_ca_is_refused(self) -> None:
        bundle = _slsa_bundle()
        other = next(a["bundle"] for a in FIXTURE["attestations"] if a["bundle"] != bundle)
        bundle["verificationMaterial"]["certificate"] = other["verificationMaterial"]["certificate"]
        with pytest.raises(AttestationError, match="not issued by the pinned Sigstore CA"):
            attestation.verify_bundle(bundle, "pfs", SHA)

    @responses.activate
    def test_verify_file_uses_the_api(self, tmp_path: Path) -> None:
        f = tmp_path / "pfs"
        f.write_bytes(b"not the real binary")
        import hashlib

        url = attestation.API_URL.format(digest=hashlib.sha256(b"not the real binary").hexdigest())
        responses.add(responses.GET, url, status=404)
        with pytest.raises(AttestationError, match="no attestation"):
            attestation.verify_file(f, "pfs")


def _svc(tmp_path: Path) -> UpdateService:
    with patch("pfsentinel.services.updater.app_config_dir", return_value=tmp_path / "cfg"):
        return UpdateService()


class TestDownloadSafety:
    @pytest.mark.parametrize(
        "url",
        [
            "http://github.com/NX1X/pfSentinel/releases/download/v1/pfs",
            "https://evil.example/pfs",
            "https://github.com.evil.example/pfs",
        ],
    )
    def test_only_https_github_hosts(self, tmp_path: Path, url: str) -> None:
        with pytest.raises(UpdateError, match="Refusing to download"):
            _svc(tmp_path)._download(url, io.BytesIO())

    @responses.activate
    def test_a_redirect_to_another_host_is_refused(self, tmp_path: Path) -> None:
        responses.add(
            responses.GET, GH + "pfs", status=302, headers={"Location": "https://evil.example/x"}
        )
        responses.add(responses.GET, "https://evil.example/x", body=b"payload")
        with pytest.raises(UpdateError, match="Refusing to download from https://evil.example"):
            _svc(tmp_path)._download(GH + "pfs", io.BytesIO())

    @responses.activate
    def test_oversized_downloads_are_refused(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        svc.MAX_DOWNLOAD_BYTES = 1000  # type: ignore[misc]
        responses.add(responses.GET, GH + "pfs", body=b"x" * 5000)
        with pytest.raises(UpdateError, match="maximum"):
            svc._download(GH + "pfs", io.BytesIO())

    @responses.activate
    def test_a_failed_check_leaves_no_staged_file(self, tmp_path: Path) -> None:
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        exe = exe_dir / "pfs"
        exe.write_bytes(b"old")
        responses.add(responses.GET, GH + "pfs", body=b"new")
        svc = _svc(tmp_path)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch.object(svc, "_verify_checksum"),
            patch.object(
                svc,
                "_verify_attestation",
                side_effect=UpdateError("Build attestation check failed"),
            ),
            pytest.raises(UpdateError, match="attestation"),
        ):
            mock_sys.executable = str(exe)
            svc._install_binary(GH + "pfs", "v0.4.0")
        assert exe.read_bytes() == b"old"
        assert sorted(p.name for p in exe_dir.iterdir()) == ["pfs"], "nothing staged is left behind"


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid"), reason="POSIX only")
class TestRootUpdates:
    def test_root_refuses_a_folder_others_can_write(self, tmp_path: Path) -> None:
        folder = tmp_path / "bin"
        folder.mkdir(mode=0o777)
        folder.chmod(0o777)
        with (
            patch("pfsentinel.services.updater.os.geteuid", return_value=0),
            pytest.raises(UpdateError, match="Refusing to update as root"),
        ):
            UpdateService._refuse_unsafe_root_update(folder)

    def test_a_normal_user_is_not_affected(self, tmp_path: Path) -> None:
        UpdateService._refuse_unsafe_root_update(tmp_path)


class TestRevertPath:
    def test_revert_ignores_a_path_planted_in_the_state_file(self, tmp_path: Path) -> None:
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        exe = exe_dir / "pfs"
        exe.write_bytes(b"current")
        planted = tmp_path / "attacker-file"
        planted.write_bytes(b"evil")
        svc = _svc(tmp_path)
        svc._state.update(
            previous_version="0.3.0", install_method="binary", previous_binary_backup=str(planted)
        )
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            pytest.raises(UpdateError, match="No binary backup"),
        ):
            mock_sys.executable = str(exe)
            svc.revert()
        assert exe.read_bytes() == b"current"


class TestPrerelease:
    def test_newest_release_includes_prereleases_and_skips_drafts(self) -> None:
        releases = [
            {"tag_name": "v0.3.0", "prerelease": False},
            {"tag_name": "v1.0.0rc1", "prerelease": True},
            {"tag_name": "v1.0.0", "draft": True},
            {"tag_name": "not-a-version"},
        ]
        assert _newest_release(releases)["tag_name"] == "v1.0.0rc1"

    def test_the_changelog_style_rc_tag_sorts_above_the_last_stable(self) -> None:
        releases = [{"tag_name": "v0.6.0"}, {"tag_name": "v1.0.0-rc.1", "prerelease": True}]
        assert _newest_release(releases)["tag_name"] == "v1.0.0-rc.1"

    @responses.activate
    def test_check_with_pre_finds_the_release_candidate(self, tmp_path: Path) -> None:
        responses.add(
            responses.GET,
            UpdateService.GITHUB_RELEASES_URL,
            json=[
                {"tag_name": "v0.3.0", "assets": [], "html_url": ""},
                {"tag_name": "v1.0.0rc1", "prerelease": True, "assets": [], "html_url": ""},
            ],
        )
        result = _svc(tmp_path).check(force=True, prerelease=True)
        assert result is not None and result["latest"] == "1.0.0rc1"


def test_install_command_has_pre_option() -> None:
    import re

    from typer.testing import CliRunner

    from pfsentinel.cli.app import app

    out = CliRunner().invoke(app, ["update", "install", "--help"]).stdout
    # CI terminals get colored help; the color codes can split "--pre".
    assert "--pre" in re.sub(r"\x1b\[[0-9;]*m", "", out)
