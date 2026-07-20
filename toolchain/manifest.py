from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


TARGET_KEYS = frozenset({"darwin-arm64", "linux-x64", "win-x64"})
SYSTEM_SETUPS = frozenset({"none", "musl-tools"})
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class SourceSpec:
    repository: str
    tag: str
    commit: str


@dataclass(frozen=True)
class PatchSpec:
    path: str
    sha256: str


@dataclass(frozen=True)
class LicenseFileSpec:
    path: str
    asset_name: str


@dataclass(frozen=True)
class LicenseSpec:
    spdx: str
    files: tuple[LicenseFileSpec, ...]


@dataclass(frozen=True)
class BinarySpec:
    package: str
    source_name: str
    asset_base: str
    smoke_args: tuple[str, ...]
    module: str | None = None


@dataclass(frozen=True)
class TargetSpec:
    runner: str
    target_triple: str
    exe: str
    system_setup: Literal["none", "musl-tools"]
    environment: dict[str, str]


@dataclass(frozen=True)
class CargoBuilderSpec:
    kind: Literal["cargo"]
    rust_version: str
    locked: bool
    binaries: tuple[BinarySpec, ...]


@dataclass(frozen=True)
class PythonBuilderSpec:
    kind: Literal["python-pyinstaller"]
    python_version: str
    uv_version: str
    pyinstaller_version: str
    lock_file: str
    collect_all: str
    binaries: tuple[BinarySpec, ...]


BuilderSpec = CargoBuilderSpec | PythonBuilderSpec


@dataclass(frozen=True)
class ToolManifest:
    schema_version: int
    name: str
    version: str
    build_revision: int
    source: SourceSpec
    license: LicenseSpec
    patches: tuple[PatchSpec, ...]
    builder: BuilderSpec
    targets: dict[str, TargetSpec]


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must be an object")
    return value


def _fields(
    value: dict[str, Any],
    path: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - value.keys())
    if missing:
        raise SystemExit(f"{path} missing fields: {', '.join(missing)}")
    unknown = sorted(value.keys() - required - optional)
    if unknown:
        raise SystemExit(f"{path} has unknown fields: {', '.join(unknown)}")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{path} must be a non-empty string")
    return value


def _version(value: Any, path: str) -> str:
    result = _string(value, path)
    if not SEMVER.fullmatch(result):
        raise SystemExit(f"{path} must be an exact three-part version")
    return result


def _name(value: Any, path: str) -> str:
    result = _string(value, path)
    if not SAFE_NAME.fullmatch(result):
        raise SystemExit(f"{path} contains unsupported characters")
    return result


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"{path} must be an array of strings")
    return tuple(value)


def _load_source(value: Any) -> SourceSpec:
    data = _object(value, "source")
    _fields(data, "source", required={"repository", "tag", "commit"})
    repository = _string(data["repository"], "source.repository")
    tag = _string(data["tag"], "source.tag")
    commit = _string(data["commit"], "source.commit")
    if not repository.startswith("https://github.com/"):
        raise SystemExit("source.repository must be an https://github.com URL")
    if not SHA40.fullmatch(commit):
        raise SystemExit("source.commit must be 40 lowercase hexadecimal characters")
    return SourceSpec(repository, tag, commit)


def _load_license(value: Any) -> LicenseSpec:
    data = _object(value, "license")
    _fields(data, "license", required={"spdx", "files"})
    files_value = data["files"]
    if not isinstance(files_value, list) or not files_value:
        raise SystemExit("license.files must be a non-empty array")
    files: list[LicenseFileSpec] = []
    release_names: set[str] = set()
    for index, item in enumerate(files_value):
        path = f"license.files[{index}]"
        entry = _object(item, path)
        _fields(entry, path, required={"path", "assetName"})
        source_path = _string(entry["path"], f"{path}.path")
        asset_name = _name(entry["assetName"], f"{path}.assetName")
        if asset_name in release_names:
            raise SystemExit(f"duplicate license assetName: {asset_name}")
        release_names.add(asset_name)
        files.append(LicenseFileSpec(source_path, asset_name))
    return LicenseSpec(_string(data["spdx"], "license.spdx"), tuple(files))


def _load_patches(value: Any) -> tuple[PatchSpec, ...]:
    if not isinstance(value, list):
        raise SystemExit("patches must be an array")
    result: list[PatchSpec] = []
    for index, item in enumerate(value):
        path = f"patches[{index}]"
        entry = _object(item, path)
        _fields(entry, path, required={"path", "sha256"})
        digest = _string(entry["sha256"], f"{path}.sha256")
        if not SHA256.fullmatch(digest):
            raise SystemExit(f"{path}.sha256 must be 64 lowercase hexadecimal characters")
        result.append(PatchSpec(_string(entry["path"], f"{path}.path"), digest))
    return tuple(result)


def _load_binary(value: Any, index: int, *, python: bool) -> BinarySpec:
    path = f"builder.binaries[{index}]"
    data = _object(value, path)
    required = {"package", "sourceName", "assetBase", "smokeArgs"}
    if python:
        required.add("module")
    _fields(data, path, required=required)
    asset_base = _name(data["assetBase"], f"{path}.assetBase")
    return BinarySpec(
        package=_name(data["package"], f"{path}.package"),
        source_name=_name(data["sourceName"], f"{path}.sourceName"),
        asset_base=asset_base,
        smoke_args=_strings(data["smokeArgs"], f"{path}.smokeArgs"),
        module=_string(data["module"], f"{path}.module") if python else None,
    )


