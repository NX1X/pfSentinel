"""Tests for scripts/winget_manifest.py (the winget manifest for a release)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "winget_manifest.py"
_spec = importlib.util.spec_from_file_location("winget_manifest", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
wm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wm)

SHA = "cf4e4af066187da255a7fb2fe30bcc5071fda3b374dba90b1fd126f205099c2d"
CHECKSUMS = (
    "1111111111111111111111111111111111111111111111111111111111111111  pfs\n"
    f"{SHA}  pfs.exe\n"
    "2222222222222222222222222222222222222222222222222222222222222222  "
    "pfsentinel-1.0.0-py3-none-any.whl\n"
)


def _field(text: str, key: str) -> str:
    return next(
        line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(key + ":")
    )


class TestExeSha256:
    def test_takes_the_pfs_exe_line_in_upper_case(self):
        assert wm.exe_sha256(CHECKSUMS) == SHA.upper()

    def test_binary_mode_star_is_accepted(self):
        assert wm.exe_sha256(f"{SHA} *pfs.exe\n") == SHA.upper()

    def test_pfs_alone_is_not_pfs_exe(self):
        with pytest.raises(wm.ManifestError, match="No checksum"):
            wm.exe_sha256(f"{SHA}  pfs\n")

    def test_malformed_hash_is_refused(self):
        with pytest.raises(wm.ManifestError, match="Malformed"):
            wm.exe_sha256("abc  pfs.exe\n")


class TestRender:
    def test_fills_every_placeholder(self):
        files = wm.render("1.0.0", "2026-12-04", SHA)
        assert set(files) == set(wm.FILES)
        for text in files.values():
            assert "{{" not in text
            assert _field(text, "PackageIdentifier") == "NX1X.pfSentinel"
            assert _field(text, "PackageVersion") == "1.0.0"
        installer = files["NX1X.pfSentinel.installer.yaml"]
        assert _field(installer, "  InstallerSha256") == SHA.upper()
        assert _field(installer, "ReleaseDate") == "2026-12-04"
        assert "https://github.com/NX1X/pfSentinel/releases/download/v1.0.0/pfs.exe" in installer

    def test_all_files_use_one_manifest_version(self):
        files = wm.render("1.0.0", "2026-12-04", SHA)
        assert len({_field(t, "ManifestVersion") for t in files.values()}) == 1

    @pytest.mark.parametrize("version", ["1.0.0-rc.5", "1.0.0rc5", "v1.0.0", "1.0"])
    def test_release_candidates_and_odd_versions_are_refused(self, version):
        with pytest.raises(wm.ManifestError, match="not a stable version"):
            wm.render(version, "2026-12-04", SHA)

    def test_bad_date_is_refused(self):
        with pytest.raises(ValueError):
            wm.render("1.0.0", "04/12/2026", SHA)

    def test_left_placeholder_is_refused(self, tmp_path):
        for name in wm.FILES:
            (tmp_path / name).write_text("PackageVersion: {{VERSION}}\nX: {{NEW}}\n")
        with pytest.raises(wm.ManifestError, match="placeholder"):
            wm.render("1.0.0", "2026-12-04", SHA, template_dir=tmp_path)


def test_main_writes_the_winget_pkgs_layout(tmp_path, capsys):
    checksums = tmp_path / "checksums-sha256.txt"
    checksums.write_text(CHECKSUMS)
    out = tmp_path / "build"
    code = wm.main(
        [
            "--version",
            "1.2.3",
            "--date",
            "2027-01-02",
            "--checksums",
            str(checksums),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    folder = out / "manifests" / "n" / "NX1X" / "pfSentinel" / "1.2.3"
    assert capsys.readouterr().out.strip() == folder.as_posix()
    assert sorted(p.name for p in folder.iterdir()) == sorted(wm.FILES)


def test_write_refuses_a_version_that_is_a_path(tmp_path):
    files = wm.render("1.0.0", "2026-12-04", SHA)
    with pytest.raises(wm.ManifestError, match="not a stable version"):
        wm.write(files, "../../evil", tmp_path)


def test_write_refuses_unknown_file_names(tmp_path):
    with pytest.raises(wm.ManifestError, match="Unknown manifest files"):
        wm.write({"../x.yaml": "x"}, "1.0.0", tmp_path)


def test_main_fails_for_a_release_candidate(tmp_path, capsys):
    checksums = tmp_path / "checksums-sha256.txt"
    checksums.write_text(CHECKSUMS)
    code = wm.main(
        [
            "--version",
            "1.0.0-rc.6",
            "--date",
            "2026-12-04",
            "--checksums",
            str(checksums),
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 1
    assert "not a stable version" in capsys.readouterr().err
