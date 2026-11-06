from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from toolchain.builders.upstream_archive import (  # noqa: E402
    build_upstream_archive,
    materialize_upstream_archive,
    upstream_archive_url,
    verify_upstream_archive,
)
from toolchain.manifest import (  # noqa: E402
    UpstreamArchiveTargetSpec,
    expected_release_files,
    load_manifest,
    release_tag,
)
from toolchain.source import AppliedPatch, PreparedSource, copy_license_assets  # noqa: E402

BINARY = "tool/bin/tool"


def write_zip(path: Path, members: dict[str, bytes], *, modes: dict[str, int] | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (modes or {}).get(name, 0o100644) << 16
            archive.writestr(info, payload)


def describe(path: Path, *, name: str = "tool.zip", binary: str = BINARY) -> UpstreamArchiveTargetSpec:
    return UpstreamArchiveTargetSpec(
        name=name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size=path.stat().st_size,
        binary=binary,
    )


class UpstreamArchiveVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.archive = self.root / "tool.zip"

    def test_accepts_the_pinned_archive(self) -> None:
        write_zip(self.archive, {BINARY: b"run"})
        verify_upstream_archive(self.archive, describe(self.archive))

    def test_refuses_another_checksum_before_reading_members(self) -> None:
        self.archive.write_bytes(b"not a zip at all")
        pinned = replace(describe(self.archive), sha256="0" * 64)
        with self.assertRaisesRegex(SystemExit, "checksum"):
            verify_upstream_archive(self.archive, pinned)

    def test_refuses_another_size(self) -> None:
        write_zip(self.archive, {BINARY: b"run"})
        pinned = replace(describe(self.archive), size=1)
        with self.assertRaisesRegex(SystemExit, "size"):
            verify_upstream_archive(self.archive, pinned)

    def test_refuses_an_archive_without_the_binary(self) -> None:
        write_zip(self.archive, {"tool/readme.txt": b"text"})
        with self.assertRaisesRegex(SystemExit, "no binary"):
            verify_upstream_archive(self.archive, describe(self.archive))

    def test_refuses_members_that_leave_the_destination(self) -> None:
        names = ["../evil", "/evil", "tool/../../evil", "C:/evil"]
        if os.sep != "\\":
            # On Windows zipfile itself turns the backslash into a separator.
            names.append("tool\\evil")
        for name in names:
            with self.subTest(name=name):
                write_zip(self.archive, {BINARY: b"run", name: b"x"})
                with self.assertRaisesRegex(SystemExit, "unsafe member"):
                    verify_upstream_archive(self.archive, describe(self.archive))

    def test_refuses_symbolic_links(self) -> None:
        write_zip(
            self.archive,
            {BINARY: b"run", "tool/link": b"/etc/passwd"},
            modes={"tool/link": stat.S_IFLNK | 0o777},
        )
        with self.assertRaisesRegex(SystemExit, "symbolic link"):
            verify_upstream_archive(self.archive, describe(self.archive))

    def test_materialize_keeps_content_and_returns_the_binary(self) -> None:
        write_zip(
            self.archive,
            {BINARY: b"run", "tool/lib/helper": b"help", "tool/readme.txt": b"text"},
            modes={"tool/lib/helper": 0o100755},
        )
        binary = materialize_upstream_archive(
            self.archive, describe(self.archive), self.root / "out"
        )
        self.assertEqual(binary, self.root / "out" / "tool" / "bin" / "tool")
        self.assertEqual(binary.read_bytes(), b"run")
        helper = self.root / "out" / "tool" / "lib" / "helper"
        self.assertEqual(helper.read_bytes(), b"help")
        if os.name == "posix":
            self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
            readme = self.root / "out" / "tool" / "readme.txt"
            self.assertFalse(readme.stat().st_mode & stat.S_IXUSR)


class CheckedInManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.manifest = load_manifest(REPO_ROOT / "manifests" / "bsl-language-server.json")

    def source(self, patches: tuple[AppliedPatch, ...] = ()) -> PreparedSource:
        return PreparedSource(self.root, self.manifest.source.commit, "tree", patches)

    def test_release_identity_and_file_set(self) -> None:
        self.assertEqual(release_tag(self.manifest), "bsl-language-server-v1.0.7-build.1")
        self.assertEqual(
            expected_release_files(self.manifest),
            {
                "bsl-language-server_mac.zip",
                "bsl-language-server_nix.zip",
                "bsl-language-server_win.zip",
                "license-bsl-language-server-LGPL.txt",
                "license-bsl-language-server-GPL.txt",
                *(f"checksums-bsl-language-server-{key}.txt" for key in self.manifest.targets),
                *(f"provenance-bsl-language-server-{key}.json" for key in self.manifest.targets),
            },
        )

    def test_archive_comes_from_the_pinned_release_of_the_publisher(self) -> None:
        self.assertEqual(
            upstream_archive_url(
                self.manifest, self.manifest.builder.archives["linux-x64"]
            ),
            "https://github.com/1c-syntax/bsl-language-server/releases/download/"
            "v1.0.7/bsl-language-server_nix.zip",
        )

    def test_build_publishes_the_verified_bytes_unchanged(self) -> None:
        payload = self.root / "payload.zip"
        write_zip(payload, {BINARY: b"run"})
        archive = describe(payload, name="bsl-language-server_nix.zip")
        manifest = replace(
            self.manifest,
            builder=replace(
                self.manifest.builder,
                archives={**self.manifest.builder.archives, "linux-x64": archive},
            ),
        )
        assets = build_upstream_archive(
            manifest,
            "linux-x64",
            self.source(),
            self.root / "dist",
            self.root / "work",
            fetch=lambda _url, destination: shutil.copyfile(payload, destination),
        )
        self.assertEqual([asset.name for asset in assets], ["bsl-language-server_nix.zip"])
        self.assertEqual(assets[0].read_bytes(), payload.read_bytes())

    def test_build_publishes_nothing_when_the_download_differs(self) -> None:
        def tampered(_url: str, destination: Path) -> None:
            destination.write_bytes(b"something else")

        with self.assertRaises(SystemExit):
            build_upstream_archive(
                self.manifest,
                "linux-x64",
                self.source(),
                self.root / "dist",
                self.root / "work",
                fetch=tampered,
            )
        self.assertFalse((self.root / "dist" / "bsl-language-server_nix.zip").exists())

    def test_build_refuses_patches_for_an_unchanged_archive(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot apply patches"):
            build_upstream_archive(
                self.manifest,
                "linux-x64",
                self.source((AppliedPatch("patches/x.patch", "0" * 64),)),
                self.root / "dist",
                self.root / "work",
                fetch=mock.Mock(),
            )

    def test_license_set_carries_both_texts_lgpl_requires(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        (source_root / "COPYING.md").write_text("LGPL text", encoding="utf-8")
        copied = copy_license_assets(
            self.manifest,
            PreparedSource(source_root, self.manifest.source.commit, "tree", ()),
            self.root / "licenses",
            repo_root=REPO_ROOT,
        )
        by_name = {path.name: path.read_text(encoding="utf-8") for path in copied}
        self.assertEqual(by_name["license-bsl-language-server-LGPL.txt"], "LGPL text")
        self.assertIn(
            "GNU GENERAL PUBLIC LICENSE", by_name["license-bsl-language-server-GPL.txt"]
        )


class ManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.document = json.loads(
            (REPO_ROOT / "manifests" / "bsl-language-server.json").read_text(encoding="utf-8")
        )

    def load(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.document), encoding="utf-8")
        return load_manifest(path)

    def test_requires_an_archive_for_every_target(self) -> None:
        del self.document["builder"]["archives"]["win-x64"]
        with self.assertRaisesRegex(SystemExit, "archives must cover"):
            self.load()

    def test_requires_a_full_checksum(self) -> None:
        self.document["builder"]["archives"]["win-x64"]["sha256"] = "abc"
        with self.assertRaisesRegex(SystemExit, "sha256"):
            self.load()

    def test_requires_a_positive_size(self) -> None:
        self.document["builder"]["archives"]["win-x64"]["size"] = 0
        with self.assertRaisesRegex(SystemExit, "size"):
            self.load()

    def test_requires_a_relative_binary(self) -> None:
        self.document["builder"]["archives"]["win-x64"]["binary"] = "/abs/tool.exe"
        with self.assertRaisesRegex(SystemExit, "binary"):
            self.load()

    def test_requires_one_binary(self) -> None:
        binaries = self.document["builder"]["binaries"]
        binaries.append({**binaries[0], "assetBase": "second"})
        with self.assertRaisesRegex(SystemExit, "exactly one binary"):
            self.load()

    def test_refuses_an_unknown_license_origin(self) -> None:
        self.document["license"]["files"][1]["origin"] = "elsewhere"
        with self.assertRaisesRegex(SystemExit, "origin"):
            self.load()

    def test_refuses_patches_at_load(self) -> None:
        self.document["patches"] = [{"path": "patches/x.patch", "sha256": "0" * 64}]
        with self.assertRaisesRegex(SystemExit, "patches"):
            self.load()


if __name__ == "__main__":
    unittest.main()
