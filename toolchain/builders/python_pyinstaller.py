from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from toolchain.manifest import PythonBuilderSpec, ToolManifest
from toolchain.source import PreparedSource


Runner = Callable[..., str]
EntrypointResolver = Callable[[Path, str], tuple[str, str]]
UV_VERSION = re.compile(r"^uv ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")
PYTHON_VERSION = re.compile(r"^Python ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")


def run_command(command: list[str], *, cwd=None, env=None) -> str:
    print("+", " ".join(str(item) for item in command), flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return (result.stdout or result.stderr).strip()


def parse_uv_version(output: str) -> str:
    match = UV_VERSION.match(output.strip())
    if match is None:
        raise SystemExit(f"cannot parse uv version: {output.strip()}")
    return match.group(1)


def parse_python_version(output: str) -> str:
    match = PYTHON_VERSION.match(output.strip())
    if match is None:
        raise SystemExit(f"cannot parse Python version: {output.strip()}")
    return match.group(1)


def verify_builder_identity(
    *,
    python_version: str,
    uv_version: str,
    pyinstaller_version: str,
    expected_python: str,
    expected_uv: str,
    expected_pyinstaller: str,
) -> None:
    checks = (
        ("Python", python_version, expected_python),
        ("uv", uv_version, expected_uv),
        ("PyInstaller", pyinstaller_version, expected_pyinstaller),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise SystemExit(f"builder uses {name} {actual}, expected {expected}")


def resolve_entrypoint(python: Path, name: str) -> tuple[str, str]:
    code = r"""
import json
import sys
from importlib.metadata import entry_points

name = sys.argv[1]
matches = list(entry_points().select(group="console_scripts", name=name))
if len(matches) != 1:
    raise SystemExit(f"expected one console entrypoint for {name}, found {len(matches)}")
entrypoint = matches[0]
if not entrypoint.attr:
    raise SystemExit(f"entrypoint is not callable: {entrypoint.value}")
print(json.dumps({"module": entrypoint.module, "attr": entrypoint.attr}))
"""
    result = subprocess.run(
        [str(python), "-c", code, name],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    data = json.loads(result.stdout)
    return data["module"], data["attr"]


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
        newline="\n",
    )


def build_python_pyinstaller(
    manifest: ToolManifest,
    target_key: str,
    source: PreparedSource,
    out_dir: Path,
    work_dir: Path,
    *,
    runner: Runner = run_command,
    entrypoint_resolver: EntrypointResolver = resolve_entrypoint,
) -> list[Path]:
    if not isinstance(manifest.builder, PythonBuilderSpec):
        raise SystemExit(f"{manifest.name} is not a Python/PyInstaller tool")
    if target_key not in manifest.targets:
        raise SystemExit(f"unknown target {target_key}")
    target = manifest.targets[target_key]
    builder = manifest.builder
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
            f"pyinstaller=={builder.pyinstaller_version}",
        ],
        cwd=None,
        env=None,
    )
    pyinstaller_version = runner(
        [str(venv_python), "-m", "PyInstaller", "--version"], cwd=None, env=None
    ).strip()
    verify_builder_identity(
        python_version=python_version,
        uv_version=uv_version,
        pyinstaller_version=pyinstaller_version,
        expected_python=builder.python_version,
        expected_uv=builder.uv_version,
        expected_pyinstaller=builder.pyinstaller_version,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    for binary in builder.binaries:
        module, attr = entrypoint_resolver(venv_python, binary.source_name)
        if module != binary.module:
            raise SystemExit(
                f"{binary.source_name} resolves to {module}, expected {binary.module}"
            )
        build_root = work_dir / "pyinstaller" / binary.source_name
        build_root.mkdir(parents=True, exist_ok=True)
        stub = build_root / "entrypoint.py"
        write_entrypoint_stub(stub, module, attr)
        asset_name = f"{binary.asset_base}-{target_key}{target.exe}"
        runner(
            [
                str(venv_python),
                "-m",
                "PyInstaller",
                "--onefile",
                "--clean",
                "--noconfirm",
                "--name",
                asset_name,
                "--distpath",
                str(out_dir),
                "--workpath",
                str(build_root / "build"),
                "--specpath",
                str(build_root / "spec"),
                "--collect-all",
                builder.collect_all,
                "--hidden-import",
                module,
                str(stub),
            ],
            cwd=build_root,
            env={**os.environ, "PYTHONHASHSEED": "0"},
        )
        asset = out_dir / asset_name
        if not asset.is_file():
            raise SystemExit(f"PyInstaller output not found: {asset}")
        if not asset.name.endswith(".exe"):
            asset.chmod(asset.stat().st_mode | 0o755)
        assets.append(asset)
    return assets
