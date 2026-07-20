from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_manifest import cargo_manifest
from toolchain.builders.cargo import build_cargo, verify_rust_identity
from toolchain.manifest import load_manifest
from toolchain.source import PreparedSource


class CargoBuilderTests(unittest.TestCase):
    def test_verifies_exact_rust_version(self) -> None:
        verify_rust_identity("1.95.0", "rustc 1.95.0 (59807616e 2026-04-14)")

        with self.assertRaisesRegex(SystemExit, "Rust 1.94.0, expected 1.95.0"):
            verify_rust_identity("1.95.0", "rustc 1.94.0 (example 2026-01-01)")

    def test_builds_locked_cargo_binary_and_stages_normalized_asset(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = cargo_manifest()
        data["builder"]["binaries"][0]["package"] = "bsl-analyzer"
        data["builder"]["binaries"][0]["sourceName"] = "bsl-analyzer-app"
        data["builder"]["binaries"][0]["assetBase"] = "bsl-analyzer"
        data["targets"]["darwin-arm64"]["environment"] = {"CXXFLAGS": "-DTEST=1"}
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source_dir = root / "source"
        source_dir.mkdir()
        source = PreparedSource(source_dir, "a" * 40, "b" * 40, ())
        work_dir = root / "work"
        out_dir = root / "out"
        calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

        def fake_runner(
            command: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
        ) -> str:
            calls.append((command, cwd, env))
            if command == ["rustc", "--version"]:
                return "rustc 1.95.0 (59807616e 2026-04-14)"
            produced = work_dir / "cargo-target" / "aarch64-apple-darwin" / "release"
            produced.mkdir(parents=True)
            (produced / "bsl-analyzer-app").write_bytes(b"native")
            return ""

        assets = build_cargo(
            manifest,
            "darwin-arm64",
            source,
            out_dir,
            work_dir,
            runner=fake_runner,
        )

        self.assertEqual(assets, [out_dir / "bsl-analyzer-darwin-arm64"])
        self.assertEqual(assets[0].read_bytes(), b"native")
        cargo_command, cargo_cwd, cargo_env = calls[1]
        self.assertEqual(
            cargo_command,
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--package",
                "bsl-analyzer",
                "--bin",
                "bsl-analyzer-app",
                "--target",
                "aarch64-apple-darwin",
                "--target-dir",
                str(work_dir / "cargo-target"),
            ],
        )
        self.assertEqual(cargo_cwd, source_dir)
        self.assertEqual(cargo_env["CXXFLAGS"], "-DTEST=1")


if __name__ == "__main__":
    unittest.main()
