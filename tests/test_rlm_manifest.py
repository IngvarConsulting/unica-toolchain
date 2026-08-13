from pathlib import Path
import unittest

from toolchain.manifest import (
    PythonBuilderSpec,
    expected_asset_names,
    expected_release_files,
    load_manifest,
    release_tag,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class RlmManifestTests(unittest.TestCase):
    def test_v1_33_release_keeps_upstream_entrypoints_and_renames_assets(self) -> None:
        manifest = load_manifest(REPO_ROOT / "manifests" / "rlm-tools-bsl.json")

        self.assertIsInstance(manifest.builder, PythonBuilderSpec)
        self.assertEqual(manifest.name, "rlm-tools-bsl")
        self.assertEqual(manifest.version, "1.33.0")
        self.assertEqual(manifest.build_revision, 1)
        self.assertEqual(manifest.source.kind, "release")
        self.assertEqual(manifest.source.ref, "v1.33.0")
        self.assertEqual(
            manifest.source.commit,
            "3e6920cd015a61af4ba7aa1a5f1fedd8bc935549",
        )
        self.assertEqual(release_tag(manifest), "rlm-tools-bsl-v1.33.0-build.1")

        self.assertEqual(
            [
                (binary.source_name, binary.asset_base, binary.package, binary.module)
                for binary in manifest.builder.binaries
            ],
            [
                ("rlm-tools-bsl", "rlm-bsl-mcp", "rlm_tools_bsl", "rlm_tools_bsl.server"),
                ("rlm-bsl-index", "rlm-bsl-index", "rlm_tools_bsl", "rlm_tools_bsl.cli"),
            ],
        )
        self.assertEqual(
            expected_asset_names(manifest),
            {
                "rlm-bsl-mcp-darwin-arm64",
                "rlm-bsl-mcp-linux-x64",
                "rlm-bsl-mcp-win-x64.exe",
                "rlm-bsl-index-darwin-arm64",
                "rlm-bsl-index-linux-x64",
                "rlm-bsl-index-win-x64.exe",
            },
        )
        self.assertEqual(
            expected_release_files(manifest) - expected_asset_names(manifest),
            {
                "license-rlm-tools-bsl-MIT.txt",
                "checksums-rlm-tools-bsl-darwin-arm64.txt",
                "checksums-rlm-tools-bsl-linux-x64.txt",
                "checksums-rlm-tools-bsl-win-x64.txt",
                "provenance-rlm-tools-bsl-darwin-arm64.json",
                "provenance-rlm-tools-bsl-linux-x64.json",
                "provenance-rlm-tools-bsl-win-x64.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
