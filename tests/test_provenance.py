from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_manifest import cargo_manifest
from toolchain.manifest import load_manifest
from toolchain.provenance import write_target_metadata
from toolchain.source import AppliedPatch, PreparedSource


class ProvenanceTests(unittest.TestCase):
    def test_records_source_patches_builder_target_and_assets_with_lf(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(cargo_manifest()), encoding="utf-8")
        manifest = load_manifest(manifest_path)
        source = PreparedSource(
            root / "source",
            "a" * 40,
            "b" * 40,
            (AppliedPatch("patches/v8-runner/0001.patch", "c" * 64),),
        )
        out_dir = root / "out"
        out_dir.mkdir()
        asset = out_dir / "v8-runner-linux-x64"
        asset.write_bytes(b"native")

        provenance_path, checksums_path = write_target_metadata(
            manifest,
            "linux-x64",
            source,
            [asset],
            out_dir,
            builder_identity={"kind": "cargo", "rust": "1.95.0"},
        )

        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(provenance["schemaVersion"], 3)
        self.assertEqual(provenance["releaseTag"], "v8-runner-v0.5.1-build.1")
        self.assertEqual(provenance["source"]["kind"], "release")
        self.assertEqual(provenance["source"]["ref"], "v0.5.1")
        self.assertEqual(provenance["source"]["commit"], "a" * 40)
        self.assertEqual(provenance["source"]["tree"], "b" * 40)
        self.assertEqual(provenance["source"]["patches"][0]["sha256"], "c" * 64)
        self.assertEqual(provenance["builder"], {"kind": "cargo", "rust": "1.95.0"})
        self.assertEqual(provenance["target"]["triple"], "x86_64-unknown-linux-musl")
        self.assertEqual(provenance["target"]["systemSetup"], "musl-tools")
        self.assertEqual(
            provenance["assets"][0]["sha256"], hashlib.sha256(b"native").hexdigest()
        )
        self.assertNotIn(b"\r\n", provenance_path.read_bytes())
        self.assertNotIn(b"\r\n", checksums_path.read_bytes())
        self.assertIn(asset.name.encode(), checksums_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
