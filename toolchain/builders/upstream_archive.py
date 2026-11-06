from __future__ import annotations

import shutil
import stat
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from toolchain.manifest import ToolManifest, UpstreamArchiveSpec, UpstreamArchiveTargetSpec
from toolchain.source import PreparedSource, sha256


Fetch = Callable[[str, Path], None]
CHUNK_BYTES = 1024 * 1024


def fetch_url(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream, CHUNK_BYTES)


def upstream_archive_url(manifest: ToolManifest, archive: UpstreamArchiveTargetSpec) -> str:
    return (
        f"{manifest.source.repository}/releases/download/"
        f"{manifest.source.ref}/{archive.name}"
    )


def _safe_member(info: zipfile.ZipInfo, archive_name: str) -> PurePosixPath:
    name = info.filename
    path = PurePosixPath(name)
    unix_mode = info.external_attr >> 16 if info.create_system in (3, 19) else 0
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise SystemExit(f"upstream archive {archive_name} has unsafe member {name!r}")
    if stat.S_ISLNK(unix_mode):
        raise SystemExit(f"upstream archive {archive_name} has symbolic link {name!r}")
    return path


def verify_upstream_archive(path: Path, archive: UpstreamArchiveTargetSpec) -> None:
    """Check identity first, so that an unverified archive is never parsed."""
    size = path.stat().st_size
    if size != archive.size:
        raise SystemExit(
            f"upstream archive {archive.name} size is {size}, expected {archive.size}"
        )
    digest = sha256(path)
    if digest != archive.sha256:
        raise SystemExit(
            f"upstream archive {archive.name} checksum is {digest}, expected {archive.sha256}"
        )
    with zipfile.ZipFile(path) as opened:
        names = {str(_safe_member(info, archive.name)) for info in opened.infolist()}
    if archive.binary not in names:
        raise SystemExit(f"upstream archive {archive.name} has no binary {archive.binary}")


def materialize_upstream_archive(
    path: Path,
    archive: UpstreamArchiveTargetSpec,
    destination: Path,
) -> Path:
    """Unpack a verified archive with its Unix modes and return the binary."""
    verify_upstream_archive(path, archive)
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(path) as opened:
        for info in opened.infolist():
            member = _safe_member(info, archive.name)
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with opened.open(info) as source, target.open("wb") as stream:
                shutil.copyfileobj(source, stream, CHUNK_BYTES)
            unix_mode = info.external_attr >> 16 if info.create_system in (3, 19) else 0
            if unix_mode & 0o111:
                target.chmod(target.stat().st_mode | 0o755)
    binary = destination.joinpath(*PurePosixPath(archive.binary).parts)
    binary.chmod(binary.stat().st_mode | 0o755)
    return binary


def build_upstream_archive(
    manifest: ToolManifest,
    target_key: str,
    source: PreparedSource,
    out_dir: Path,
    work_dir: Path,
    *,
    fetch: Fetch = fetch_url,
) -> list[Path]:
    if not isinstance(manifest.builder, UpstreamArchiveSpec):
        raise SystemExit(f"{manifest.name} is not an upstream archive tool")
    archive = manifest.builder.archives[target_key]
    if source.patches:
        raise SystemExit(f"{manifest.name} publishes the upstream archive and cannot apply patches")
    downloaded = work_dir / "upstream" / archive.name
    downloaded.parent.mkdir(parents=True, exist_ok=True)
    url = upstream_archive_url(manifest, archive)
    print("+ download", url, flush=True)
    fetch(url, downloaded)
    verify_upstream_archive(downloaded, archive)
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / archive.name
    shutil.copyfile(downloaded, destination)
    return [destination]
