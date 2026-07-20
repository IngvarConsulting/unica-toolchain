from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_manifest import python_manifest
from toolchain.builders.python_pyinstaller import (
    build_python_pyinstaller,
    parse_uv_version,
    verify_builder_identity,
)
from toolchain.manifest import load_manifest
from toolchain.source import PreparedSource


class PythonBuilderTests(unittest.TestCase):
    def test_parses_uv_platform_metadata_and_checks_exact_identity(self) -> None:
        self.assertEqual(
            parse_uv_version("uv 0.11.29 (901092ee1 aarch64-apple-darwin)"),
            "0.11.29",
        )
        verify_builder_identity(
            python_version="3.12.10",
            uv_version="0.11.29",
            pyinstaller_version="6.21.0",
            expected_python="3.12.10",
            expected_uv="0.11.29",
            expected_pyinstaller="6.21.0",
        )
        with self.assertRaisesRegex(SystemExit, "Python 3.12.13, expected 3.12.10"):
            verify_builder_identity(
                python_version="3.12.13",
                uv_version="0.11.29",
                pyinstaller_version="6.21.0",
                expected_python="3.12.10",
                expected_uv="0.11.29",
                expected_pyinstaller="6.21.0",
            )

    def test_builds_two_entrypoints_from_one_frozen_environment(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = python_manifest()
        data["builder"]["binaries"].append(
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-bsl-index",
                "module": "rlm_tools_bsl.cli",
                "assetBase": "rlm-bsl-index",
                "smokeArgs": ["--help"],
            }
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source_dir = root / "source"
        source_dir.mkdir()
        source = PreparedSource(source_dir, "a" * 40, "b" * 40, ())
        out_dir = root / "out"
        work_dir = root / "work"
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, cwd=None, env=None) -> str:
            calls.append(command)
            if command[-1:] == ["--version"] and "PyInstaller" in command:
                return "6.21.0"
            if command[0] == "uv" and command[1:] == ["--version"]:
                return "uv 0.11.29 (test)"
            if command[-1:] == ["--version"]:
                return "Python 3.12.10"
            if "PyInstaller" in command:
                name = command[command.index("--name") + 1]
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / name).write_bytes(b"native")
            return ""

        modules = {
            "rlm-tools-bsl": ("rlm_tools_bsl.server", "main"),
            "rlm-bsl-index": ("rlm_tools_bsl.cli", "main"),
        }
        assets = build_python_pyinstaller(
            manifest,
            "darwin-arm64",
            source,
            out_dir,
            work_dir,
            runner=fake_runner,
            entrypoint_resolver=lambda _python, name: modules[name],
        )

        self.assertEqual(
            [path.name for path in assets],
            ["rlm-tools-bsl-darwin-arm64", "rlm-bsl-index-darwin-arm64"],
        )
        sync_calls = [command for command in calls if command[:2] == ["uv", "sync"]]
        install_calls = [command for command in calls if command[:3] == ["uv", "pip", "install"]]
        self.assertEqual(len(sync_calls), 1)
        self.assertIn("--frozen", sync_calls[0])
        self.assertIn("--no-dev", sync_calls[0])
        self.assertEqual(len(install_calls), 1)
        self.assertIn("pyinstaller==6.21.0", install_calls[0])
        first_stub = work_dir / "pyinstaller" / "rlm-tools-bsl" / "entrypoint.py"
        self.assertIn("MODULE = 'rlm_tools_bsl.server'", first_stub.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
