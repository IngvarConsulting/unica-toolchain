from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_rlm.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_rlm", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildRlmContractTests(unittest.TestCase):
    def test_build_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), f"missing build script: {SCRIPT}")

    def test_manifest_defines_reproducible_release_identity(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "rlm.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "name": "rlm-tools-bsl",
                        "version": "1.26.0",
                        "upstreamRepository": "https://github.com/Dach-Coin/rlm-tools-bsl",
                        "upstreamTag": "v1.26.0",
                        "upstreamCommit": "dcfff95ce678f49971b14d8acd82b042a6855470",
                        "pythonVersion": "3.12.10",
                        "uvVersion": "0.11.29",
                        "pyinstallerVersion": "6.21.0",
                        "buildRevision": 1,
                    }
                ),
                encoding="utf-8",
            )

            manifest = module.load_manifest(manifest_path)

        self.assertEqual(module.release_tag(manifest), "rlm-tools-bsl-v1.26.0-build.1")

    def test_expected_assets_cover_both_entrypoints_on_all_targets(self) -> None:
        module = load_module()
        self.assertEqual(
            module.expected_asset_names(),
            {
                "rlm-tools-bsl-darwin-arm64",
                "rlm-bsl-index-darwin-arm64",
                "rlm-tools-bsl-linux-x64",
                "rlm-bsl-index-linux-x64",
                "rlm-tools-bsl-win-x64.exe",
                "rlm-bsl-index-win-x64.exe",
            },
        )

    def test_source_commit_must_match_manifest(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "source"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "config", "user.name", "CI"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=repo, check=True)
            (repo / "marker.txt").write_text("pinned\n", encoding="utf-8")
            subprocess.run(["git", "add", "marker.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "pinned"], cwd=repo, check=True, stdout=subprocess.PIPE)
            actual = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            module.verify_source_commit(repo, actual)
            with self.assertRaisesRegex(SystemExit, "expected deadbeef"):
                module.verify_source_commit(repo, "deadbeef")

    def test_provenance_records_builder_and_asset_checksums(self) -> None:
        module = load_module()
        manifest = {
            "schemaVersion": 1,
            "name": "rlm-tools-bsl",
            "version": "1.26.0",
            "upstreamRepository": "https://github.com/Dach-Coin/rlm-tools-bsl",
            "upstreamTag": "v1.26.0",
            "upstreamCommit": "dcfff95ce678f49971b14d8acd82b042a6855470",
            "pythonVersion": "3.12.10",
            "uvVersion": "0.11.29",
            "pyinstallerVersion": "6.21.0",
            "buildRevision": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = out_dir / "rlm-tools-bsl-linux-x64"
            second = out_dir / "rlm-bsl-index-linux-x64"
            first.write_bytes(b"server")
            second.write_bytes(b"index")

            provenance_path, checksums_path = module.write_release_metadata(
                manifest=manifest,
                target="linux-x64",
                assets=[first, second],
                out_dir=out_dir,
            )

            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            checksums = checksums_path.read_text(encoding="utf-8")
            checksums_bytes = checksums_path.read_bytes()

        self.assertEqual(provenance["releaseTag"], "rlm-tools-bsl-v1.26.0-build.1")
        self.assertEqual(provenance["source"]["commit"], manifest["upstreamCommit"])
        self.assertEqual(provenance["builder"]["python"], "3.12.10")
        self.assertEqual(provenance["builder"]["uv"], "0.11.29")
        self.assertEqual(provenance["builder"]["pyinstaller"], "6.21.0")
        assets_by_name = {asset["name"]: asset for asset in provenance["assets"]}
        self.assertEqual(
            assets_by_name["rlm-tools-bsl-linux-x64"]["sha256"],
            hashlib.sha256(b"server").hexdigest(),
        )
        self.assertIn(hashlib.sha256(b"index").hexdigest(), checksums)
        self.assertNotIn(b"\r\n", checksums_bytes)

    def test_builder_versions_must_match_manifest(self) -> None:
        module = load_module()
        manifest = {
            "pythonVersion": "3.12.10",
            "uvVersion": "0.11.29",
            "pyinstallerVersion": "6.21.0",
        }
        module.verify_builder_identity(
            manifest,
            python_version="3.12.10",
            uv_version="0.11.29",
            pyinstaller_version="6.21.0",
        )
        with self.assertRaisesRegex(SystemExit, "Python 3.12.13, expected 3.12.10"):
            module.verify_builder_identity(
                manifest,
                python_version="3.12.13",
                uv_version="0.11.29",
                pyinstaller_version="6.21.0",
            )

    def test_uv_version_parser_accepts_platform_build_metadata(self) -> None:
        module = load_module()
        outputs = (
            "uv 0.11.29 (x86_64-unknown-linux-gnu)",
            "uv 0.11.29 (901092ee1 2026-07-15 aarch64-apple-darwin)",
            "uv 0.11.29 (901092ee1 2026-07-15 x86_64-pc-windows-msvc)",
        )
        for output in outputs:
            with self.subTest(output=output):
                self.assertEqual(module.parse_uv_version(output), "0.11.29")


if __name__ == "__main__":
    unittest.main()
