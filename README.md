# Unica Toolchain

Reproducible native builds of third-party tools distributed with
[Unica](https://github.com/IngvarConsulting/unica).

This repository owns the part of the supply chain that starts from a pinned
upstream source revision and ends with immutable, checksummed native release
assets. It is intentionally not tied to one upstream project: the current
manifests cover `rlm-tools-bsl`, `bsl-analyzer`, and `v8-runner`, and the same
contract can be extended to other Cargo or Python/PyInstaller tools.

## Release contract

- Releases are built only by an explicit `workflow_dispatch` for one manifest.
- Every manifest declares an official release or nightly source identity and
  pins the upstream repository, ref, exact commit, licenses,
  builder versions, target matrix, assets, smoke commands, and toolchain build
  revision.
- Optional repository-owned patches are applied in manifest order and verified
  by SHA-256 before any build starts.
- Python dependencies come from an upstream frozen `uv.lock`; Cargo builds use
  the upstream lock file through `cargo build --locked`.
- Each target produces normalized executable names, checksums, license assets,
  and machine-readable provenance.
- A release is rejected if its tag already exists or if its file set differs
  from the manifest-derived contract.
- Consumers pin an immutable toolchain release tag and SHA-256; they never
  download `latest` or rebuild an upstream project in their own CI.

Official release tags have the form
`<tool>-v<version>-build.<revision>`, including prereleases such as
`v8-runner-v0.5.2-pre.1-build.1`. Sources built from a branch, non-release tag,
or exact commit use `<tool>-nightly-<source>-build.<revision>`, for example
`v8-runner-nightly-master-build.1`. Direct commit sources use the first 12
characters of the pinned SHA in the release name. Each tool is released
independently, so changing one manifest does not rebuild the others.

## Adding or updating a tool

1. Add or update `manifests/<tool>.json`, including `source.kind`,
   `source.ref`, and the exact upstream commit. Use `release` only for a
   published version tag matching `v<version>`; use `nightly` for branches,
   non-release tags, and direct commits.
2. Put required patches under `patches/<tool>/` and record each SHA-256 in the
   manifest. Leave `patches` empty when the pinned source builds unchanged.
3. Increment `buildRevision` when rebuilding the same release version or
   nightly source label.
4. Open a pull request and wait for source, patch, license, schema, and workflow
   validation.
5. Merge the pull request, then manually run `Build tool release` on `main` with
   the manifest name (without `.json`).
6. Verify the assets, checksums, provenance, and native smoke command before
   updating the consumer lock file.

## Local checks

```sh
python3.12 -m unittest discover -s tests
python3.12 -m py_compile scripts/*.py toolchain/*.py toolchain/builders/*.py tests/*.py
actionlint
```

Source pins and declared license files can be verified without compiling a
tool:

```sh
python3.12 scripts/toolchain.py validate-source \
  --manifest manifests/bsl-analyzer.json \
  --repo-root . \
  --work-dir .build/source-bsl-analyzer \
  --out-dir dist/source-bsl-analyzer
```
