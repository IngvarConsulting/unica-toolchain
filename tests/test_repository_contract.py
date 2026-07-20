from __future__ import annotations

import unittest
from pathlib import Path

from toolchain.manifest import (
    CargoBuilderSpec,
    PythonBuilderSpec,
    expected_asset_names,
    load_manifest,
    release_tag,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = REPO_ROOT / "manifests"


class RepositoryContractTests(unittest.TestCase):
    def load(self, name: str):
        return load_manifest(MANIFESTS / f"{name}.json")

    def test_checked_in_tools_have_independent_release_identities(self) -> None:
        expected = {
            "rlm-tools-bsl": (
                "v1.26.0",
                "dcfff95ce678f49971b14d8acd82b042a6855470",
                "rlm-tools-bsl-v1.26.0-build.3",
            ),
            "bsl-analyzer": (
                "v0.2.55",
                "5a02bb44dedaf29e0e29af1f740279d279199854",
                "bsl-analyzer-v0.2.55-build.1",
            ),
            "v8-runner": (
                "v0.5.1",
                "ad72f64222ab0a7e6dfd391adb437a956c0a2428",
                "v8-runner-v0.5.1-build.1",
            ),
        }
        for name, (tag, commit, release) in expected.items():
            with self.subTest(name=name):
                manifest = self.load(name)
                self.assertEqual(manifest.source.tag, tag)
                self.assertEqual(manifest.source.commit, commit)
                self.assertEqual(release_tag(manifest), release)
                self.assertEqual(manifest.patches, ())
                license_names = [item.asset_name for item in manifest.license.files]
                self.assertEqual(len(license_names), len(set(license_names)))

    def test_cargo_tools_pin_rust_and_normalize_assets(self) -> None:
        analyzer = self.load("bsl-analyzer")
        runner = self.load("v8-runner")

        self.assertIsInstance(analyzer.builder, CargoBuilderSpec)
        self.assertIsInstance(runner.builder, CargoBuilderSpec)
        self.assertEqual(analyzer.builder.rust_version, "1.95.0")
        self.assertEqual(runner.builder.rust_version, "1.95.0")
        self.assertEqual(analyzer.builder.binaries[0].source_name, "bsl-analyzer-app")
        self.assertEqual(analyzer.builder.binaries[0].asset_base, "bsl-analyzer")
        self.assertEqual(
            analyzer.targets["win-x64"].environment,
            {
                "CXXFLAGS": "/DMAP_FAILED=((void*)-1)",
                "RUSTFLAGS": "-C target-feature=+crt-static",
            },
        )
        self.assertEqual(runner.targets["linux-x64"].system_setup, "musl-tools")
        self.assertEqual(
            expected_asset_names(runner),
            {
                "v8-runner-darwin-arm64",
                "v8-runner-linux-x64",
                "v8-runner-win-x64.exe",
            },
        )

    def test_rlm_pins_python_builder_and_both_entrypoints(self) -> None:
        manifest = self.load("rlm-tools-bsl")

        self.assertIsInstance(manifest.builder, PythonBuilderSpec)
        self.assertEqual(manifest.builder.python_version, "3.12.10")
        self.assertEqual(manifest.builder.uv_version, "0.11.29")
        self.assertEqual(manifest.builder.pyinstaller_version, "6.21.0")
        self.assertEqual(
            [binary.module for binary in manifest.builder.binaries],
            ["rlm_tools_bsl.server", "rlm_tools_bsl.cli"],
        )
        self.assertEqual(len(expected_asset_names(manifest)), 6)

    def test_rlm_only_implementation_is_removed(self) -> None:
        self.assertFalse((REPO_ROOT / "scripts" / "build_rlm.py").exists())
        self.assertFalse((REPO_ROOT / "tests" / "test_build_rlm.py").exists())

    def test_one_generic_release_workflow_builds_only_on_dispatch(self) -> None:
        workflow = REPO_ROOT / ".github" / "workflows" / "release-tool.yml"
        self.assertTrue(workflow.is_file())
        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "release-rlm.yml").exists())
        text = workflow.read_text(encoding="utf-8")
        trigger = text.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger)
        self.assertIn("tool:", trigger)
        self.assertNotIn("pull_request:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertIn("scripts/toolchain.py describe", text)
        self.assertIn("scripts/toolchain.py validate-source", text)
        self.assertIn("scripts/toolchain.py build", text)
        self.assertIn("fromJSON(needs.metadata.outputs.matrix)", text)
        self.assertIn("gh release view", text)
        self.assertIn("refs/tags/", text)
        self.assertIn("expected_release_files", text)
        self.assertIn("actions/attest-build-provenance@v2", text)
        self.assertIn("softprops/action-gh-release@v3", text)
        self.assertIn("make_latest: false", text)

    def test_pull_request_ci_validates_sources_without_building_tools(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", text)
        self.assertIn("python -m py_compile scripts/*.py toolchain/*.py toolchain/builders/*.py tests/*.py", text)
        self.assertIn("scripts/toolchain.py validate-source", text)
        self.assertIn("manifests/*.json", text)
        self.assertIn("rhysd/actionlint:1.7.7", text)
        self.assertNotIn("scripts/toolchain.py build", text)


if __name__ == "__main__":
    unittest.main()
