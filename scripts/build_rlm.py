#!/usr/bin/env python3
"""Build pinned native RLM entrypoints for Unica releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import entry_points, version
from pathlib import Path
from typing import Any


TARGETS = {
    "darwin-arm64": "",
    "linux-x64": "",
    "win-x64": ".exe",
}
ENTRYPOINTS = ("rlm-tools-bsl", "rlm-bsl-index")
ENTRYPOINT_MODULES = {
    "rlm-tools-bsl": "rlm_tools_bsl.server",
    "rlm-bsl-index": "rlm_tools_bsl.cli",
}
REQUIRED_MANIFEST_FIELDS = {
    "name": str,
    "version": str,
    "upstreamRepository": str,
    "upstreamTag": str,
    "upstreamCommit": str,
    "pythonVersion": str,
    "uvVersion": str,
    "pyinstallerVersion": str,
    "buildRevision": int,
}


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1:
        raise SystemExit(f"unsupported manifest schemaVersion: {manifest.get('schemaVersion')}")
    for field, expected_type in REQUIRED_MANIFEST_FIELDS.items():
        value = manifest.get(field)
        if not isinstance(value, expected_type) or value == "":
            raise SystemExit(f"manifest field {field} must be a non-empty {expected_type.__name__}")
    return manifest


def release_tag(manifest: dict[str, Any]) -> str:
    return f"{manifest['name']}-v{manifest['version']}-build.{manifest['buildRevision']}"


def expected_asset_names() -> set[str]:
    return {
        f"{entrypoint}-{target}{suffix}"
        for target, suffix in TARGETS.items()
        for entrypoint in ENTRYPOINTS
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source_commit(source_dir: Path, expected_commit: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    actual = result.stdout.strip()
    if actual != expected_commit:
        raise SystemExit(f"source checkout is {actual}, expected {expected_commit}")


def verify_builder_identity(
    manifest: dict[str, Any],
    *,
    python_version: str,
    uv_version: str,
    pyinstaller_version: str,
) -> None:
    checks = (
        ("Python", python_version, manifest["pythonVersion"]),
        ("uv", uv_version, manifest["uvVersion"]),
        ("PyInstaller", pyinstaller_version, manifest["pyinstallerVersion"]),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise SystemExit(f"builder uses {name} {actual}, expected {expected}")


def current_uv_version() -> str:
    result = subprocess.run(
        ["uv", "--version"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    parts = result.stdout.strip().split()
    if len(parts) != 2 or parts[0] != "uv":
        raise SystemExit(f"cannot parse uv version: {result.stdout.strip()}")
    return parts[1]


def resolve_entrypoint(command_name: str) -> tuple[str, str]:
    candidates = entry_points().select(group="console_scripts", name=command_name)
    matches = list(candidates)
    if len(matches) != 1:
        raise SystemExit(f"expected one installed console_scripts entrypoint for {command_name}")
    entrypoint = matches[0]
    if not entrypoint.attr:
        raise SystemExit(f"console_scripts entrypoint is not callable: {entrypoint.value}")
    return entrypoint.module, entrypoint.attr


def write_entrypoint_stub(path: Path, module: str, attr: str) -> None:
    path.write_text(
        "\n".join(
            [
                "import importlib",
                "import sys",
                "",
                f"MODULE = {module!r}",
                f"CALLABLE = {attr!r}",
                "",
                "def main():",
                "    obj = importlib.import_module(MODULE)",
                "    for part in CALLABLE.split('.'):",
                "        obj = getattr(obj, part)",
                "    return obj()",
                "",
                "if __name__ == '__main__':",
                "    sys.exit(main())",
                "",
            ]
        ),
        encoding="utf-8",
    )


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def build_entrypoint(
    *,
    command_name: str,
    target: str,
    out_dir: Path,
    work_dir: Path,
) -> Path:
    if target not in TARGETS:
        raise SystemExit(f"unsupported target {target}")
    module, attr = resolve_entrypoint(command_name)
    expected_module = ENTRYPOINT_MODULES[command_name]
    if module != expected_module:
        raise SystemExit(f"{command_name} resolves to {module}, expected {expected_module}")

    build_root = work_dir / command_name
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    stub = build_root / "entrypoint.py"
    write_entrypoint_stub(stub, module, attr)
    asset_name = f"{command_name}-{target}{TARGETS[target]}"
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--clean",
            "--noconfirm",
            "--name",
            asset_name,
            "--distpath",
            str(out_dir.resolve()),
            "--workpath",
            str((build_root / "build").resolve()),
            "--specpath",
            str((build_root / "spec").resolve()),
            "--collect-all",
            "rlm_tools_bsl",
            "--hidden-import",
            module,
            str(stub.resolve()),
        ],
        cwd=build_root,
    )
    asset = out_dir / asset_name
    if not asset.is_file():
        raise SystemExit(f"PyInstaller output not found: {asset}")
    if not asset.name.endswith(".exe"):
        asset.chmod(asset.stat().st_mode | 0o755)
    return asset


def write_release_metadata(
    *,
    manifest: dict[str, Any],
    target: str,
    assets: list[Path],
    out_dir: Path,
) -> tuple[Path, Path]:
    entries = [
        {"name": asset.name, "sha256": sha256(asset), "size": asset.stat().st_size}
        for asset in sorted(assets, key=lambda item: item.name)
    ]
    provenance = {
        "schemaVersion": 1,
        "releaseTag": release_tag(manifest),
        "target": target,
        "source": {
            "repository": manifest["upstreamRepository"],
            "tag": manifest["upstreamTag"],
            "commit": manifest["upstreamCommit"],
        },
        "builder": {
            "python": manifest["pythonVersion"],
            "uv": manifest["uvVersion"],
            "pyinstaller": manifest["pyinstallerVersion"],
        },
        "assets": entries,
    }
    provenance_path = out_dir / f"rlm-toolchain-{target}.json"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    checksums_path = out_dir / f"checksums-{target}.txt"
    checksums_path.write_text(
        "".join(f"{entry['sha256']}  {entry['name']}\n" for entry in entries),
        encoding="utf-8",
    )
    return provenance_path, checksums_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    verify_builder_identity(
        manifest,
        python_version=platform.python_version(),
        uv_version=current_uv_version(),
        pyinstaller_version=version("pyinstaller"),
    )
    source_dir = args.source_dir.resolve()
    verify_source_commit(source_dir, manifest["upstreamCommit"])
    os.environ.setdefault("PYTHONHASHSEED", "0")
    assets = [
        build_entrypoint(
            command_name=command_name,
            target=args.target,
            out_dir=args.out_dir,
            work_dir=args.work_dir,
        )
        for command_name in ENTRYPOINTS
    ]
    write_release_metadata(
        manifest=manifest,
        target=args.target,
        assets=assets,
        out_dir=args.out_dir,
    )


if __name__ == "__main__":
    main()
