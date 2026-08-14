from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_manifest import python_nuitka_manifest
from toolchain.builders.python_nuitka_standalone import (
    build_python_nuitka_standalone,
    parse_nuitka_report,
    parse_nuitka_version,
)
from toolchain.manifest import load_manifest, release_tag
from toolchain.runtime_archive import validate_runtime_archive
from toolchain.source import PreparedSource


class PythonNuitkaStandaloneTests(unittest.TestCase):
    def make_manifest(self, root: Path, *, target: str = "darwin-arm64"):
        data = python_nuitka_manifest()
        data["builder"]["binaries"] = [
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-bsl-index",
                "module": "rlm_tools_bsl.cli",
                "assetBase": "rlm-bsl-index",
                "smokeArgs": ["--help"],
            },
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-tools-bsl",
                "module": "rlm_tools_bsl.server",
                "assetBase": "rlm-bsl-mcp",
                "smokeArgs": ["--help"],
            },
        ]
        path = root / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_manifest(path), target

    def test_builds_one_archive_from_two_main_programs_and_records_compiler(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest, target_key = self.make_manifest(root)
        source_dir = root / "source"
        source_dir.mkdir()
        source = PreparedSource(
            source_dir,
            manifest.source.commit,
            "b" * 40,
            (),
        )
        out_dir = root / "out"
        work_dir = root / "work"
        commands: list[list[str]] = []

        def fake_runner(command: list[str], *, cwd=None, env=None) -> str:
            commands.append(command)
            if command == ["uv", "--version"]:
                return "uv 0.11.29 (test)"
            if command[-1:] == ["--version"] and "nuitka" in command:
                return "4.1.3"
            if command[-1:] == ["--version"]:
                return "Python 3.12.10"
            if "--mode=standalone" in command:
                output_arg = next(item for item in command if item.startswith("--output-dir="))
                report_arg = next(item for item in command if item.startswith("--report="))
                output = Path(output_arg.split("=", 1)[1])
                report = Path(report_arg.split("=", 1)[1])
                dist = output / "rlm-bsl-index.dist"
                nested = dist / "package"
                nested.mkdir(parents=True)
                executable = dist / "rlm-bsl-index"
                executable.write_bytes(b"compiled multidist")
                executable.chmod(0o755)
                (dist / "libpython3.12.dylib").write_bytes(b"python")
                (nested / "data.json").write_bytes(b"{}")
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    '<nuitka-compilation-report nuitka_version="4.1.3">'
                    '<scons_environment c_compiler="Clang" '
                    'the_cc_name="clang" the_compiler="clang" />'
                    "</nuitka-compilation-report>",
                    encoding="utf-8",
                )
            return ""

        modules = {
            "rlm-bsl-index": ("rlm_tools_bsl.cli", "main"),
            "rlm-tools-bsl": ("rlm_tools_bsl.server", "main"),
        }
        result = build_python_nuitka_standalone(
            manifest,
            target_key,
            source,
            out_dir,
            work_dir,
            runner=fake_runner,
            entrypoint_resolver=lambda _python, name: modules[name],
        )

        self.assertEqual(
            [path.name for path in result.assets],
            ["rlm-tools-bsl-darwin-arm64.tar.gz"],
        )
        compile_commands = [command for command in commands if "--mode=standalone" in command]
        self.assertEqual(len(compile_commands), 1)
        compile_command = compile_commands[0]
        main_args = [item for item in compile_command if item.startswith("--main=")]
        self.assertEqual(len(main_args), 2)
        self.assertTrue(main_args[0].endswith("/rlm-bsl-index.py"))
        self.assertTrue(main_args[1].endswith("/rlm-bsl-mcp.py"))
        self.assertIn("--include-package=rlm_tools_bsl", compile_command)
        self.assertIn("--include-package-data=rlm_tools_bsl", compile_command)
        self.assertIn("--assume-yes-for-downloads", compile_command)
        self.assertFalse(any("onefile" in item for item in compile_command))
        self.assertFalse(any("tempdir" in item for item in compile_command))
        self.assertEqual(
            result.builder_identity,
            {
                "kind": "python-nuitka-standalone",
                "python": "3.12.10",
                "uv": "0.11.29",
                "nuitka": "4.1.3",
                "compiler": {
                    "cCompiler": "Clang",
                    "ccName": "clang",
                    "compiler": "clang",
                },
            },
        )

        archive = validate_runtime_archive(
            result.assets[0],
            expected_release_tag=release_tag(manifest),
            expected_source_ref=manifest.source.ref,
            expected_source_commit=manifest.source.commit,
            expected_target_key=target_key,
            expected_target_triple=manifest.targets[target_key].target_triple,
            expected_entrypoints={
                "rlm-bsl-index": "rlm-bsl-index",
                "rlm-bsl-mcp": "rlm-bsl-mcp",
            },
        )
        by_path = {item.path: item for item in archive.files}
        self.assertEqual(
            by_path["rlm-bsl-index"].sha256,
            by_path["rlm-bsl-mcp"].sha256,
        )
        self.assertIn("libpython3.12.dylib", by_path)
        self.assertIn("package/data.json", by_path)
        self.assertEqual(archive.manifest.builder, result.builder_identity)

    def test_windows_build_uses_exe_names_and_utf8_entrypoints(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest, target_key = self.make_manifest(root, target="win-x64")
        source_dir = root / "source"
        source_dir.mkdir()
        source = PreparedSource(source_dir, manifest.source.commit, "b" * 40, ())

        def fake_runner(command: list[str], *, cwd=None, env=None) -> str:
            if command == ["uv", "--version"]:
                return "uv 0.11.29"
            if command[-1:] == ["--version"] and "nuitka" in command:
                return "4.1.3"
            if command[-1:] == ["--version"]:
                return "Python 3.12.10"
            if "--mode=standalone" in command:
                output = Path(next(item for item in command if item.startswith("--output-dir=")).split("=", 1)[1])
                report = Path(next(item for item in command if item.startswith("--report=")).split("=", 1)[1])
                dist = output / "rlm-bsl-index.dist"
                dist.mkdir(parents=True)
                (dist / "rlm-bsl-index.exe").write_bytes(b"compiled")
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    '<nuitka-compilation-report nuitka_version="4.1.3">'
                    '<scons_environment c_compiler="MSVC" '
                    'the_cc_name="cl" the_compiler="msvc" />'
                    "</nuitka-compilation-report>",
                    encoding="utf-8",
                )
            return ""

        result = build_python_nuitka_standalone(
            manifest,
            target_key,
            source,
            root / "out",
            root / "work",
            runner=fake_runner,
            entrypoint_resolver=lambda _python, name: {
                "rlm-bsl-index": ("rlm_tools_bsl.cli", "main"),
                "rlm-tools-bsl": ("rlm_tools_bsl.server", "main"),
            }[name],
        )

        archive = validate_runtime_archive(
            result.assets[0],
            expected_release_tag=release_tag(manifest),
            expected_source_ref=manifest.source.ref,
            expected_source_commit=manifest.source.commit,
            expected_target_key=target_key,
            expected_target_triple=manifest.targets[target_key].target_triple,
            expected_entrypoints={
                "rlm-bsl-index": "rlm-bsl-index.exe",
                "rlm-bsl-mcp": "rlm-bsl-mcp.exe",
            },
        )
        self.assertEqual(
            {item.path for item in archive.files},
            {"rlm-bsl-index.exe", "rlm-bsl-mcp.exe"},
        )
        stubs = root / "work" / "nuitka" / "entrypoints"
        for name in ("rlm-bsl-index.py", "rlm-bsl-mcp.py"):
            source_text = (stubs / name).read_text(encoding="utf-8")
            self.assertLess(
                source_text.index("configure_utf8_stdio()"),
                source_text.index("importlib.import_module"),
            )

    def test_rejects_wrong_versions_modules_and_incomplete_compiler_report(self) -> None:
        self.assertEqual(parse_nuitka_version("4.1.3\nCommercial: not installed"), "4.1.3")
        with self.assertRaisesRegex(SystemExit, "cannot parse Nuitka version"):
            parse_nuitka_version("Nuitka unknown")

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        report = root / "report.xml"
        report.write_text(
            '<nuitka-compilation-report nuitka_version="4.1.3">'
            "<scons_environment c_compiler=\"Clang\" />"
            "</nuitka-compilation-report>",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SystemExit, "compiler identity"):
            parse_nuitka_report(report, expected_nuitka="4.1.3")

        manifest, target_key = self.make_manifest(root)
        source_dir = root / "source"
        source_dir.mkdir()
        source = PreparedSource(source_dir, manifest.source.commit, "b" * 40, ())

        def wrong_version(command: list[str], *, cwd=None, env=None) -> str:
            if command == ["uv", "--version"]:
                return "uv 0.11.29"
            if command[-1:] == ["--version"] and "nuitka" in command:
                return "4.1.2"
            if command[-1:] == ["--version"]:
                return "Python 3.12.10"
            return ""

        with self.assertRaisesRegex(SystemExit, "Nuitka 4.1.2, expected 4.1.3"):
            build_python_nuitka_standalone(
                manifest,
                target_key,
                source,
                root / "out",
                root / "work-version",
                runner=wrong_version,
                entrypoint_resolver=lambda _python, _name: ("ignored", "main"),
            )

        with self.assertRaisesRegex(
            SystemExit,
            "rlm-bsl-index resolves to other.module, expected rlm_tools_bsl.cli",
        ):
            build_python_nuitka_standalone(
                manifest,
                target_key,
                source,
                root / "out-module",
                root / "work-module",
                runner=lambda command, cwd=None, env=None: (
                    "uv 0.11.29"
                    if command == ["uv", "--version"]
                    else "4.1.3"
                    if command[-1:] == ["--version"] and "nuitka" in command
                    else "Python 3.12.10"
                    if command[-1:] == ["--version"]
                    else ""
                ),
                entrypoint_resolver=lambda _python, _name: ("other.module", "main"),
            )


if __name__ == "__main__":
    unittest.main()
