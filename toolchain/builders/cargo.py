from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from toolchain.manifest import CargoBuilderSpec, ToolManifest
from toolchain.source import PreparedSource


Runner = Callable[..., str]
RUST_VERSION = re.compile(r"^rustc ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def verify_rust_identity(expected: str, output: str) -> None:
    match = RUST_VERSION.match(output.strip())
    if match is None:
        raise SystemExit(f"cannot parse rustc version: {output.strip()}")
    actual = match.group(1)
    if actual != expected:
        raise SystemExit(f"builder uses Rust {actual}, expected {expected}")


def build_cargo(
    manifest: ToolManifest,
    target_key: str,
    source: PreparedSource,
    out_dir: Path,
    work_dir: Path,
    *,
    runner: Runner = run_command,
) -> list[Path]:
    if not isinstance(manifest.builder, CargoBuilderSpec):
        raise SystemExit(f"{manifest.name} is not a Cargo tool")
    if target_key not in manifest.targets:
        raise SystemExit(f"unknown target {target_key}")
    target = manifest.targets[target_key]
    verify_rust_identity(
        manifest.builder.rust_version,
        runner(["rustc", "--version"], cwd=None, env=None),
    )
    target_dir = work_dir / "cargo-target"
    out_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(target.environment)
    assets: list[Path] = []
    for binary in manifest.builder.binaries:
        runner(
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--package",
                binary.package,
                "--bin",
                binary.source_name,
                "--target",
                target.target_triple,
                "--target-dir",
                str(target_dir),
            ],
            cwd=source.path,
            env=environment,
        )
        produced = target_dir / target.target_triple / "release" / f"{binary.source_name}{target.exe}"
        if not produced.is_file():
            raise SystemExit(f"Cargo output not found: {produced}")
        destination = out_dir / f"{binary.asset_base}-{target_key}{target.exe}"
        shutil.copy2(produced, destination)
        if not destination.name.endswith(".exe"):
            destination.chmod(destination.stat().st_mode | 0o755)
        assets.append(destination)
    return assets
