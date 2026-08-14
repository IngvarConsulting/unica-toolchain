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

from tests.test_manifest import cargo_manifest, python_manifest, python_nuitka_manifest
from toolchain.builders.python_nuitka_standalone import NuitkaBuildResult
from toolchain.manifest import load_manifest, release_tag
from toolchain.runtime_archive import write_runtime_archive
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

    def test_smoke_captures_full_rlm_index_help_lifecycle_and_cyrillic(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = python_manifest()
        binary = data["builder"]["binaries"][0]
        binary.update(
            {
                "sourceName": "rlm-bsl-index",
                "module": "rlm_tools_bsl.cli",
                "assetBase": "rlm-bsl-index",
                "smokeArgs": ["--help"],
                "smokeChecks": [
                    {
                        "args": ["index", "build", "--help"],
                        "expectedOutput": [
                            "Строить неполный индекс",
                            "--allow-unsupported-format",
                        ],
                    },
                    {
                        "args": ["index", "update", "--help"],
                        "expectedOutput": ["usage: rlm-bsl-index index update"],
                    },
                    {
                        "args": ["index", "info", "--help"],
                        "expectedOutput": ["usage: rlm-bsl-index index info"],
                    },
                ],
            }
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        asset = root / "rlm-bsl-index-win-x64.exe"
        calls = root / "calls.txt"
        asset.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            f"log = pathlib.Path({str(calls)!r})\n"
            "with log.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if args == ['--help']:\n"
            "    print('usage: rlm-bsl-index {index}')\n"
            "elif args == ['index', 'build', '--help']:\n"
            "    print('Строить неполный индекс --allow-unsupported-format')\n"
            "elif args == ['index', 'update', '--help']:\n"
            "    print('usage: rlm-bsl-index index update')\n"
            "elif args == ['index', 'info', '--help']:\n"
            "    print('usage: rlm-bsl-index index info')\n"
            "else:\n"
            "    raise SystemExit(9)\n",
            encoding="utf-8",
        )
        asset.chmod(0o755)
        module = load_script()
        captured = io.StringIO()

        with redirect_stdout(captured):
            module._smoke(manifest, "win-x64", [asset])

        self.assertEqual(
            calls.read_text(encoding="utf-8").splitlines(),
            [
                "--help",
                "index build --help",
                "index update --help",
                "index info --help",
            ],
        )
        self.assertIn("Строить неполный индекс", captured.getvalue())
        self.assertIn("smoke passed: rlm-bsl-index-win-x64.exe index build --help", captured.getvalue())

        console_bytes = io.BytesIO()
        cp1252_console = io.TextIOWrapper(
            console_bytes,
            encoding="cp1252",
            errors="strict",
            write_through=True,
        )
        with redirect_stdout(cp1252_console):
            module._smoke(manifest, "win-x64", [asset])
        self.assertIn("Строить неполный индекс".encode("utf-8"), console_bytes.getvalue())

        asset.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('ASCII help without the declared output')\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            SystemExit,
            "smoke output mismatch.*Строить неполный индекс",
        ):
            with redirect_stdout(io.StringIO()):
                module._smoke(manifest, "win-x64", [asset])

    def test_python_builder_identity_records_only_applied_windows_stdio_policy(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(python_manifest()), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        module = load_script()

        self.assertEqual(
            module.builder_identity(manifest, "win-x64"),
            {
                "kind": "python-pyinstaller",
                "python": "3.12.10",
                "uv": "0.11.29",
                "pyinstaller": "6.21.0",
                "stdio": {
                    "encoding": "utf-8",
                    "stdoutErrors": "surrogateescape",
                    "stderrErrors": "backslashreplace",
                },
            },
        )
        self.assertEqual(
            module.builder_identity(manifest, "darwin-arm64"),
            {
                "kind": "python-pyinstaller",
                "python": "3.12.10",
                "uv": "0.11.29",
                "pyinstaller": "6.21.0",
            },
        )

    def test_nuitka_build_smokes_extracted_archive_before_writing_metadata(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = python_nuitka_manifest()
        data["builder"]["binaries"] = [
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-bsl-index",
                "module": "rlm_tools_bsl.cli",
                "assetBase": "rlm-bsl-index",
                "smokeArgs": ["--help"],
                "smokeChecks": [
                    {
                        "args": ["index", "build", "--help"],
                        "expectedOutput": ["Строить неполный индекс"],
                    },
                    {
                        "args": ["index", "update", "--help"],
                        "expectedOutput": ["usage: rlm-bsl-index index update"],
                    },
                    {
                        "args": ["index", "info", "--help"],
                        "expectedOutput": ["usage: rlm-bsl-index index info"],
                    },
                ],
            },
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-tools-bsl",
                "module": "rlm_tools_bsl.server",
                "assetBase": "rlm-bsl-mcp",
                "smokeArgs": ["--help"],
            },
        ]
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source = PreparedSource(root / "source", manifest.source.commit, "b" * 40, ())
        source.path.mkdir()
        payload = root / "payload"
        payload.mkdir()
        calls = root / "calls.txt"
        executable = (
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"log = Path({str(calls)!r})\n"
            "with log.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(Path(sys.argv[0]).name + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "name = Path(sys.argv[0]).name\n"
            "args = sys.argv[1:]\n"
            "if name == 'rlm-bsl-mcp':\n"
            "    print('usage: rlm-bsl-mcp')\n"
            "elif args == ['--help']:\n"
            "    print('usage: rlm-bsl-index')\n"
            "elif args == ['index', 'build', '--help']:\n"
            "    print('Строить неполный индекс')\n"
            "elif args == ['index', 'update', '--help']:\n"
            "    print('usage: rlm-bsl-index index update')\n"
            "elif args == ['index', 'info', '--help']:\n"
            "    print('usage: rlm-bsl-index index info')\n"
            "else:\n"
            "    raise SystemExit(9)\n"
        ).encode()
        for name in ("rlm-bsl-index", "rlm-bsl-mcp"):
            path = payload / name
            path.write_bytes(executable)
            path.chmod(0o755)
        archive = root / "out" / "rlm-tools-bsl-darwin-arm64.tar.gz"
        builder_identity = {
            "kind": "python-nuitka-standalone",
            "python": "3.12.10",
            "uv": "0.11.29",
            "nuitka": "4.1.3",
            "compiler": {
                "cCompiler": "Clang",
                "ccName": "clang",
                "compiler": "clang",
            },
        }
        write_runtime_archive(
            archive_path=archive,
            payload_root=payload,
            release_tag=release_tag(manifest),
            source={
                "ref": manifest.source.ref,
                "commit": source.commit,
                "tree": source.tree,
                "patches": [],
            },
            target={
                "key": "darwin-arm64",
                "triple": manifest.targets["darwin-arm64"].target_triple,
            },
            entrypoints={
                "rlm-bsl-index": "rlm-bsl-index",
                "rlm-bsl-mcp": "rlm-bsl-mcp",
            },
            builder=builder_identity,
        )
        module = load_script()
        metadata = Mock()

        with (
            patch.object(module, "_prepare", return_value=source),
            patch.object(
                module,
                "build_python_nuitka_standalone",
                return_value=NuitkaBuildResult((archive,), builder_identity),
                create=True,
            ),
            patch.object(module, "write_target_metadata", metadata),
        ):
            module.build(
                manifest,
                root,
                "darwin-arm64",
                root / "work",
                root / "out",
            )

        self.assertEqual(
            calls.read_text(encoding="utf-8").splitlines(),
            [
                "rlm-bsl-index --help",
                "rlm-bsl-index index build --help",
                "rlm-bsl-index index update --help",
                "rlm-bsl-index index info --help",
                "rlm-bsl-mcp --help",
            ],
        )
        metadata.assert_called_once()
        self.assertEqual(metadata.call_args.args[3], [archive])
        self.assertEqual(
            metadata.call_args.kwargs["builder_identity"], builder_identity
        )

    def test_nuitka_build_rejects_corrupt_archive_before_metadata(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = python_nuitka_manifest()
        data["builder"]["binaries"] = [
            dict(data["builder"]["binaries"][0], sourceName="rlm-bsl-index", module="rlm_tools_bsl.cli", assetBase="rlm-bsl-index"),
            dict(data["builder"]["binaries"][0], sourceName="rlm-tools-bsl", module="rlm_tools_bsl.server", assetBase="rlm-bsl-mcp"),
        ]
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source = PreparedSource(root / "source", manifest.source.commit, "b" * 40, ())
        archive = root / "out" / "rlm-tools-bsl-darwin-arm64.tar.gz"
        archive.parent.mkdir()
        archive.write_bytes(b"not a tar archive")
        identity = {
            "kind": "python-nuitka-standalone",
            "python": "3.12.10",
            "uv": "0.11.29",
            "nuitka": "4.1.3",
            "compiler": {"cCompiler": "Clang", "ccName": "clang", "compiler": "clang"},
        }
        module = load_script()
        metadata = Mock()

        with (
            patch.object(module, "_prepare", return_value=source),
            patch.object(
                module,
                "build_python_nuitka_standalone",
                return_value=NuitkaBuildResult((archive,), identity),
                create=True,
            ),
            patch.object(module, "write_target_metadata", metadata),
        ):
            with self.assertRaisesRegex(SystemExit, "cannot read runtime archive"):
                module.build(manifest, root, "darwin-arm64", root / "work", root / "out")

        metadata.assert_not_called()

    def test_nuitka_build_rejects_archive_builder_identity_drift_before_metadata(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = python_nuitka_manifest()
        data["builder"]["binaries"] = [
            dict(data["builder"]["binaries"][0], sourceName="rlm-bsl-index", module="rlm_tools_bsl.cli", assetBase="rlm-bsl-index"),
            dict(data["builder"]["binaries"][0], sourceName="rlm-tools-bsl", module="rlm_tools_bsl.server", assetBase="rlm-bsl-mcp"),
        ]
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source = PreparedSource(root / "source", manifest.source.commit, "b" * 40, ())
        payload = root / "payload"
        payload.mkdir()
        for name in ("rlm-bsl-index", "rlm-bsl-mcp"):
            executable = payload / name
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        archive = root / "out" / "rlm-tools-bsl-darwin-arm64.tar.gz"
        write_runtime_archive(
            archive_path=archive,
            payload_root=payload,
            release_tag=release_tag(manifest),
            source={"ref": manifest.source.ref, "commit": source.commit, "tree": source.tree, "patches": []},
            target={"key": "darwin-arm64", "triple": manifest.targets["darwin-arm64"].target_triple},
            entrypoints={"rlm-bsl-index": "rlm-bsl-index", "rlm-bsl-mcp": "rlm-bsl-mcp"},
            builder={"kind": "tampered-builder"},
        )
        observed = {
            "kind": "python-nuitka-standalone",
            "python": "3.12.10",
            "uv": "0.11.29",
            "nuitka": "4.1.3",
            "compiler": {"cCompiler": "Clang", "ccName": "clang", "compiler": "clang"},
        }
        module = load_script()
        metadata = Mock()

        with (
            patch.object(module, "_prepare", return_value=source),
            patch.object(
                module,
                "build_python_nuitka_standalone",
                return_value=NuitkaBuildResult((archive,), observed),
                create=True,
            ),
            patch.object(module, "write_target_metadata", metadata),
        ):
            with self.assertRaisesRegex(SystemExit, "archive builder identity mismatch"):
                module.build(manifest, root, "darwin-arm64", root / "work", root / "out")

        metadata.assert_not_called()

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
