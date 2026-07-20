from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from tests.test_manifest import cargo_manifest
from toolchain.manifest import load_manifest
from toolchain.source import PreparedSource


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "toolchain.py"


def load_script():
    spec = importlib.util.spec_from_file_location("toolchain_cli", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolchainCliTests(unittest.TestCase):
    def write_manifest(self, root: Path) -> Path:
        path = root / "manifest.json"
        path.write_text(json.dumps(cargo_manifest()), encoding="utf-8")
        return path

    def test_describe_emits_release_builder_matrix_and_expected_files(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest = self.write_manifest(root)

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "describe", "--manifest", str(manifest)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        data = json.loads(result.stdout)

        self.assertEqual(data["releaseTag"], "v8-runner-v0.5.1-build.1")
        self.assertEqual(data["builderKind"], "cargo")
        self.assertEqual(data["builderVersions"], {"rust": "1.95.0"})
        self.assertEqual(len(data["matrix"]["include"]), 3)
        linux = next(row for row in data["matrix"]["include"] if row["target"] == "linux-x64")
        self.assertEqual(linux["systemSetup"], "musl-tools")
        self.assertIn("license-v8-runner.txt", data["expectedReleaseFiles"])

    def test_build_rejects_unknown_target_before_checkout(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest = self.write_manifest(root)

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "build",
                "--manifest",
                str(manifest),
                "--target",
                "solaris-x64",
                "--repo-root",
                str(REPO_ROOT),
                "--work-dir",
                str(root / "work"),
                "--out-dir",
                str(root / "out"),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown target solaris-x64", result.stderr)
        self.assertFalse((root / "work" / "source").exists())

    def test_validate_source_copies_declared_license(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest_path = self.write_manifest(root)
        manifest = load_manifest(manifest_path)
        source_dir = root / "source"
        source_dir.mkdir()
        (source_dir / "LICENSE").write_text("MIT\n", encoding="utf-8")
        prepared = PreparedSource(source_dir, manifest.source.commit, "b" * 40, ())
        module = load_script()

        with (
            patch.object(module, "checkout_source", return_value=source_dir),
            patch.object(module, "prepare_source", return_value=prepared),
            patch.object(
                sys,
                "argv",
                [
                    "toolchain.py",
                    "validate-source",
                    "--manifest",
                    str(manifest_path),
                    "--repo-root",
                    str(root),
                    "--work-dir",
                    str(root / "work"),
                    "--out-dir",
                    str(root / "out"),
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            module.main()

        self.assertEqual((root / "out" / "license-v8-runner.txt").read_text(), "MIT\n")

    def test_build_dispatches_cargo_runs_smoke_and_writes_metadata(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest = load_manifest(self.write_manifest(root))
        source = PreparedSource(root / "source", manifest.source.commit, "b" * 40, ())
        out_dir = root / "out"
        out_dir.mkdir()
        asset = out_dir / "v8-runner-darwin-arm64"
        asset.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        asset.chmod(0o755)
        module = load_script()
        cargo = Mock(return_value=[asset])
        metadata = Mock()

        with (
            patch.object(module, "_prepare", return_value=source),
            patch.object(module, "build_cargo", cargo),
            patch.object(module, "write_target_metadata", metadata),
        ):
            module.build(
                manifest,
                root,
                "darwin-arm64",
                root / "work",
                out_dir,
            )

        cargo.assert_called_once()
        metadata.assert_called_once()

    def test_main_resolves_relative_work_and_output_paths_before_build(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest_path = self.write_manifest(root)
        module = load_script()
        build = Mock()

        with (
            patch.object(module, "build", build),
            patch.object(
                sys,
                "argv",
                [
                    "toolchain.py",
                    "build",
                    "--manifest",
                    str(manifest_path),
                    "--repo-root",
                    ".",
                    "--target",
                    "darwin-arm64",
                    "--work-dir",
                    ".build/example",
                    "--out-dir",
                    "dist/example",
                ],
            ),
        ):
            module.main()

        args = build.call_args.args
        self.assertTrue(args[1].is_absolute())
        self.assertTrue(args[3].is_absolute())
        self.assertTrue(args[4].is_absolute())


if __name__ == "__main__":
    unittest.main()
