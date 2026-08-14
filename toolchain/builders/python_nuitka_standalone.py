from __future__ import annotations

import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from toolchain.builders.python_pyinstaller import (
    parse_python_version,
    parse_uv_version,
    resolve_entrypoint,
    run_command,
    write_entrypoint_stub,
)
from toolchain.manifest import (
    PythonNuitkaStandaloneSpec,
    ToolManifest,
    release_tag,
)
from toolchain.runtime_archive import write_runtime_archive
from toolchain.source import PreparedSource


Runner = Callable[..., str]
EntrypointResolver = Callable[[Path, str], tuple[str, str]]
NUITKA_VERSION = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")


@dataclass(frozen=True)
class NuitkaBuildResult:
    assets: tuple[Path, ...]
    builder_identity: dict[str, Any]


def parse_nuitka_version(output: str) -> str:
    match = NUITKA_VERSION.match(output.strip())
    if match is None:
        raise SystemExit(f"cannot parse Nuitka version: {output.strip()}")
    return match.group(1)


def parse_nuitka_report(
    path: Path,
    *,
    expected_nuitka: str,
) -> dict[str, str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise SystemExit(f"cannot read Nuitka report: {exc}") from exc
    if root.tag != "nuitka-compilation-report":
        raise SystemExit(f"unexpected Nuitka report root: {root.tag}")
    report_version = root.get("nuitka_version")
    if report_version != expected_nuitka:
        raise SystemExit(
            f"Nuitka report version is {report_version}, expected {expected_nuitka}"
        )
    environment = root.find("scons_environment")
    attributes = (
        environment.attrib if environment is not None else {}
    )
    required = ("c_compiler", "the_cc_name", "the_compiler")
    if any(not attributes.get(name) for name in required):
        raise SystemExit("Nuitka report is missing compiler identity")
    return {
        "cCompiler": attributes["c_compiler"],
        "ccName": attributes["the_cc_name"],
        "compiler": attributes["the_compiler"],
    }


def _verify_identity(
    *,
    python_version: str,
    uv_version: str,
    nuitka_version: str,
    builder: PythonNuitkaStandaloneSpec,
) -> None:
    checks = (
        ("Python", python_version, builder.python_version),
        ("uv", uv_version, builder.uv_version),
        ("Nuitka", nuitka_version, builder.nuitka_version),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise SystemExit(f"builder uses {name} {actual}, expected {expected}")


def _copy_dist_payload(
    *,
    dist_dir: Path,
    compiled_executable: Path,
    payload_root: Path,
    entrypoint_paths: set[str],
) -> None:
    if not dist_dir.is_dir() or dist_dir.is_symlink():
        raise SystemExit(f"Nuitka standalone directory not found: {dist_dir}")
    payload_root.mkdir(parents=True)
    for source_path in sorted(dist_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = source_path.relative_to(dist_dir).as_posix()
        if source_path.is_symlink():
            raise SystemExit(f"Nuitka output must not contain links: {relative}")
        if source_path.is_dir():
            continue
        if not source_path.is_file():
            raise SystemExit(f"Nuitka output must contain ordinary files: {relative}")
        if source_path == compiled_executable:
            continue
        if relative in entrypoint_paths:
            raise SystemExit(f"Nuitka output collides with entrypoint: {relative}")
        destination = payload_root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)


def build_python_nuitka_standalone(
    manifest: ToolManifest,
    target_key: str,
    source: PreparedSource,
    out_dir: Path,
    work_dir: Path,
    *,
    runner: Runner = run_command,
    entrypoint_resolver: EntrypointResolver = resolve_entrypoint,
) -> NuitkaBuildResult:
    if not isinstance(manifest.builder, PythonNuitkaStandaloneSpec):
        raise SystemExit(f"{manifest.name} is not a Python/Nuitka standalone tool")
    if target_key not in manifest.targets:
        raise SystemExit(f"unknown target {target_key}")
    builder = manifest.builder
    target = manifest.targets[target_key]
    if len(builder.binaries) < 2:
        raise SystemExit("Nuitka multidist requires at least two binaries")

    uv_version = parse_uv_version(runner(["uv", "--version"], cwd=None, env=None))
    runner(
        [
            "uv",
            "sync",
            "--frozen",
            "--no-dev",
            "--directory",
            str(source.path),
            "--python",
            sys.executable,
        ],
        cwd=None,
        env=None,
    )
    if os.name == "nt":
        venv_python = source.path / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = source.path / ".venv" / "bin" / "python"
    python_version = parse_python_version(
        runner([str(venv_python), "--version"], cwd=None, env=None)
    )
    runner(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            f"Nuitka=={builder.nuitka_version}",
        ],
        cwd=None,
        env=None,
    )
    nuitka_version = parse_nuitka_version(
        runner(
            [str(venv_python), "-m", "nuitka", "--version"],
            cwd=None,
            env=None,
        )
    )
    _verify_identity(
        python_version=python_version,
        uv_version=uv_version,
        nuitka_version=nuitka_version,
        builder=builder,
    )

    build_root = work_dir / "nuitka"
    entrypoint_root = build_root / "entrypoints"
    output_root = build_root / "output"
    report = build_root / "report.xml"
    entrypoint_root.mkdir(parents=True, exist_ok=True)
    main_args: list[str] = []
    for binary in builder.binaries:
        module, attr = entrypoint_resolver(venv_python, binary.source_name)
        if module != binary.module:
            raise SystemExit(
                f"{binary.source_name} resolves to {module}, expected {binary.module}"
            )
        stub = entrypoint_root / f"{binary.asset_base}.py"
        write_entrypoint_stub(
            stub,
            module,
            attr,
            configure_utf8_stdio=target_key == "win-x64",
        )
        main_args.append(f"--main={stub}")

    runner(
        [
            str(venv_python),
            "-m",
            "nuitka",
            "--mode=standalone",
            "--assume-yes-for-downloads",
            f"--include-package={builder.include_package}",
            f"--include-package-data={builder.include_package}",
            f"--output-dir={output_root}",
            f"--report={report}",
            *main_args,
        ],
        cwd=build_root,
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    compiler = parse_nuitka_report(report, expected_nuitka=builder.nuitka_version)
    builder_identity: dict[str, Any] = {
        "kind": builder.kind,
        "python": python_version,
        "uv": uv_version,
        "nuitka": nuitka_version,
        "compiler": compiler,
    }

    first = builder.binaries[0]
    dist_dir = output_root / f"{first.asset_base}.dist"
    compiled_executable = dist_dir / f"{first.asset_base}{target.exe}"
    if not compiled_executable.is_file() or compiled_executable.is_symlink():
        raise SystemExit(f"Nuitka output executable not found: {compiled_executable}")
    payload_root = build_root / "payload"
    if payload_root.exists():
        shutil.rmtree(payload_root)
    entrypoints = {
        binary.asset_base: f"{binary.asset_base}{target.exe}"
        for binary in builder.binaries
    }
    _copy_dist_payload(
        dist_dir=dist_dir,
        compiled_executable=compiled_executable,
        payload_root=payload_root,
        entrypoint_paths=set(entrypoints.values()),
    )
    for relative in entrypoints.values():
        destination = payload_root / relative
        shutil.copyfile(compiled_executable, destination)
        destination.chmod(0o755)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{manifest.name}-{target_key}.tar.gz"
    write_runtime_archive(
        archive_path=archive,
        payload_root=payload_root,
        release_tag=release_tag(manifest),
        source={
            "ref": manifest.source.ref,
            "commit": source.commit,
            "tree": source.tree,
            "patches": [
                {"path": patch.path, "sha256": patch.sha256}
                for patch in source.patches
            ],
        },
        target={"key": target_key, "triple": target.target_triple},
        entrypoints=entrypoints,
        builder=builder_identity,
    )
    return NuitkaBuildResult(assets=(archive,), builder_identity=builder_identity)
