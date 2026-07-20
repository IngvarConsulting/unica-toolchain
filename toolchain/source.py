from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from toolchain.manifest import ToolManifest


@dataclass(frozen=True)
class AppliedPatch:
    path: str
    sha256: str


@dataclass(frozen=True)
class PreparedSource:
    path: Path
    commit: str
    tree: str
    patches: tuple[AppliedPatch, ...]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(source_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail}") from exc
    return result.stdout.strip()


def verify_source_commit(source_dir: Path, expected_commit: str) -> None:
    actual = _git(source_dir, "rev-parse", "HEAD")
    if actual != expected_commit:
        raise SystemExit(f"source checkout is {actual}, expected {expected_commit}")


def checkout_source(manifest: ToolManifest, destination: Path) -> Path:
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(destination)], check=True)
    _git(destination, "remote", "add", "origin", manifest.source.repository)
    _git(destination, "fetch", "--depth", "1", "origin", manifest.source.tag)
    try:
        _git(destination, "checkout", "--detach", manifest.source.commit)
    except SystemExit as exc:
        raise SystemExit(
            f"upstream tag {manifest.source.tag} did not fetch commit {manifest.source.commit}"
        ) from exc
    verify_source_commit(destination, manifest.source.commit)
    return destination


def _resolve_patch(manifest: ToolManifest, repo_root: Path, relative_path: str) -> Path:
    if Path(relative_path).is_absolute():
        raise SystemExit(f"patch path must be inside patches/{manifest.name}: {relative_path}")
    patch_root = (repo_root / "patches" / manifest.name).resolve()
    patch_path = (repo_root / relative_path).resolve()
    try:
        patch_path.relative_to(patch_root)
    except ValueError as exc:
        raise SystemExit(f"patch must be inside patches/{manifest.name}: {relative_path}") from exc
    if not patch_path.is_file():
        raise SystemExit(f"patch not found: {relative_path}")
    return patch_path


def prepare_source(
    manifest: ToolManifest,
    repo_root: Path,
    source_dir: Path,
) -> PreparedSource:
    source_dir = source_dir.resolve()
    verify_source_commit(source_dir, manifest.source.commit)
    applied: list[AppliedPatch] = []
    for patch in manifest.patches:
        patch_path = _resolve_patch(manifest, repo_root.resolve(), patch.path)
        actual = sha256(patch_path)
        if actual != patch.sha256:
            raise SystemExit(
                f"patch checksum mismatch for {patch.path}: {actual} != {patch.sha256}"
            )
        try:
            _git(source_dir, "apply", "--check", str(patch_path))
        except SystemExit as exc:
            raise SystemExit(f"patch does not apply: {patch.path}") from exc
        _git(source_dir, "apply", str(patch_path))
        applied.append(AppliedPatch(patch.path, patch.sha256))

    _git(source_dir, "add", "-A")
    tree = _git(source_dir, "write-tree")
    return PreparedSource(source_dir, manifest.source.commit, tree, tuple(applied))


def copy_license_assets(
    manifest: ToolManifest,
    source: PreparedSource,
    out_dir: Path,
) -> list[Path]:
    source_root = source.path.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for license_file in manifest.license.files:
        candidate = (source_root / license_file.path).resolve()
        try:
            candidate.relative_to(source_root)
        except ValueError as exc:
            raise SystemExit(f"license path escapes source tree: {license_file.path}") from exc
        if not candidate.is_file():
            raise SystemExit(f"license source not found: {license_file.path}")
        destination = out_dir / license_file.asset_name
        shutil.copy2(candidate, destination)
        copied.append(destination)
    return copied
