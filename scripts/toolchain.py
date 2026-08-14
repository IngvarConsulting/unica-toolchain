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
from toolchain.builders.python_pyinstaller import (  # noqa: E402
    WINDOWS_STDIO_POLICY,
    build_python_pyinstaller,
)
from toolchain.builders.python_nuitka_standalone import (  # noqa: E402
    build_python_nuitka_standalone,
)
from toolchain.manifest import (  # noqa: E402
    CargoBuilderSpec,
    PythonBuilderSpec,
    PythonNuitkaStandaloneSpec,
    ToolManifest,
    expected_release_files,
    load_manifest,
    release_tag,
)
from toolchain.provenance import write_target_metadata  # noqa: E402
from toolchain.runtime_archive import (  # noqa: E402
    materialize_runtime_archive,
    validate_runtime_archive,
)
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
    if isinstance(manifest.builder, PythonNuitkaStandaloneSpec):
        return {
            "python": manifest.builder.python_version,
            "uv": manifest.builder.uv_version,
            "nuitka": manifest.builder.nuitka_version,
        }
    raise SystemExit(f"unsupported builder: {manifest.builder}")


def builder_identity(
    manifest: ToolManifest,
    target_key: str,
    observed: dict | None = None,
) -> dict:
    if isinstance(manifest.builder, PythonNuitkaStandaloneSpec):
        if observed is None:
            raise SystemExit("Nuitka builder identity must come from the build report")
        expected = {"kind": manifest.builder.kind, **builder_versions(manifest)}
        for key, value in expected.items():
            if observed.get(key) != value:
                raise SystemExit(
                    f"observed Nuitka builder {key} is {observed.get(key)}, expected {value}"
                )
        compiler = observed.get("compiler")
        if not isinstance(compiler, dict) or not compiler:
            raise SystemExit("observed Nuitka builder is missing compiler identity")
        return observed
    identity: dict = {"kind": manifest.builder.kind, **builder_versions(manifest)}
    if isinstance(manifest.builder, PythonBuilderSpec) and target_key == "win-x64":
        identity["stdio"] = dict(WINDOWS_STDIO_POLICY)
    return identity


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


def _write_utf8_stdout(value: str) -> None:
    end = "" if value.endswith("\n") else "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        print(value, end=end)
        return
    buffer.write((value + end).encode("utf-8"))
    buffer.flush()


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


def _smoke(
    manifest: ToolManifest,
    target_key: str,
    assets: list[Path],
    *,
    smoke_root: Path | None = None,
    expected_builder_identity: dict | None = None,
) -> None:
    target = manifest.targets[target_key]
    if isinstance(manifest.builder, PythonNuitkaStandaloneSpec):
        if len(assets) != 1:
            raise SystemExit(
                f"Nuitka build must produce one archive, got {[path.name for path in assets]}"
            )
        if smoke_root is None:
            raise SystemExit("Nuitka archive smoke requires an isolated destination")
        expected_entrypoints = {
            binary.asset_base: f"{binary.asset_base}{target.exe}"
            for binary in manifest.builder.binaries
        }
        validated = validate_runtime_archive(
            assets[0],
            expected_release_tag=release_tag(manifest),
            expected_source_ref=manifest.source.ref,
            expected_source_commit=manifest.source.commit,
            expected_target_key=target_key,
            expected_target_triple=target.target_triple,
            expected_entrypoints=expected_entrypoints,
        )
        if validated.manifest.builder != expected_builder_identity:
            raise SystemExit(
                "archive builder identity mismatch: "
                f"{validated.manifest.builder} != {expected_builder_identity}"
            )
        materialized = materialize_runtime_archive(validated, smoke_root)
        by_base = {
            name: materialized[relative]
            for name, relative in validated.manifest.entrypoints.items()
        }
    else:
        by_name = {path.name: path for path in assets}
        by_base = {
            binary.asset_base: by_name[
                f"{binary.asset_base}-{target_key}{target.exe}"
            ]
            for binary in manifest.builder.binaries
        }
    for binary in manifest.builder.binaries:
        executable = by_base[binary.asset_base]
        name = executable.name
        if not binary.smoke_checks:
            subprocess.run([str(executable), *binary.smoke_args], check=True)
            continue
        checks = [(binary.smoke_args, ())]
        checks.extend(
            (check.args, check.expected_output) for check in binary.smoke_checks
        )
        for args, expected_output in checks:
            command = [str(executable), *args]
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout = result.stdout.decode("utf-8")
                stderr = result.stderr.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SystemExit(
                    f"smoke output is not UTF-8 for {name} {' '.join(args)}: {exc}"
                ) from exc
            if stdout:
                _write_utf8_stdout(stdout)
            if stderr:
                _write_utf8_stdout(stderr)
            label = f"{name} {' '.join(args)}".rstrip()
            if result.returncode != 0:
                raise SystemExit(f"smoke failed: {label} exited {result.returncode}")
            output = stdout + stderr
            missing = [expected for expected in expected_output if expected not in output]
            if missing:
                raise SystemExit(
                    f"smoke output mismatch for {label}: missing {missing!r}"
                )
            print(f"smoke passed: {label}")


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
        observed_identity = None
    elif isinstance(manifest.builder, PythonBuilderSpec):
        assets = build_python_pyinstaller(manifest, target_key, prepared, out_dir, work_dir)
        observed_identity = None
    elif isinstance(manifest.builder, PythonNuitkaStandaloneSpec):
        result = build_python_nuitka_standalone(
            manifest, target_key, prepared, out_dir, work_dir
        )
        assets = list(result.assets)
        observed_identity = result.builder_identity
    else:
        raise SystemExit(f"unsupported builder: {manifest.builder}")
    _smoke(
        manifest,
        target_key,
        assets,
        smoke_root=(work_dir / "smoke-runtime")
        if isinstance(manifest.builder, PythonNuitkaStandaloneSpec)
        else None,
        expected_builder_identity=observed_identity,
    )
    write_target_metadata(
        manifest,
        target_key,
        prepared,
        assets,
        out_dir,
        builder_identity=builder_identity(
            manifest, target_key, observed=observed_identity
        ),
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