def _load_binaries(value: Any, *, python: bool) -> tuple[BinarySpec, ...]:
    if not isinstance(value, list) or not value:
        raise SystemExit("builder.binaries must be a non-empty array")
    binaries = tuple(_load_binary(item, index, python=python) for index, item in enumerate(value))
    bases = [binary.asset_base for binary in binaries]
    if len(set(bases)) != len(bases):
        raise SystemExit("builder.binaries assetBase values must be unique")
    return binaries


def _load_builder(value: Any) -> BuilderSpec:
    data = _object(value, "builder")
    kind = data.get("kind")
    if kind == "cargo":
        _fields(data, "builder", required={"kind", "rustVersion", "locked", "binaries"})
        if data["locked"] is not True:
            raise SystemExit("builder.locked must be true")
        return CargoBuilderSpec(
            kind="cargo",
            rust_version=_version(data["rustVersion"], "builder.rustVersion"),
            locked=True,
            binaries=_load_binaries(data["binaries"], python=False),
        )
    if kind == "python-pyinstaller":
        _fields(
            data,
            "builder",
            required={
                "kind",
                "pythonVersion",
                "uvVersion",
                "pyinstallerVersion",
                "lockFile",
                "collectAll",
                "binaries",
            },
        )
        return PythonBuilderSpec(
            kind="python-pyinstaller",
            python_version=_version(data["pythonVersion"], "builder.pythonVersion"),
            uv_version=_version(data["uvVersion"], "builder.uvVersion"),
            pyinstaller_version=_version(data["pyinstallerVersion"], "builder.pyinstallerVersion"),
            lock_file=_string(data["lockFile"], "builder.lockFile"),
            collect_all=_name(data["collectAll"], "builder.collectAll"),
            binaries=_load_binaries(data["binaries"], python=True),
        )
    raise SystemExit(f"unsupported builder kind: {kind}")


def _load_targets(value: Any) -> dict[str, TargetSpec]:
    data = _object(value, "targets")
    if set(data) != TARGET_KEYS:
        raise SystemExit(f"targets must be exactly: {', '.join(sorted(TARGET_KEYS))}")
    result: dict[str, TargetSpec] = {}
    for key, item in data.items():
        path = f"targets.{key}"
        target = _object(item, path)
        _fields(
            target,
            path,
            required={"runner", "targetTriple", "exe", "systemSetup", "environment"},
        )
        setup = _string(target["systemSetup"], f"{path}.systemSetup")
        if setup not in SYSTEM_SETUPS:
            raise SystemExit(f"{path}.systemSetup must be one of: {', '.join(sorted(SYSTEM_SETUPS))}")
        environment = _object(target["environment"], f"{path}.environment")
        for env_name, env_value in environment.items():
            if not ENV_NAME.fullmatch(env_name) or not isinstance(env_value, str):
                raise SystemExit(f"{path}.environment must map environment names to strings")
        exe = target["exe"]
        if exe not in ("", ".exe"):
            raise SystemExit(f"{path}.exe must be empty or .exe")
        result[key] = TargetSpec(
            runner=_string(target["runner"], f"{path}.runner"),
            target_triple=_string(target["targetTriple"], f"{path}.targetTriple"),
            exe=exe,
            system_setup=setup,  # type: ignore[arg-type]
            environment=dict(environment),
        )
    return result


def load_manifest(path: Path) -> ToolManifest:
    try:
        root = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"failed to load manifest {path}: {exc}") from exc
    _fields(
        root,
        str(path),
        required={
            "schemaVersion",
            "name",
            "version",
            "buildRevision",
            "source",
            "license",
            "patches",
            "builder",
            "targets",
        },
    )
    if root["schemaVersion"] != 2:
        raise SystemExit(f"unsupported manifest schemaVersion: {root['schemaVersion']}")
    if not isinstance(root["buildRevision"], int) or root["buildRevision"] < 1:
        raise SystemExit("buildRevision must be a positive integer")
    return ToolManifest(
        schema_version=2,
        name=_name(root["name"], "name"),
        version=_version(root["version"], "version"),
        build_revision=root["buildRevision"],
        source=_load_source(root["source"]),
        license=_load_license(root["license"]),
        patches=_load_patches(root["patches"]),
        builder=_load_builder(root["builder"]),
        targets=_load_targets(root["targets"]),
    )


def release_tag(manifest: ToolManifest) -> str:
    return f"{manifest.name}-v{manifest.version}-build.{manifest.build_revision}"


def expected_asset_names(manifest: ToolManifest) -> set[str]:
    return {
        f"{binary.asset_base}-{target_key}{target.exe}"
        for target_key, target in manifest.targets.items()
        for binary in manifest.builder.binaries
    }


def expected_release_files(manifest: ToolManifest) -> set[str]:
    result = expected_asset_names(manifest)
    result.update(item.asset_name for item in manifest.license.files)
    for target in manifest.targets:
        result.add(f"checksums-{manifest.name}-{target}.txt")
        result.add(f"provenance-{manifest.name}-{target}.json")
    return result
