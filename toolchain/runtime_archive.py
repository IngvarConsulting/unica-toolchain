from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuntimeFile:
    path: str
    sha256: str
    size: int
    executable: bool


@dataclass(frozen=True)
class RuntimeArchiveManifest:
    schema_version: int
    release_tag: str
    source: dict[str, Any]
    target: dict[str, str]
    entrypoints: dict[str, str]
    builder: dict[str, Any]
    files: tuple[RuntimeFile, ...]


@dataclass(frozen=True)
class RuntimeArchiveFile:
    path: str
    sha256: str
    size: int
    executable: bool
    payload: bytes


@dataclass(frozen=True)
class ValidatedRuntimeArchive:
    manifest: RuntimeArchiveManifest
    files: tuple[RuntimeArchiveFile, ...]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{field} must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or "\x00" in value:
        raise SystemExit(f"unsafe {field}: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SystemExit(f"unsafe {field}: {value!r}")
    return value


def _exact_fields(data: dict[str, Any], expected: set[str], *, field: str) -> None:
    missing = sorted(expected - data.keys())
    unknown = sorted(data.keys() - expected)
    if missing or unknown:
        raise SystemExit(
            f"{field} fields mismatch: missing={missing}, unknown={unknown}"
        )


def _dict(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SystemExit(f"{field} must be an object")
    return value


def _validate_source(value: Any) -> dict[str, Any]:
    source = _dict(value, field="source")
    _exact_fields(source, {"ref", "commit", "tree", "patches"}, field="source")
    if not isinstance(source["ref"], str) or not source["ref"]:
        raise SystemExit("source.ref must be a non-empty string")
    for field in ("commit", "tree"):
        if not isinstance(source[field], str) or not SHA40.fullmatch(source[field]):
            raise SystemExit(f"source.{field} must be 40 lowercase hexadecimal characters")
    patches = source["patches"]
    if not isinstance(patches, list):
        raise SystemExit("source.patches must be an array")
    normalized_patches: list[dict[str, str]] = []
    for index, item in enumerate(patches):
        patch = _dict(item, field=f"source.patches[{index}]")
        _exact_fields(patch, {"path", "sha256"}, field=f"source.patches[{index}]")
        path = _safe_relative_path(patch["path"], field=f"source.patches[{index}].path")
        digest = patch["sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise SystemExit(
                f"source.patches[{index}].sha256 must be 64 lowercase hexadecimal characters"
            )
        normalized_patches.append({"path": path, "sha256": digest})
    return {
        "ref": source["ref"],
        "commit": source["commit"],
        "tree": source["tree"],
        "patches": normalized_patches,
    }


def _validate_target(value: Any) -> dict[str, str]:
    target = _dict(value, field="target")
    _exact_fields(target, {"key", "triple"}, field="target")
    for field in ("key", "triple"):
        if not isinstance(target[field], str) or not target[field]:
            raise SystemExit(f"target.{field} must be a non-empty string")
    return {"key": target["key"], "triple": target["triple"]}


def _validate_entrypoints(value: Any) -> dict[str, str]:
    entrypoints = _dict(value, field="entrypoints")
    if len(entrypoints) < 2:
        raise SystemExit("entrypoints must contain at least two programs")
    normalized: dict[str, str] = {}
    for name, path_value in entrypoints.items():
        if not isinstance(name, str) or not name:
            raise SystemExit("entrypoints names must be non-empty strings")
        normalized[name] = _safe_relative_path(
            path_value, field=f"entrypoints.{name}"
        )
    if len(set(normalized.values())) != len(normalized):
        raise SystemExit("entrypoints paths must be unique")
    return normalized


def _validate_builder(value: Any) -> dict[str, Any]:
    builder = _dict(value, field="builder")
    if not builder or not isinstance(builder.get("kind"), str):
        raise SystemExit("builder.kind must be a non-empty string")
    try:
        json.dumps(builder, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"builder must be JSON serializable: {exc}") from exc
    return builder


def _payload_files(payload_root: Path) -> tuple[RuntimeArchiveFile, ...]:
    if not payload_root.is_dir() or payload_root.is_symlink():
        raise SystemExit(f"payload root must be an ordinary directory: {payload_root}")
    result: list[RuntimeArchiveFile] = []
    for path in sorted(payload_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(payload_root).as_posix()
        _safe_relative_path(relative, field="payload path")
        if path.is_symlink():
            raise SystemExit(f"payload member must be an ordinary file: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SystemExit(f"payload member must be an ordinary file: {relative}")
        payload = path.read_bytes()
        executable = bool(path.stat().st_mode & 0o111)
        result.append(
            RuntimeArchiveFile(
                path=relative,
                sha256=_sha256_bytes(payload),
                size=len(payload),
                executable=executable,
                payload=payload,
            )
        )
    if not result:
        raise SystemExit("payload must contain at least one ordinary file")
    return tuple(result)


def _require_entrypoints(
    entrypoints: dict[str, str], files: tuple[RuntimeArchiveFile, ...]
) -> None:
    by_path = {item.path: item for item in files}
    selected: list[RuntimeArchiveFile] = []
    for name, path in entrypoints.items():
        item = by_path.get(path)
        if item is None:
            raise SystemExit(f"entrypoints.{name} is missing from payload: {path}")
        if not item.executable:
            raise SystemExit(f"entrypoints.{name} is not executable: {path}")
        selected.append(item)
    if len({item.sha256 for item in selected}) != 1:
        raise SystemExit("multidist entrypoints must be byte-identical")


def _manifest_document(
    *,
    release_tag: str,
    source: dict[str, Any],
    target: dict[str, str],
    entrypoints: dict[str, str],
    builder: dict[str, Any],
    files: tuple[RuntimeArchiveFile, ...],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "releaseTag": release_tag,
        "source": source,
        "target": target,
        "entrypoints": entrypoints,
        "builder": builder,
        "files": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size": item.size,
                "executable": item.executable,
            }
            for item in files
        ],
    }


def _add_bytes(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    executable: bool,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o755 if executable else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def write_runtime_archive(
    *,
    archive_path: Path,
    payload_root: Path,
    release_tag: str,
    source: dict[str, Any],
    target: dict[str, Any],
    entrypoints: dict[str, Any],
    builder: dict[str, Any],
) -> Path:
    if not isinstance(release_tag, str) or not release_tag:
        raise SystemExit("releaseTag must be a non-empty string")
    normalized_source = _validate_source(source)
    normalized_target = _validate_target(target)
    normalized_entrypoints = _validate_entrypoints(entrypoints)
    normalized_builder = _validate_builder(builder)
    files = _payload_files(payload_root)
    _require_entrypoints(normalized_entrypoints, files)
    document = _manifest_document(
        release_tag=release_tag,
        source=normalized_source,
        target=normalized_target,
        entrypoints=normalized_entrypoints,
        builder=normalized_builder,
        files=files,
    )
    manifest_bytes = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                _add_bytes(archive, "manifest.json", manifest_bytes, False)
                for item in files:
                    _add_bytes(
                        archive,
                        f"payload/{item.path}",
                        item.payload,
                        item.executable,
                    )
    return archive_path


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(f"duplicate JSON key in manifest: {key}")
        result[key] = value
    return result


def _load_manifest_bytes(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid UTF-8 JSON manifest: {exc}") from exc
    return _dict(value, field="manifest")


def _parse_manifest(document: dict[str, Any]) -> RuntimeArchiveManifest:
    _exact_fields(
        document,
        {
            "schemaVersion",
            "releaseTag",
            "source",
            "target",
            "entrypoints",
            "builder",
            "files",
        },
        field="manifest",
    )
    if document["schemaVersion"] != 1:
        raise SystemExit(f"unsupported runtime archive schemaVersion: {document['schemaVersion']}")
    release_tag = document["releaseTag"]
    if not isinstance(release_tag, str) or not release_tag:
        raise SystemExit("releaseTag must be a non-empty string")
    source = _validate_source(document["source"])
    target = _validate_target(document["target"])
    entrypoints = _validate_entrypoints(document["entrypoints"])
    builder = _validate_builder(document["builder"])
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise SystemExit("files must be a non-empty array")
    files: list[RuntimeFile] = []
    paths: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        item = _dict(raw_file, field=f"files[{index}]")
        _exact_fields(
            item,
            {"path", "sha256", "size", "executable"},
            field=f"files[{index}]",
        )
        path = _safe_relative_path(item["path"], field=f"files[{index}].path")
        if path in paths:
            raise SystemExit(f"duplicate file path in manifest: {path}")
        paths.add(path)
        digest = item["sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise SystemExit(f"files[{index}].sha256 must be 64 lowercase hexadecimal characters")
        size = item["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SystemExit(f"files[{index}].size must be a non-negative integer")
        executable = item["executable"]
        if not isinstance(executable, bool):
            raise SystemExit(f"files[{index}].executable must be a boolean")
        files.append(RuntimeFile(path, digest, size, executable))
    if [item.path for item in files] != sorted(item.path for item in files):
        raise SystemExit("files must be sorted by path")
    return RuntimeArchiveManifest(
        schema_version=1,
        release_tag=release_tag,
        source=source,
        target=target,
        entrypoints=entrypoints,
        builder=builder,
        files=tuple(files),
    )


def validate_runtime_archive(
    path: Path,
    *,
    expected_release_tag: str,
    expected_source_ref: str,
    expected_source_commit: str,
    expected_target_key: str,
    expected_target_triple: str,
    expected_entrypoints: dict[str, str],
) -> ValidatedRuntimeArchive:
    member_payloads: dict[str, tuple[bytes, int]] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = _safe_relative_path(member.name, field="archive member")
                if name in member_payloads:
                    raise SystemExit(f"duplicate archive member: {name}")
                if not member.isreg():
                    raise SystemExit(f"archive member must be an ordinary file: {name}")
                if name != "manifest.json" and not name.startswith("payload/"):
                    raise SystemExit(f"unsafe archive member outside payload: {name}")
                if member.mode not in (0o644, 0o755):
                    raise SystemExit(f"unsafe archive member mode for {name}: {oct(member.mode)}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SystemExit(f"archive member cannot be read: {name}")
                member_payloads[name] = (stream.read(), member.mode)
    except (tarfile.TarError, OSError) as exc:
        raise SystemExit(f"cannot read runtime archive: {exc}") from exc
    manifest_entry = member_payloads.get("manifest.json")
    if manifest_entry is None:
        raise SystemExit("runtime archive is missing manifest.json")
    if manifest_entry[1] != 0o644:
        raise SystemExit("manifest.json mode must be 0644")
    manifest = _parse_manifest(_load_manifest_bytes(manifest_entry[0]))
    if manifest.release_tag != expected_release_tag:
        raise SystemExit(
            f"releaseTag mismatch: {manifest.release_tag} != {expected_release_tag}"
        )
    if manifest.source["ref"] != expected_source_ref:
        raise SystemExit(
            f"source.ref mismatch: {manifest.source['ref']} != {expected_source_ref}"
        )
    if manifest.source["commit"] != expected_source_commit:
        raise SystemExit(
            "source.commit mismatch: "
            f"{manifest.source['commit']} != {expected_source_commit}"
        )
    if manifest.target["key"] != expected_target_key:
        raise SystemExit(
            f"target.key mismatch: {manifest.target['key']} != {expected_target_key}"
        )
    if manifest.target["triple"] != expected_target_triple:
        raise SystemExit(
            "target.triple mismatch: "
            f"{manifest.target['triple']} != {expected_target_triple}"
        )
    if manifest.entrypoints != expected_entrypoints:
        raise SystemExit(
            f"entrypoints mismatch: {manifest.entrypoints} != {expected_entrypoints}"
        )
    expected_names = {f"payload/{item.path}" for item in manifest.files}
    actual_names = set(member_payloads) - {"manifest.json"}
    if actual_names != expected_names:
        raise SystemExit(
            "runtime archive file set mismatch: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"unexpected={sorted(actual_names - expected_names)}"
        )
    files: list[RuntimeArchiveFile] = []
    for item in manifest.files:
        payload, mode = member_payloads[f"payload/{item.path}"]
        if len(payload) != item.size:
            raise SystemExit(f"size mismatch for payload/{item.path}")
        digest = _sha256_bytes(payload)
        if digest != item.sha256:
            raise SystemExit(f"sha256 mismatch for payload/{item.path}")
        expected_mode = 0o755 if item.executable else 0o644
        if mode != expected_mode:
            raise SystemExit(f"mode mismatch for payload/{item.path}")
        files.append(
            RuntimeArchiveFile(
                path=item.path,
                sha256=item.sha256,
                size=item.size,
                executable=item.executable,
                payload=payload,
            )
        )
    _require_entrypoints(manifest.entrypoints, tuple(files))
    return ValidatedRuntimeArchive(manifest=manifest, files=tuple(files))


def materialize_runtime_archive(
    archive: ValidatedRuntimeArchive,
    destination: Path,
) -> dict[str, Path]:
    if destination.exists():
        raise SystemExit(f"runtime archive destination already exists: {destination}")
    destination.mkdir(parents=True)
    result: dict[str, Path] = {}
    for item in archive.files:
        path = destination.joinpath(*item.path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(item.payload)
        path.chmod(0o755 if item.executable else 0o644)
        result[item.path] = path
    return result
