from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path

from toolchain.manifest import (
    CargoBuilderSpec,
    PythonBuilderSpec,
    expected_asset_names,
    expected_release_files,
    load_manifest,
    release_tag,
)


TARGETS = {
    "darwin-arm64": {
        "runner": "macos-14",
        "targetTriple": "aarch64-apple-darwin",
        "exe": "",
        "systemSetup": "none",
        "environment": {},
    },
    "linux-x64": {
        "runner": "ubuntu-latest",
        "targetTriple": "x86_64-unknown-linux-musl",
        "exe": "",
        "systemSetup": "musl-tools",
        "environment": {},
    },
    "win-x64": {
        "runner": "windows-latest",
        "targetTriple": "x86_64-pc-windows-msvc",
        "exe": ".exe",
        "systemSetup": "none",
        "environment": {},
    },
}


def cargo_manifest() -> dict:
    return {
        "schemaVersion": 2,
        "name": "v8-runner",
        "version": "0.5.1",
        "buildRevision": 1,
        "source": {
            "repository": "https://github.com/alkoleft/v8-runner-rust",
            "tag": "v0.5.1",
            "commit": "a" * 40,
        },
        "license": {
            "spdx": "MIT",
            "files": [{"path": "LICENSE", "assetName": "license-v8-runner.txt"}],
        },
        "patches": [],
        "builder": {
            "kind": "cargo",
            "rustVersion": "1.95.0",
            "locked": True,
            "binaries": [
                {
                    "package": "v8-runner",
                    "sourceName": "v8-runner",
                    "assetBase": "v8-runner",
                    "smokeArgs": ["--help"],
                }
            ],
        },
        "targets": copy.deepcopy(TARGETS),
    }


def python_manifest() -> dict:
    data = cargo_manifest()
    data["name"] = "rlm-tools-bsl"
    data["builder"] = {
        "kind": "python-pyinstaller",
        "pythonVersion": "3.12.10",
        "uvVersion": "0.11.29",
        "pyinstallerVersion": "6.21.0",
        "lockFile": "uv.lock",
        "collectAll": "rlm_tools_bsl",
        "binaries": [
            {
                "package": "rlm_tools_bsl",
                "sourceName": "rlm-tools-bsl",
                "module": "rlm_tools_bsl.server",
                "assetBase": "rlm-tools-bsl",
                "smokeArgs": ["--help"],
            }
        ],
    }
    return data


class ManifestTests(unittest.TestCase):
    def write_manifest(self, data: dict) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / "tool.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_loads_cargo_manifest_and_generates_independent_assets(self) -> None:
        manifest = load_manifest(self.write_manifest(cargo_manifest()))

        self.assertIsInstance(manifest.builder, CargoBuilderSpec)
        self.assertEqual(manifest.name, "v8-runner")
        self.assertEqual(release_tag(manifest), "v8-runner-v0.5.1-build.1")
        self.assertEqual(
            expected_asset_names(manifest),
            {
                "v8-runner-darwin-arm64",
                "v8-runner-linux-x64",
                "v8-runner-win-x64.exe",
            },
        )
        self.assertEqual(
            expected_release_files(manifest) - expected_asset_names(manifest),
            {
                "license-v8-runner.txt",
                "checksums-v8-runner-darwin-arm64.txt",
                "checksums-v8-runner-linux-x64.txt",
                "checksums-v8-runner-win-x64.txt",
                "provenance-v8-runner-darwin-arm64.json",
                "provenance-v8-runner-linux-x64.json",
                "provenance-v8-runner-win-x64.json",
            },
        )

    def test_loads_python_builder(self) -> None:
        manifest = load_manifest(self.write_manifest(python_manifest()))

        self.assertIsInstance(manifest.builder, PythonBuilderSpec)
        self.assertEqual(manifest.builder.python_version, "3.12.10")
        self.assertEqual(manifest.builder.binaries[0].module, "rlm_tools_bsl.server")

    def test_rejects_invalid_contracts(self) -> None:
        cases = []
        schema = cargo_manifest()
        schema["schemaVersion"] = 1
        cases.append((schema, "schemaVersion"))

        builder = cargo_manifest()
        builder["builder"]["kind"] = "shell"
        cases.append((builder, "unsupported builder"))

        commit = cargo_manifest()
        commit["source"]["commit"] = "deadbeef"
        cases.append((commit, "40 lowercase hexadecimal"))

        target = cargo_manifest()
        del target["targets"]["win-x64"]
        cases.append((target, "targets must be exactly"))

        license_files = cargo_manifest()
        license_files["license"]["files"] = []
        cases.append((license_files, "license.files"))

        asset = cargo_manifest()
        asset["builder"]["binaries"][0]["assetBase"] = "bin/v8-runner"
        cases.append((asset, "assetBase"))

        setup = cargo_manifest()
        setup["targets"]["linux-x64"]["systemSetup"] = "apt-anything"
        cases.append((setup, "systemSetup"))

        unknown = cargo_manifest()
        unknown["surprise"] = True
        cases.append((unknown, "unknown fields"))

        for data, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SystemExit, message):
                    load_manifest(self.write_manifest(data))


if __name__ == "__main__":
    unittest.main()
