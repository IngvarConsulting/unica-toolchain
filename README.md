# Unica Toolchain

Pinned native builds of third-party runtime tools distributed with
[Unica](https://github.com/IngvarConsulting/unica).

The first toolchain target is `Dach-Coin/rlm-tools-bsl`. Its two Python console
entrypoints are frozen into native executables for Linux x64, Windows x64, and
Apple silicon macOS.

## Release contract

- Releases are built only by an explicit `workflow_dispatch`.
- `manifests/rlm-tools-bsl.json` pins the upstream tag and commit, Python, uv,
  PyInstaller, and the toolchain build revision.
- Upstream runtime dependencies are installed from its committed `uv.lock`
  using `uv sync --frozen`.
- Every release contains six executable assets, per-platform checksum files,
  and machine-readable provenance.
- Consumers must pin an immutable release tag and SHA-256. They must never
  download `latest`.

Release tags have the form `rlm-tools-bsl-v<upstream>-build.<revision>`.

## Updating RLM

1. Update every pin in `manifests/rlm-tools-bsl.json`.
2. Increment `buildRevision` when rebuilding the same upstream version.
3. Open a pull request and wait for `Verify toolchain source`.
4. Merge the pull request, then manually run `Build RLM release` on `main`.
5. Verify all release assets and provenance before updating Unica's
   `plugins/unica/third-party/tools.lock.json`.

## Local checks

```sh
python3.12 -m unittest discover -s tests
python3.12 -m py_compile scripts/*.py tests/*.py
```
