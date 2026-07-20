# General Unica Toolchain Design

Status: approved design, awaiting written-spec review

## Purpose

`IngvarConsulting/unica-toolchain` is the controlled source-build and supply
boundary for third-party native tools distributed with Unica. It builds every
external runtime tool from an exact upstream commit, optionally applies
auditable local patches, and publishes immutable, independently versioned
releases that Unica consumes by tag and SHA-256.

The first supported tools are:

- `Dach-Coin/rlm-tools-bsl`;
- `itrous/bsl-analyzer`;
- `alkoleft/v8-runner-rust`.

All three move to this supply boundary in the same migration. The repository
does not merely mirror upstream release assets.

## Goals

- Give every tool an independent release lifecycle.
- Build macOS Apple silicon, Linux x64, and Windows x64 binaries from pinned
  source.
- Make local changes explicit as ordered, hash-pinned patch files.
- Keep manifests declarative and prohibit arbitrary shell programs in them.
- Normalize release assets so Unica downloads raw executables for every
  external tool.
- Record enough provenance to reproduce the inputs and audit every output.
- Carry the declared upstream license and notice files with every binary
  release.
- Keep pull-request CI fast; production platform builds remain explicit release
  operations.

## Non-goals

- A general-purpose CI language embedded in JSON.
- Building an unknown tool without a supported builder type.
- Automatically publishing a release after every merge.
- Code signing, Apple notarization, or Windows Authenticode in the first
  iteration.
- Bit-for-bit reproducibility. Inputs are pinned and auditable, but the builders
  are not claimed to produce identical bytes across repeated runs.

## Architecture

The implementation uses a common orchestrator with typed builders:

```text
manifest
  -> exact source checkout
  -> ordered patch verification and application
  -> typed builder (cargo or python-pyinstaller)
  -> native smoke tests
  -> checksums and provenance
  -> independent GitHub Release
```

The components have narrow responsibilities:

- `scripts/toolchain.py` is the command-line entry point. It loads a manifest,
  coordinates source preparation, invokes a builder, and writes metadata.
- `toolchain/manifest.py` validates schema and semantic constraints.
- `toolchain/source.py` checks out the exact commit and applies patches.
- `toolchain/provenance.py` calculates hashes and writes portable LF metadata.
- `toolchain/builders/cargo.py` builds declared Cargo binaries.
- `toolchain/builders/python_pyinstaller.py` freezes declared Python console
  entrypoints.

Builders return produced asset paths. They do not publish releases or know
about GitHub Actions. Adding another build ecosystem requires a new typed
builder and tests rather than manifest-provided shell commands.

The current RLM-specific script and workflow are replaced by this common
contract; they do not remain as a parallel implementation.

## Manifest contract

Each `manifests/<tool>.json` uses schema version 2 and declares:

- `name`, upstream `version`, and integer `buildRevision`;
- `source.repository`, `source.tag`, and the exact 40-character
  `source.commit`;
- the SPDX license identifier and an explicit mapping from upstream license or
  notice paths to unique release-asset names;
- an ordered `patches` array;
- one typed `builder` object;
- a target map for `darwin-arm64`, `linux-x64`, and `win-x64`;
- the expected release assets and native smoke arguments.

Every patch entry contains a repository-relative path below
`patches/<tool>/` and its lowercase SHA-256. An empty array is valid and is
required when no patches apply.

The Cargo builder declares an exact Rust version, locked dependency use, and
one or more Cargo binary-to-asset mappings. Per-target data contains the
GitHub runner, Rust target triple, executable suffix, narrowly scoped build
environment, and typed setup flags such as installing musl build tools. The
builder always runs Cargo with `--locked`.

The Python/PyInstaller builder declares exact Python, uv, and PyInstaller
versions plus console-entrypoint-to-asset mappings. It installs upstream
runtime dependencies from the committed lock file in frozen mode.

Manifest asset names use one normalized convention:

- `bsl-analyzer-{target}` with `.exe` on Windows;
- `v8-runner-{target}` with `.exe` on Windows;
- `rlm-tools-bsl-{target}` and `rlm-bsl-index-{target}`, with `.exe` on
  Windows.

`v8-runner` is published as a raw executable. Its upstream archive also
contains README and examples, but Unica consumes only the binary; retaining the
archive would preserve an unnecessary second packaging model.

## Source and patch processing

Source preparation follows a fixed sequence:

1. Fetch the declared upstream tag and check out the declared commit in
   detached HEAD state.
2. Verify `HEAD` equals the manifest commit before making any change.
3. Resolve every patch path and reject paths outside `patches/<tool>/`.
4. Verify each patch SHA-256.
5. Run `git apply --check` and then `git apply` for each patch in manifest
   order.
6. Stage the patched working tree only to calculate `git write-tree`; do not
   create a synthetic source commit.
7. Pass the prepared tree to the selected builder.

Missing patches, hash mismatches, stale patches, path escapes, reordered
dependencies between patches, or a wrong upstream commit stop the build before
compilation. Patch discovery never uses globs: the manifest order is the only
order.

## Provenance and release contract

Every target publishes:

- its normalized executable assets;
- `checksums-<tool>-<target>.txt` using LF line endings;
- `provenance-<tool>-<target>.json`.

Each release also contains the manifest-declared license and notice files under
unique, tool-prefixed asset names. These files are copied from the same pinned
source tree; release file-set validation treats them as required assets.

