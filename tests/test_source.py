from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from toolchain.manifest import load_manifest
from toolchain.source import (
    checkout_source,
    copy_license_assets,
    prepare_source,
    verify_source_commit,
)


def git(repo: Path, *args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if capture else ""


class SourcePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "CI")
        git(self.repo, "config", "user.email", "ci@example.invalid")
        git(self.repo, "config", "tag.gpgSign", "false")
        (self.repo / "message.txt").write_text("original\n", encoding="utf-8")
        (self.repo / "LICENSE").write_text("MIT\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "source")
        self.commit = git(self.repo, "rev-parse", "HEAD", capture=True)
        git(self.repo, "tag", "v1.0.0")
        git(self.repo, "tag", "snapshot-tag")

    def manifest(
        self,
        patches: list[dict] | None = None,
        licenses: list[dict] | None = None,
        commit: str | None = None,
    ):
        data = {
            "schemaVersion": 3,
            "name": "demo",
            "version": "1.0.0",
            "buildRevision": 1,
            "source": {
                "kind": "release",
                "repository": "https://github.com/example/demo",
                "ref": "v1.0.0",
                "commit": commit or self.commit,
            },
            "license": {
                "spdx": "MIT",
                "files": licenses
                if licenses is not None
                else [{"path": "LICENSE", "assetName": "license-demo.txt"}],
            },
            "patches": patches or [],
            "builder": {
                "kind": "cargo",
                "rustVersion": "1.95.0",
                "locked": True,
                "binaries": [
                    {
                        "package": "demo",
                        "sourceName": "demo",
                        "assetBase": "demo",
                        "smokeArgs": ["--help"],
                    }
                ],
            },
            "targets": {
                key: {
                    "runner": runner,
                    "targetTriple": triple,
                    "exe": exe,
                    "systemSetup": "none",
                    "environment": {},
                }
                for key, runner, triple, exe in (
                    ("darwin-arm64", "macos-14", "aarch64-apple-darwin", ""),
                    ("linux-x64", "ubuntu-latest", "x86_64-unknown-linux-gnu", ""),
                    ("win-x64", "windows-latest", "x86_64-pc-windows-msvc", ".exe"),
                )
            },
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_manifest(path)

    def checkout_manifest(self, kind: str, ref: str):
        manifest = self.manifest()
        source = replace(
            manifest.source,
            kind=kind,
            repository=str(self.repo),
            ref=ref,
        )
        return replace(
            manifest,
            version=manifest.version if kind == "release" else None,
            source=source,
        )

    def make_patch(self) -> tuple[str, str]:
        (self.repo / "message.txt").write_text("patched\n", encoding="utf-8")
        patch_text = git(self.repo, "diff", "--binary", capture=True) + "\n"
        git(self.repo, "checkout", "--", "message.txt")
        patch_dir = self.root / "patches" / "demo"
        patch_dir.mkdir(parents=True)
        patch_path = patch_dir / "0001-message.patch"
        patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
        return (
            "patches/demo/0001-message.patch",
            hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        )

    def test_prepares_exact_patched_tree_and_copies_license(self) -> None:
        patch_path, digest = self.make_patch()
        manifest = self.manifest([{"path": patch_path, "sha256": digest}])

        prepared = prepare_source(manifest, self.root, self.repo)
        licenses = copy_license_assets(manifest, prepared, self.root / "out")

        self.assertEqual(prepared.commit, self.commit)
        self.assertEqual([item.path for item in prepared.patches], [patch_path])
        self.assertRegex(prepared.tree, r"^[0-9a-f]{40}$")
        self.assertEqual((self.repo / "message.txt").read_text(encoding="utf-8"), "patched\n")
        self.assertEqual([path.name for path in licenses], ["license-demo.txt"])
        self.assertEqual(licenses[0].read_text(encoding="utf-8"), "MIT\n")

    def test_empty_patch_set_preserves_head_tree(self) -> None:
        manifest = self.manifest()
        expected_tree = git(self.repo, "rev-parse", "HEAD^{tree}", capture=True)

        prepared = prepare_source(manifest, self.root, self.repo)

        self.assertEqual(prepared.tree, expected_tree)
        self.assertEqual(prepared.patches, ())

    def test_checks_out_release_branch_tag_and_direct_commit_refs(self) -> None:
        cases = (
            ("release", "v1.0.0"),
            ("nightly", "main"),
            ("nightly", "snapshot-tag"),
            ("nightly", self.commit),
        )
        for index, (kind, ref) in enumerate(cases):
            with self.subTest(kind=kind, ref=ref):
                destination = self.root / f"checkout-{index}"
                checkout_source(self.checkout_manifest(kind, ref), destination)
                self.assertEqual(
                    git(destination, "rev-parse", "HEAD", capture=True),
                    self.commit,
                )

    def test_rejects_wrong_source_commit(self) -> None:
        with self.assertRaisesRegex(SystemExit, "expected deadbeef"):
            verify_source_commit(self.repo, "deadbeef")

    def test_rejects_patch_hash_mismatch(self) -> None:
        patch_path, _ = self.make_patch()
        manifest = self.manifest([{"path": patch_path, "sha256": "0" * 64}])

        with self.assertRaisesRegex(SystemExit, "patch checksum mismatch"):
            prepare_source(manifest, self.root, self.repo)

    def test_rejects_patch_outside_tool_directory(self) -> None:
        outside = self.root / "outside.patch"
        outside.write_text("not a patch\n", encoding="utf-8")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        manifest = self.manifest([{"path": "outside.patch", "sha256": digest}])

        with self.assertRaisesRegex(SystemExit, "must be inside"):
            prepare_source(manifest, self.root, self.repo)

    def test_rejects_stale_patch(self) -> None:
        patch_path, digest = self.make_patch()
        (self.repo / "message.txt").write_text("unrelated\n", encoding="utf-8")
        git(self.repo, "add", "message.txt")
        git(self.repo, "commit", "-m", "diverged")
        diverged = git(self.repo, "rev-parse", "HEAD", capture=True)
        manifest = self.manifest([{"path": patch_path, "sha256": digest}], commit=diverged)

        with self.assertRaisesRegex(SystemExit, "patch does not apply"):
            prepare_source(manifest, self.root, self.repo)

    def test_rejects_missing_license_source(self) -> None:
        manifest = self.manifest(licenses=[{"path": "COPYING", "assetName": "license-demo.txt"}])
        prepared = prepare_source(manifest, self.root, self.repo)

        with self.assertRaisesRegex(SystemExit, "license source not found"):
            copy_license_assets(manifest, prepared, self.root / "out")


if __name__ == "__main__":
    unittest.main()
