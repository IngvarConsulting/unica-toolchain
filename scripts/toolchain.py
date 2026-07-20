#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from toolchain.builders.cargo import build_cargo  # noqa: E402
from toolchain.builders.python_pyinstaller import build_python_pyinstaller  # noqa: E402
from toolchain.manifest import (  # noqa: E402
    CargoBuilderSpec,
    PythonBuilderSpec,
    ToolManifest,
    expected_release_files,
    load_manifest,
    release_tag,
)
from toolchain.provenance import write_target_metadata  # noqa: E402
from toolchain.source import (  # noqa: E402
    checkout_source,
    copy_license_assets,
    prepare_source,
)


def builder_versions(manifest: ToolManifest) -> dict[str, str]:
    if isinstance(manifest.builder, CargoBuilderSpec):
        return {"rust": manifest.builder.rust_version}
    if isinstance(manifest.builder, PythonBuilderSpec):
        return {
            "python": manifest.builder.python_version,
            "uv": manifest.builder.uv_version,
            "pyinstaller": manifest.builder.pyinstaller_version,
        }
    raise SystemExit(f"unsupported builder: {manifest.builder}")


def describe(manifest: ToolManifest) -> dict:
    matrix = {
        "include": [
            {
                "target": key,
                "runner": target.runner,
                "targetTriple": target.target_triple,
                "systemSetup": target.system_setup,
            }
            for key, target in manifest.targets.items()
        ]
    }
    return {
        "tool": manifest.name,
        "releaseTag": release_tag(manifest),
        "builderKind": manifest.builder.kind,
        "builderVersions": builder_versions(manifest),
        "matrix": matrix,
        "expectedReleaseFiles": sorted(expected_release_files(manifest)),
    }


def _prepare(manifest: ToolManifest, repo_root: Path, work_dir: Path):
    source_dir = work_dir / "source"
    checkout_source(manifest, source_dir)
    return prepare_source(manifest, repo_root, source_dir)


def validate_source(
    manifest: ToolManifest,
    repo_root: Path,
    work_dir: Path,
    out_dir: Path,
) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
    prepared = _prepare(manifest, repo_root, work_dir)
    copied = copy_license_assets(manifest, prepared, out_dir)
    print(
        json.dumps(
            {
                "commit": prepared.commit,
                "tree": prepared.tree,
                "patches": [patch.path for patch in prepared.patches],
                "licenses": [path.name for path in copied],
            }
        )
    )


def _smoke(manifest: ToolManifest, target_key: str, assets: list[Path]) -> None:
    by_name = {path.name: path for path in assets}
    target = manifest.targets[target_key]
    for binary in manifest.builder.binaries:
        name = f"{binary.asset_base}-{target_key}{target.exe}"
        subprocess.run([str(by_name[name]), *binary.smoke_args], check=True)


def build(
    manifest: ToolManifest,
    repo_root: Path,
    target_key: str,
    work_dir: Path,
    out_dir: Path,
) -> None:
    if target_key not in manifest.targets:
        raise SystemExit(f"unknown target {target_key}")
    shutil.rmtree(work_dir, ignore_errors=True)
    prepared = _prepare(manifest, repo_root, work_dir)
    if isinstance(manifest.builder, CargoBuilderSpec):
        assets = build_cargo(manifest, target_key, prepared, out_dir, work_dir)
    elif isinstance(manifest.builder, PythonBuilderSpec):
        assets = build_python_pyinstaller(manifest, target_key, prepared, out_dir, work_dir)
    else:
        raise SystemExit(f"unsupported builder: {manifest.builder}")
    _smoke(manifest, target_key, assets)
    write_target_metadata(
        manifest,
        target_key,
        prepared,
        assets,
        out_dir,
        builder_identity={"kind": manifest.builder.kind, **builder_versions(manifest)},
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    describe_parser = subparsers.add_parser("describe")
    describe_parser.add_argument("--manifest", type=Path, required=True)

    for name in ("validate-source", "build"):
        command = subparsers.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--repo-root", type=Path, default=REPO_ROOT)
        command.add_argument("--work-dir", type=Path, required=True)
        command.add_argument("--out-dir", type=Path, required=True)
        if name == "build":
            command.add_argument("--target", required=True)
    return parser


def main() -> None:
    args = make_parser().parse_args()
    manifest = load_manifest(args.manifest)
    if args.command == "describe":
        print(json.dumps(describe(manifest), separators=(",", ":")))
        return
    if args.command == "validate-source":
        validate_source(
            manifest,
            args.repo_root.resolve(),
            args.work_dir.resolve(),
            args.out_dir.resolve(),
        )
        return
    if args.command == "build":
        build(
            manifest,
            args.repo_root.resolve(),
            args.target,
            args.work_dir.resolve(),
            args.out_dir.resolve(),
        )
        return
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
