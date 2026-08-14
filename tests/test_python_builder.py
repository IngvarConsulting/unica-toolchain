from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_manifest import python_manifest
from toolchain.builders.python_pyinstaller import (
    build_python_pyinstaller,
    parse_uv_version,
    run_command,
    verify_builder_identity,
    write_entrypoint_stub,
)
from toolchain.manifest import load_manifest
from toolchain.source import PreparedSource


class PythonBuilderTests(unittest.TestCase):
    def test_command_capture_decodes_utf8_independently_of_windows_locale(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        command = root / "utf8_output.py"
        command.write_text(
            "import os\nos.write(1, 'Привет'.encode('utf-8'))\n",
            encoding="utf-8",
        )

        with patch("locale.getencoding", return_value="cp1252"):
            output = run_command([sys.executable, str(command)])

        self.assertEqual(output, "Привет")

    def test_frozen_entrypoint_dispatches_multiprocessing_before_cli_parsing(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = root / "entrypoint.py"

        write_entrypoint_stub(stub, "rlm_tools_bsl.server", "main")

        source = stub.read_text(encoding="utf-8")
        self.assertIn("import multiprocessing", source)
        self.assertIn("multiprocessing.freeze_support()", source)
        self.assertLess(
            source.index("multiprocessing.freeze_support()"),
            source.index("sys.exit(main())"),
        )

    def test_default_entrypoint_stub_keeps_existing_non_windows_bytes(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = root / "entrypoint.py"

        write_entrypoint_stub(stub, "rlm_tools_bsl.server", "main")

        self.assertEqual(
            stub.read_bytes(),
            (
                "import importlib\n"
                "import multiprocessing\n"
                "import sys\n"
                "\n"
                "MODULE = 'rlm_tools_bsl.server'\n"
                "CALLABLE = 'main'\n"
                "\n"
                "def main():\n"
                "    obj = importlib.import_module(MODULE)\n"
                "    for part in CALLABLE.split('.'):\n"
                "        obj = getattr(obj, part)\n"
                "    return obj()\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    multiprocessing.freeze_support()\n"
                "    sys.exit(main())\n"
            ).encode("utf-8"),
        )

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

    def test_windows_frozen_entrypoints_configure_utf8_before_upstream_import(self) -> None:
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

        def fake_runner(command: list[str], *, cwd=None, env=None) -> str:
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
        build_python_pyinstaller(
            manifest,
            "win-x64",
            source,
            out_dir,
            work_dir,
            runner=fake_runner,
            entrypoint_resolver=lambda _python, name: modules[name],
        )

        package = root / "fake-modules" / "rlm_tools_bsl"
        package.mkdir(parents=True)
        package.joinpath("__init__.py").write_text("", encoding="utf-8")
        module_source = (
            "import sys\n"
            "print('импорт stdout')\n"
            "print('импорт stderr', file=sys.stderr)\n"
            "def main():\n"
            "    print('вызов stdout')\n"
            "    print('вызов stderr', file=sys.stderr)\n"
            "    return 0\n"
        )
        package.joinpath("server.py").write_text(module_source, encoding="utf-8")
        package.joinpath("cli.py").write_text(module_source, encoding="utf-8")
        environment = {
            **os.environ,
            "PYTHONIOENCODING": "ascii:strict",
            "PYTHONPATH": str(root / "fake-modules"),
        }

        for source_name in ("rlm-tools-bsl", "rlm-bsl-index"):
            with self.subTest(source_name=source_name):
                stub = work_dir / "pyinstaller" / source_name / "entrypoint.py"
                result = subprocess.run(
                    [sys.executable, str(stub)],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stderr.decode("utf-8", errors="replace"),
                )
                self.assertIn("импорт stdout".encode(), result.stdout)
                self.assertIn("вызов stdout".encode(), result.stdout)
                self.assertIn("импорт stderr".encode(), result.stderr)
                self.assertIn("вызов stderr".encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
