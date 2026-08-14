from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from toolchain.runtime_archive import (
    materialize_runtime_archive,
    validate_runtime_archive,
    write_runtime_archive,
)


RELEASE_TAG = "rlm-tools-bsl-v1.33.0-build.3"
SOURCE = {
    "ref": "v1.33.0",
    "commit": "3e6920cd015a61af4ba7aa1a5f1fedd8bc935549",
    "tree": "4b321de0454d4d0998762659891374a3a1326cd0",
    "patches": [],
}
TARGET = {
    "key": "linux-x64",
    "triple": "x86_64-unknown-linux-gnu",
}
ENTRYPOINTS = {
    "rlm-bsl-index": "rlm-bsl-index",
    "rlm-bsl-mcp": "rlm-bsl-mcp",
}
BUILDER = {
    "kind": "python-nuitka-standalone",
    "python": "3.12.10",
    "uv": "0.11.29",
    "nuitka": "4.1.3",
    "compiler": {"cCompiler": "Clang", "ccName": "clang", "compiler": "clang"},
}


def add_member(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o644,
    type_: bytes = tarfile.REGTYPE,
    linkname: str = "",
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload) if type_ == tarfile.REGTYPE else 0
    info.mode = mode
    info.type = type_
    info.linkname = linkname
    archive.addfile(info, io.BytesIO(payload) if type_ == tarfile.REGTYPE else None)


def raw_archive(path: Path, members: list[tuple[str, bytes, int, bytes, str]]) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, payload, mode, type_, linkname in members:
                    add_member(
                        archive,
                        name,
                        payload,
                        mode=mode,
                        type_=type_,
                        linkname=linkname,
                    )


def expected_manifest(binary: bytes = b"multidist") -> dict:
    files = [
        {
            "path": "libpython3.12.so.1.0",
            "sha256": hashlib.sha256(b"shared").hexdigest(),
            "size": len(b"shared"),
            "executable": False,
        },
        {
            "path": "rlm-bsl-index",
            "sha256": hashlib.sha256(binary).hexdigest(),
            "size": len(binary),
            "executable": True,
        },
        {
            "path": "rlm-bsl-mcp",
            "sha256": hashlib.sha256(binary).hexdigest(),
            "size": len(binary),
            "executable": True,
        },
    ]
    return {
        "schemaVersion": 1,
        "releaseTag": RELEASE_TAG,
        "source": SOURCE,
        "target": TARGET,
        "entrypoints": ENTRYPOINTS,
        "builder": BUILDER,
        "files": files,
    }