Provenance schema version 2 records:

- the release tag and target;
- upstream repository, tag, and commit;
- ordered patch paths and hashes;
- the resulting Git tree hash;
- builder kind and exact tool versions;
- target triple and relevant build environment;
- every asset name, byte size, and SHA-256.

Release tags are independent:

- `bsl-analyzer-v<upstream-version>-build.<revision>`;
- `v8-runner-v<upstream-version>-build.<revision>`;
- `rlm-tools-bsl-v<upstream-version>-build.<revision>`.

Consumers must pin a tag and asset SHA-256. They never use `latest`.

The release workflow also emits GitHub build-provenance attestations for the
published executables. Machine-readable repository provenance remains the
portable contract; GitHub attestations supplement it.

## GitHub Actions

### Pull-request CI

The existing source-verification workflow is extended to run:

- unit and contract tests;
- Python compilation checks;
- manifest validation for every checked-in tool;
- patch path and SHA-256 validation;
- source preflight for every manifest, including checkout of the exact upstream
  commit and verification of declared license files; manifests with patches
  additionally run `git apply --check`;
- `actionlint`.

Tests for source and patch behavior use temporary local Git repositories so
success and failure cases remain deterministic. Pull requests do not build the
production three-platform matrices.

### Tool release

A single manually dispatched workflow accepts a tool name matching a manifest
stem. It validates the manifest and produces a dynamic target matrix.

Before starting expensive builds, the workflow checks that the calculated tag
and GitHub Release do not already exist. An existing tag or release is an error;
published builds are immutable and are never updated in place.

Each target job:

1. prepares the pinned source and patches;
2. installs the exact builder toolchain;
3. builds only the declared assets;
4. runs each native smoke command on that target's runner;
5. generates checksums and provenance;
6. uploads a seven-day workflow artifact.

The release job runs only after the complete target matrix succeeds. It checks
the exact expected file set, creates attestations, and publishes a non-latest
release. A failed or partial matrix cannot publish a release.

## Error handling

- Schema errors identify the manifest path and field.
- Source errors report expected and actual commits.
- Patch errors identify the patch and distinguish missing file, hash mismatch,
  and apply failure.
- Builder identity mismatches stop before compilation.
- Missing or unexpected assets stop before upload and again before release.
- Missing declared license or notice files stop source preflight and release.
- Smoke-test failures fail the corresponding target and therefore block the
  release.
- Metadata is written with explicit UTF-8 and LF line endings on every OS.
- A release collision fails without mutating the existing tag or release.

## Testing strategy

Test-driven implementation covers these contracts before production changes:

- schema version 2 loading and rejection of incomplete manifests;
- independent release-tag calculation;
- builder selection and rejection of unsupported kinds;
- normalized expected assets for all three tools and targets;
- required license/notice asset mapping and missing-file rejection;
- exact source-commit verification;
- patch path confinement, hash checking, order, successful application, and
  stale-patch rejection;
- patched-tree hashing;
- Cargo command construction with exact toolchain, `--locked`, target, binary,
  and environment;
- Python frozen-environment and PyInstaller entrypoint construction;
- provenance contents and LF checksum files;
- release file-set validation and collision rejection;
- repository workflow contracts and `actionlint`.

Release validation downloads the published assets into a temporary directory,
checks every SHA-256 and provenance document, and executes native smoke tests.
Unica then supplies the cross-platform integration proof through its existing
three `Build tools` jobs, package jobs, and thin-bootstrap probes.

## Unica migration

The toolchain releases are published and verified before any consumer change:

1. Publish a new RLM release from the common builder contract.
2. Publish `bsl-analyzer` and `v8-runner` releases from their pinned source
   commits.
3. Verify the exact remote asset set, checksums, provenance, attestations, and
   native smoke tests.
4. Update one Unica pull request so all external tools retain upstream
   repository/tag/commit as source provenance while using
   `IngvarConsulting/unica-toolchain` as `assetRepository` with independent
   `assetTag` and SHA-256 values.
5. Make all external tools use `direct-release-asset`.
6. Remove the now-unused archive extraction strategy and its tests from Unica.
7. Run and require the complete Unica pull-request workflow before merging.

This separation avoids a provenance mistake: `repository` continues to mean
where the source came from, while `assetRepository` means where the controlled
binary supply is published.

## Rollback

Toolchain releases are additive and immutable. If one new build is unsuitable,
Unica can restore the previous asset tag and hashes without rewriting release
history. The existing RLM `build.2` release remains available while the common
builder release is validated. Upstream assets also remain available as an
emergency reference, but they are not the normal Unica supply path after the
migration.

## Acceptance criteria

- The repository has no RLM-only orchestrator or release workflow.
- All three manifests pass schema and semantic validation.
- Patches, including an empty patch set, are represented by the same contract.
- Each tool can be manually released without rebuilding either of the others.
- Every release contains exactly the normalized three-platform executable set
  for that tool plus checksums, provenance, and declared license/notice assets.
- All production assets come from pinned source and pass native smoke tests.
- Existing tags/releases cannot be overwritten by the workflow.
- Unica obtains `bsl-analyzer`, `v8-runner`, and both RLM executables from
  independently tagged `unica-toolchain` releases with pinned SHA-256 values.
- Unica has no remaining archive or source-build path for those external tools.
- Pull-request CI does not run production platform builds.
