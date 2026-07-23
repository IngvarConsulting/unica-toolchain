from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from toolchain.manifest import ToolManifest, release_tag
from toolchain.source import PreparedSource, sha256


def write_target_metadata(
    manifest: ToolManifest,
    target_key: str,
    source: PreparedSource,
    assets: list[Path],
    out_dir: Path,
    *,
    builder_identity: dict[str, Any],
) -> tuple[Path, Path]:
    if target_key not in manifest.targets:
        raise SystemExit(f"unknown target {target_key}")
    target = manifest.targets[target_key]
    expected = {
        f"{binary.asset_base}-{target_key}{target.exe}" for binary in manifest.builder.binaries
    }
    actual = {asset.name for asset in assets}
    if actual != expected:
        raise SystemExit(
            f"target asset mismatch for {manifest.name} {target_key}: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )
    entries = [
        {"name": asset.name, "sha256": sha256(asset), "size": asset.stat().st_size}
        for asset in sorted(assets, key=lambda path: path.name)
    ]
    provenance = {
        "schemaVersion": 3,
        "releaseTag": release_tag(manifest),
        "source": {
            "kind": manifest.source.kind,
            "repository": manifest.source.repository,
            "ref": manifest.source.ref,
            "commit": source.commit,
            "tree": source.tree,
            "patches": [
                {"path": patch.path, "sha256": patch.sha256} for patch in source.patches
            ],
        },
        "builder": builder_identity,
        "target": {
            "key": target_key,
            "triple": target.target_triple,
            "systemSetup": target.system_setup,
            "environment": target.environment,
        },
        "assets": entries,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    provenance_path = out_dir / f"provenance-{manifest.name}-{target_key}.json"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksums_path = out_dir / f"checksums-{manifest.name}-{target_key}.txt"
    checksums_path.write_text(
        "".join(f"{entry['sha256']}  {entry['name']}\n" for entry in entries),
        encoding="utf-8",
        newline="\n",
    )
    return provenance_path, checksums_path