class RuntimeArchiveTests(unittest.TestCase):
    def make_payload(self, root: Path) -> Path:
        payload = root / "payload"
        payload.mkdir()
        for name, data in (
            ("rlm-bsl-index", b"multidist"),
            ("rlm-bsl-mcp", b"multidist"),
            ("libpython3.12.so.1.0", b"shared"),
        ):
            path = payload / name
            path.write_bytes(data)
            path.chmod(0o755 if name.startswith("rlm-bsl-") else 0o644)
        return payload

    def validate(self, path: Path):
        return validate_runtime_archive(
            path,
            expected_release_tag=RELEASE_TAG,
            expected_source_ref=SOURCE["ref"],
            expected_source_commit=SOURCE["commit"],
            expected_target_key=TARGET["key"],
            expected_target_triple=TARGET["triple"],
            expected_entrypoints=ENTRYPOINTS,
        )

    def test_archive_is_deterministic_complete_and_materializes_exact_payload(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payload = self.make_payload(root)
        first = root / "first.tar.gz"
        second = root / "second.tar.gz"

        for archive in (first, second):
            write_runtime_archive(
                archive_path=archive,
                payload_root=payload,
                release_tag=RELEASE_TAG,
                source=SOURCE,
                target=TARGET,
                entrypoints=ENTRYPOINTS,
                builder=BUILDER,
            )

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:gz") as archive:
            members = archive.getmembers()
            self.assertEqual(
                [member.name for member in members],
                [
                    "manifest.json",
                    "payload/libpython3.12.so.1.0",
                    "payload/rlm-bsl-index",
                    "payload/rlm-bsl-mcp",
                ],
            )
            self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
            self.assertTrue(all(member.uname == "" and member.gname == "" for member in members))
            self.assertTrue(all(member.mtime == 0 for member in members))
            manifest = json.loads(archive.extractfile("manifest.json").read())
        self.assertEqual(manifest, expected_manifest())

        validated = self.validate(first)
        destination = root / "materialized"
        paths = materialize_runtime_archive(validated, destination)
        self.assertEqual(set(paths), set(ENTRYPOINTS.values()) | {"libpython3.12.so.1.0"})
        self.assertEqual((destination / "rlm-bsl-index").read_bytes(), b"multidist")
        self.assertEqual((destination / "rlm-bsl-mcp").read_bytes(), b"multidist")
        self.assertEqual((destination / "libpython3.12.so.1.0").read_bytes(), b"shared")
        self.assertTrue((destination / "rlm-bsl-index").stat().st_mode & 0o111)
        self.assertFalse((destination / "libpython3.12.so.1.0").stat().st_mode & 0o111)

    def test_rejects_unsafe_duplicate_and_non_regular_archive_members(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest = json.dumps(expected_manifest(), separators=(",", ":")).encode()
        base = [
            ("manifest.json", manifest, 0o644, tarfile.REGTYPE, ""),
            ("payload/libpython3.12.so.1.0", b"shared", 0o644, tarfile.REGTYPE, ""),
            ("payload/rlm-bsl-index", b"multidist", 0o755, tarfile.REGTYPE, ""),
            ("payload/rlm-bsl-mcp", b"multidist", 0o755, tarfile.REGTYPE, ""),
        ]
        cases = {
            "absolute": base + [("/absolute", b"x", 0o644, tarfile.REGTYPE, "")],
            "parent": base + [("payload/../escape", b"x", 0o644, tarfile.REGTYPE, "")],
            "backslash": base + [("payload\\escape", b"x", 0o644, tarfile.REGTYPE, "")],
            "duplicate": base + [("payload/rlm-bsl-index", b"multidist", 0o755, tarfile.REGTYPE, "")],
            "symlink": base + [("payload/link", b"", 0o777, tarfile.SYMTYPE, "rlm-bsl-index")],
            "hardlink": base + [("payload/link", b"", 0o777, tarfile.LNKTYPE, "rlm-bsl-index")],
            "fifo": base + [("payload/fifo", b"", 0o644, tarfile.FIFOTYPE, "")],
        }

        for label, members in cases.items():
            with self.subTest(label=label):
                archive = root / f"{label}.tar.gz"
                raw_archive(archive, members)
                with self.assertRaisesRegex(SystemExit, "unsafe|duplicate|ordinary"):
                    self.validate(archive)

    def test_rejects_manifest_identity_file_set_and_payload_drift(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        mutations: list[tuple[str, dict, list[tuple[str, bytes, int, bytes, str]], str]] = []
        base_members = [
            ("payload/libpython3.12.so.1.0", b"shared", 0o644, tarfile.REGTYPE, ""),
            ("payload/rlm-bsl-index", b"multidist", 0o755, tarfile.REGTYPE, ""),
            ("payload/rlm-bsl-mcp", b"multidist", 0o755, tarfile.REGTYPE, ""),
        ]

        wrong_release = expected_manifest()
        wrong_release["releaseTag"] = "other"
        mutations.append(("release", wrong_release, base_members, "releaseTag"))

        wrong_source = expected_manifest()
        wrong_source["source"] = dict(SOURCE, commit="a" * 40)
        mutations.append(("source", wrong_source, base_members, "source.commit"))

        wrong_target = expected_manifest()
        wrong_target["target"] = dict(TARGET, key="win-x64")
        mutations.append(("target", wrong_target, base_members, "target.key"))

        wrong_entrypoint = expected_manifest()
        wrong_entrypoint["entrypoints"] = {"rlm-bsl-index": "rlm-bsl-index"}
        mutations.append(("entrypoint", wrong_entrypoint, base_members, "entrypoints"))

        wrong_digest = expected_manifest()
        wrong_digest["files"][1]["sha256"] = "0" * 64
        mutations.append(("digest", wrong_digest, base_members, "sha256"))

        wrong_size = expected_manifest()
        wrong_size["files"][1]["size"] = 99
        mutations.append(("size", wrong_size, base_members, "size"))

        missing_file = expected_manifest()
        mutations.append(("missing", missing_file, base_members[:-1], "file set"))

        extra_file = expected_manifest()
        mutations.append(
            (
                "extra",
                extra_file,
                base_members + [("payload/extra", b"x", 0o644, tarfile.REGTYPE, "")],
                "file set",
            )
        )

        unequal = expected_manifest(binary=b"index")
        unequal["files"][2] = {
            "path": "rlm-bsl-mcp",
            "sha256": hashlib.sha256(b"mcp").hexdigest(),
            "size": 3,
            "executable": True,
        }
        mutations.append(
            (
                "unequal",
                unequal,
                [
                    base_members[0],
                    ("payload/rlm-bsl-index", b"index", 0o755, tarfile.REGTYPE, ""),
                    ("payload/rlm-bsl-mcp", b"mcp", 0o755, tarfile.REGTYPE, ""),
                ],
                "byte-identical",
            )
        )

        for label, manifest, payload_members, message in mutations:
            with self.subTest(label=label):
                archive = root / f"{label}.tar.gz"
                members = [
                    (
                        "manifest.json",
                        json.dumps(manifest, separators=(",", ":")).encode(),
                        0o644,
                        tarfile.REGTYPE,
                        "",
                    ),
                    *payload_members,
                ]
                raw_archive(archive, members)
                with self.assertRaisesRegex(SystemExit, message):
                    self.validate(archive)

    def test_writer_rejects_symlink_and_different_entrypoint_bytes(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payload = self.make_payload(root)
        (payload / "rlm-bsl-mcp").write_bytes(b"different")
        with self.assertRaisesRegex(SystemExit, "byte-identical"):
            write_runtime_archive(
                archive_path=root / "different.tar.gz",
                payload_root=payload,
                release_tag=RELEASE_TAG,
                source=SOURCE,
                target=TARGET,
                entrypoints=ENTRYPOINTS,
                builder=BUILDER,
            )

        (payload / "rlm-bsl-mcp").unlink()
        (payload / "rlm-bsl-mcp").symlink_to(payload / "rlm-bsl-index")
        with self.assertRaisesRegex(SystemExit, "ordinary"):
            write_runtime_archive(
                archive_path=root / "symlink.tar.gz",
                payload_root=payload,
                release_tag=RELEASE_TAG,
                source=SOURCE,
                target=TARGET,
                entrypoints=ENTRYPOINTS,
                builder=BUILDER,
            )


if __name__ == "__main__":
    unittest.main()
