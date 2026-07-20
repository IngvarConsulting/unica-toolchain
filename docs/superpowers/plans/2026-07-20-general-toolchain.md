# General Unica Toolchain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the RLM-only pipeline with one typed source-build toolchain for RLM, bsl-analyzer, and v8-runner, publish independent verified releases, and move Unica to those releases.

**Architecture:** A Python orchestrator loads schema-v2 JSON manifests, prepares an exact upstream Git tree plus ordered patches, invokes either a Cargo or Python/PyInstaller builder, and writes normalized assets and provenance. One manual GitHub workflow builds only the selected tool across its target matrix; Unica consumes every external executable as a direct release asset.

**Tech Stack:** Python 3.12 standard library and `unittest`, Git, Cargo/Rust 1.95.0, uv 0.11.29, PyInstaller 6.21.0, GitHub Actions, actionlint.

## Global Constraints

- Release units are independent per tool; no combined toolchain release.
- Manifests contain data for the closed builder set `cargo` and `python-pyinstaller`; manifests contain no arbitrary shell commands.
- Every source uses an exact repository, tag, and 40-character commit.
- Patches are ordered, repository-local under the matching tool directory in `patches/`, and SHA-256 pinned; an empty array is explicit.
- Rust builders use exact Rust `1.95.0` and `cargo build --locked --release`.
- RLM uses Python `3.12.10`, uv `0.11.29`, and PyInstaller `6.21.0` with upstream `uv.lock` in frozen mode.
- Targets are `darwin-arm64`, `linux-x64`, and `win-x64`; Linux v8-runner uses `x86_64-unknown-linux-musl`.
- Metadata is UTF-8 with LF line endings.
- Existing tags and releases are immutable and must never be updated in place.
- Production platform builds run only by explicit `workflow_dispatch`.
- Unica retains upstream repository/tag/commit as source provenance and uses `assetRepository`/`assetTag` for toolchain supply provenance.

---

## File map

### unica-toolchain

- Create `toolchain/manifest.py`: schema-v2 dataclasses, validation, release tags, expected assets and release files.
- Create `toolchain/source.py`: exact source verification, patch validation/application, tree hashing, license copying.
- Create `toolchain/builders/cargo.py`: Cargo command construction, identity validation, executable staging.
- Create `toolchain/builders/python_pyinstaller.py`: frozen uv environment, console entrypoint freezing, executable staging.
- Create `toolchain/provenance.py`: SHA-256, target provenance, portable checksums.
- Create `scripts/toolchain.py`: `describe`, `validate-source`, and `build` CLI commands.
- Replace `manifests/rlm-tools-bsl.json`; add `manifests/bsl-analyzer.json` and `manifests/v8-runner.json`.
- Replace `.github/workflows/release-rlm.yml` with `.github/workflows/release-tool.yml`.
- Update `.github/workflows/ci.yml` and `README.md`.
- Replace RLM-only tests with focused tests matching the modules above.

### Unica

- Modify `plugins/unica/third-party/tools.lock.json`: point bsl-analyzer, v8-runner, and RLM to independently tagged toolchain releases and hashes.
- Modify `scripts/ci/build-unica-tools.py`: remove unused archive extraction support.
- Modify `tests/ci/test_build_unica_tools.py`: require one direct-asset supply contract for every external tool.

---

### Task 1: Schema-v2 manifest model

**Files:**
- Create: `toolchain/__init__.py`
- Create: `toolchain/manifest.py`
- Create: `tests/test_manifest.py`

**Interfaces:**
- Produces: `load_manifest(path: Path) -> ToolManifest`
- Produces: `release_tag(manifest: ToolManifest) -> str`
- Produces: `expected_asset_names(manifest: ToolManifest) -> set[str]`
- Produces: `expected_release_files(manifest: ToolManifest) -> set[str]`
- Produces immutable dataclasses `ToolManifest`, `SourceSpec`, `PatchSpec`, `LicenseSpec`, `TargetSpec`, `CargoBuilderSpec`, `PythonBuilderSpec`, and `BinarySpec`.

- [ ] **Step 1: Write failing schema and naming tests**

Create minimal Cargo and Python manifests in temporary files and assert:

```python
manifest = load_manifest(path)
self.assertEqual(manifest.name, "v8-runner")
self.assertEqual(release_tag(manifest), "v8-runner-v0.5.1-build.1")
self.assertEqual(expected_asset_names(manifest), {
    "v8-runner-darwin-arm64",
    "v8-runner-linux-x64",
    "v8-runner-win-x64.exe",
})
```

Add rejection tests for schema version 1, unsupported builder kind, a non-40-character commit, a missing target, an empty license list, an asset base containing `/`, and Cargo `systemSetup` outside `none` and `musl-tools`.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_manifest`

Expected: `ModuleNotFoundError: No module named 'toolchain'`.

- [ ] **Step 3: Implement the immutable manifest model**

Use a discriminated builder union:

```python
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
```

`BinarySpec` contains `package`, `source_name`, `asset_base`, and `smoke_args`. Generate asset names by joining the asset base, target key, and target executable suffix. Reject unknown object keys so spelling mistakes cannot silently change supply behavior.

- [ ] **Step 4: Verify GREEN**

Run: `python3.12 -m unittest tests.test_manifest`

Expected: all manifest tests pass.

- [ ] **Step 5: Commit**

```bash
git add toolchain/__init__.py toolchain/manifest.py tests/test_manifest.py
git commit -m "Add typed toolchain manifest contract"
```

### Task 2: Exact source and patch pipeline

**Files:**
- Create: `toolchain/source.py`
- Create: `tests/test_source.py`

**Interfaces:**
- Consumes: `ToolManifest`, `PatchSpec`, and `LicenseSpec` from Task 1.
- Produces: `PreparedSource(path: Path, commit: str, tree: str, patches: tuple[AppliedPatch, ...])`.
- Produces: `checkout_source(manifest: ToolManifest, destination: Path) -> Path`.
- Produces: `prepare_source(manifest: ToolManifest, repo_root: Path, source_dir: Path) -> PreparedSource`.
- Produces: `copy_license_assets(manifest: ToolManifest, source: PreparedSource, out_dir: Path) -> list[Path]`.

- [ ] **Step 1: Write failing source tests**

Use temporary Git repositories with two commits. Cover exact commit success and mismatch, an empty patch set, an ordered two-patch set, hash mismatch, stale patch, `../` path escape, absolute path, a patch outside the manifest tool's patch directory, and missing license files.

```python
prepared = prepare_source(manifest, toolchain_root, source_repo)
self.assertEqual(prepared.commit, pinned_commit)
self.assertEqual([item.path for item in prepared.patches], ["patches/demo/0001.patch"])
self.assertEqual(len(prepared.tree), 40)
self.assertEqual((source_repo / "message.txt").read_text(), "patched\n")
```

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_source`

Expected: import failure for `toolchain.source`.

- [ ] **Step 3: Implement source preparation**

`checkout_source` executes argument arrays without a shell:

```python
["git", "init", str(destination)]
["git", "-C", str(destination), "remote", "add", "origin", manifest.source.repository]
["git", "-C", str(destination), "fetch", "--depth", "1", "origin", manifest.source.tag]
["git", "-C", str(destination), "checkout", "--detach", manifest.source.commit]
```

Verify `HEAD`, resolve and hash each patch, run `git apply --check` then `git apply`, stage with `git add -A`, and read `git write-tree`. Never create a synthetic source commit.

- [ ] **Step 4: Verify GREEN**

Run: `python3.12 -m unittest tests.test_source`

Expected: all source and patch tests pass.

- [ ] **Step 5: Commit**

```bash
git add toolchain/source.py tests/test_source.py
git commit -m "Add auditable source patch pipeline"
```

### Task 3: Cargo builder

**Files:**
- Create: `toolchain/builders/__init__.py`
- Create: `toolchain/builders/cargo.py`
- Create: `tests/test_cargo_builder.py`

**Interfaces:**
- Consumes: `CargoBuilderSpec`, `TargetSpec`, and `PreparedSource`.
- Produces: `verify_rust_identity(expected: str, output: str) -> None`.
- Produces: `build_cargo(manifest: ToolManifest, target: str, source: PreparedSource, out_dir: Path, work_dir: Path, runner: Callable = run) -> list[Path]`.

- [ ] **Step 1: Write failing Cargo tests**

Assert exact identity parsing for `rustc 1.95.0 (59807616e 2026-04-14)`, rejection of `1.94.0`, command construction with `--locked --release --package bsl-analyzer --bin bsl-analyzer-app --target aarch64-apple-darwin`, target environment propagation, and staging from Cargo output to normalized assets.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_cargo_builder`

Expected: import failure for `toolchain.builders.cargo`.

- [ ] **Step 3: Implement the Cargo builder**

For each binary, run:

```python
[
    "cargo", "build", "--locked", "--release",
    "--package", binary.package,
    "--bin", binary.source_name,
    "--target", target.target_triple,
    "--target-dir", str(work_dir / "cargo-target"),
]
```

Use the prepared source as `cwd` and `os.environ` plus the validated target environment. Copy and chmod each resulting raw asset.

- [ ] **Step 4: Verify GREEN**

Run: `python3.12 -m unittest tests.test_cargo_builder`

Expected: all Cargo builder tests pass.

- [ ] **Step 5: Commit**

```bash
git add toolchain/builders/__init__.py toolchain/builders/cargo.py tests/test_cargo_builder.py
git commit -m "Add pinned Cargo tool builder"
```

### Task 4: Python/PyInstaller builder

**Files:**
- Create: `toolchain/builders/python_pyinstaller.py`
- Create: `tests/test_python_builder.py`

**Interfaces:**
- Consumes: `PythonBuilderSpec`, `TargetSpec`, and `PreparedSource`.
- Produces: `parse_uv_version(output: str) -> str`.
- Produces: `resolve_entrypoint(python: Path, name: str) -> tuple[str, str]`.
- Produces: `build_python_pyinstaller(manifest: ToolManifest, target: str, source: PreparedSource, out_dir: Path, work_dir: Path, runner: Callable = run) -> list[Path]`.

- [ ] **Step 1: Write failing Python-builder tests**

Cover uv platform metadata parsing, exact Python/uv/PyInstaller identity checks, generated import stub content, `uv sync --frozen --no-dev --directory SOURCE_DIR`, pinned PyInstaller installation, entrypoint module verification, normalized output names, and two RLM binaries from one environment.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_python_builder`

Expected: import failure for `toolchain.builders.python_pyinstaller`.

- [ ] **Step 3: Implement the Python builder**

Drive entrypoints and package names from `PythonBuilderSpec`. Resolve installed console scripts through `importlib.metadata` in the upstream virtual environment. Build a fresh stub and PyInstaller work directory per binary. Verify builder identities before invoking PyInstaller.

- [ ] **Step 4: Verify GREEN**

Run: `python3.12 -m unittest tests.test_python_builder`

Expected: all Python builder tests pass.

- [ ] **Step 5: Commit**

```bash
git add toolchain/builders/python_pyinstaller.py tests/test_python_builder.py
git commit -m "Generalize pinned Python executable builds"
```

### Task 5: Provenance and orchestrator CLI

**Files:**
- Create: `toolchain/provenance.py`
- Create: `scripts/toolchain.py`
- Create: `tests/test_provenance.py`
- Create: `tests/test_toolchain_cli.py`
- Delete: `scripts/build_rlm.py`
- Delete: `tests/test_build_rlm.py`

**Interfaces:**
- Consumes all Task 1-4 interfaces.
- Produces: `write_target_metadata(...) -> tuple[Path, Path]`.
- Produces CLI commands `describe`, `validate-source`, and `build`.

- [ ] **Step 1: Write failing provenance tests**

Assert schema version 2, upstream identity, ordered patches, patched tree, builder identity, target triple/environment, asset sizes/hashes, and LF-only checksum bytes.

- [ ] **Step 2: Verify provenance RED**

Run: `python3.12 -m unittest tests.test_provenance`

Expected: import failure for `toolchain.provenance`.

- [ ] **Step 3: Implement provenance**

Write provenance and checksum filenames from the concrete manifest tool name and target key, with explicit `newline="\n"`. Sort assets by release name but preserve patch order.

- [ ] **Step 4: Write failing CLI tests**

Assert `describe` returns `releaseTag`, `builderKind`, exact builder versions, and a GitHub matrix `include` list. Assert `validate-source` checks out, patches, and copies uniquely named licenses. Assert `build` dispatches on builder kind and rejects unknown targets before source checkout.

- [ ] **Step 5: Verify CLI RED**

Run: `python3.12 -m unittest tests.test_toolchain_cli`

Expected: `scripts/toolchain.py` is missing.

- [ ] **Step 6: Implement CLI and remove RLM-only code**

Dispatch explicitly:

```python
if manifest.builder.kind == "cargo":
    assets = build_cargo(...)
elif manifest.builder.kind == "python-pyinstaller":
    assets = build_python_pyinstaller(...)
else:
    raise SystemExit(f"unsupported builder: {manifest.builder.kind}")
```

`validate-source` copies license/notice assets to its output directory. `build` writes target executables and target metadata.

- [ ] **Step 7: Verify GREEN**

Run: `python3.12 -m unittest tests.test_provenance tests.test_toolchain_cli`

Expected: all provenance and CLI tests pass.

- [ ] **Step 8: Commit**

```bash
git add toolchain/provenance.py scripts/toolchain.py tests/test_provenance.py tests/test_toolchain_cli.py scripts/build_rlm.py tests/test_build_rlm.py
git commit -m "Add general toolchain orchestrator"
```

### Task 6: Three checked-in manifests

**Files:**
- Replace: `manifests/rlm-tools-bsl.json`
- Create: `manifests/bsl-analyzer.json`
- Create: `manifests/v8-runner.json`
- Replace: `tests/test_repository_contract.py`

**Interfaces:**
- Produces release identities `rlm-tools-bsl-v1.26.0-build.3`, `bsl-analyzer-v0.2.55-build.1`, and `v8-runner-v0.5.1-build.1`.

- [ ] **Step 1: Write failing repository contract tests**

Require exact upstream commits, all targets, explicit empty patch arrays, exact builder versions, unique license assets, normalized binaries, and the three release tags.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_repository_contract`

Expected: schema-v1 RLM rejection and missing bsl/v8 manifests.

- [ ] **Step 3: Add schema-v2 manifests**

Use exact pins:

```text
rlm-tools-bsl dcfff95ce678f49971b14d8acd82b042a6855470 v1.26.0
bsl-analyzer  5a02bb44dedaf29e0e29af1f740279d279199854 v0.2.55
v8-runner     ad72f64222ab0a7e6dfd391adb437a956c0a2428 v0.5.1
```

RLM declares `LICENSE`. bsl-analyzer declares `LICENSE-APACHE`, `LICENSE-GPL`, `LICENSE-LGPL`, `LICENSE-MIT`, and `NOTICE`. v8-runner declares `LICENSE`. Prefix every license release name with its tool.

bsl-analyzer maps Cargo package `bsl-analyzer`, binary `bsl-analyzer-app`, to asset base `bsl-analyzer`. Windows uses `CXXFLAGS=/DMAP_FAILED=((void*)-1)` and `RUSTFLAGS=-C target-feature=+crt-static`.

v8-runner maps package/binary `v8-runner`; Linux uses musl setup and `x86_64-unknown-linux-musl`.

- [ ] **Step 4: Verify GREEN**

Run: `python3.12 -m unittest tests.test_repository_contract`

Expected: all repository manifest contracts pass.

- [ ] **Step 5: Commit**

```bash
git add manifests tests/test_repository_contract.py
git commit -m "Declare all Unica external tool builds"
```

### Task 7: Generic CI and release workflow

**Files:**
- Create: `.github/workflows/release-tool.yml`
- Delete: `.github/workflows/release-rlm.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_repository_contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes the common CLI commands.
- Produces manual input `tool` and immutable independent releases.

- [ ] **Step 1: Add failing workflow contract tests**

Require one `release-tool.yml`, no RLM workflow, only `workflow_dispatch`, string input `tool`, dynamic matrix from `describe`, source/license artifact, target builds, native smoke loops, release-collision checks, full file-set validation, attestations, and `softprops/action-gh-release@v3` with `make_latest: false`.

Require PR CI to run unit tests, py_compile, actionlint, and `validate-source` for every manifest without invoking builders.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.test_repository_contract`

Expected: generic workflow assertions fail.

- [ ] **Step 3: Implement workflows**

The metadata job validates `manifests/${tool}.json`, writes `describe` values to `GITHUB_OUTPUT`, runs `validate-source`, and uploads license assets. The build matrix installs exact Python or Rust according to `builderKind`; only Linux rows with `systemSetup=musl-tools` install `musl-tools`. Every build invokes the common CLI and executes declared assets with validated smoke args.

The release job downloads artifacts, compares actual basenames to `expected_release_files`, attests executable paths, and publishes only after the full matrix succeeds.

- [ ] **Step 4: Update README**

Document three tools, independent tags, patch creation using `patches/v8-runner/0001-description.patch` as the concrete example, hash pinning, manual dispatch, published-asset verification, and explicit non-reproducibility wording.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3.12 -m unittest discover -s tests
python3.12 -m py_compile scripts/*.py toolchain/*.py toolchain/builders/*.py tests/*.py
actionlint
git diff --check
```

Expected: all checks pass without warnings or diff errors.

- [ ] **Step 6: Commit**

```bash
git add .github README.md tests/test_repository_contract.py
git commit -m "Generalize toolchain release workflow"
```

### Task 8: Pull request and independent releases

**Files:**
- Modify only if CI reveals a tested defect.

- [ ] **Step 1: Push and create a ready PR**

```bash
git push -u origin codex/general-toolchain-design
gh pr create --base main --head codex/general-toolchain-design --title "Generalize Unica external tool builds" --body-file /tmp/reviewed-toolchain-pr.md
```

- [ ] **Step 2: Wait for every PR check**

Run:

```bash
toolchain_pr=$(gh pr view codex/general-toolchain-design --json number --jq .number)
gh pr checks "$toolchain_pr" --watch --interval 10
```

Expected: no failing or pending checks.

- [ ] **Step 3: Squash-merge**

Run: `gh pr merge "$toolchain_pr" --squash --delete-branch`

Expected: PR state `MERGED`; local `main` fast-forwards to the merge commit.

- [ ] **Step 4: Dispatch each release**

```bash
gh workflow run release-tool.yml -R IngvarConsulting/unica-toolchain -f tool=rlm-tools-bsl --ref main
gh workflow run release-tool.yml -R IngvarConsulting/unica-toolchain -f tool=bsl-analyzer --ref main
gh workflow run release-tool.yml -R IngvarConsulting/unica-toolchain -f tool=v8-runner --ref main
```

- [ ] **Step 5: Verify runs and published assets**

Wait with `gh run watch --exit-status`. For each release, use `gh release view`, download into `mktemp -d`, run checksum files, validate provenance source/tree/builder fields, execute native macOS smoke tests, and verify tags point to the merged commit. Trash temporary directories.

### Task 9: Migrate Unica

**Files:**
- Modify: `/Users/ingvarvilkman/Documents/git/unica/plugins/unica/third-party/tools.lock.json`
- Modify: `/Users/ingvarvilkman/Documents/git/unica/scripts/ci/build-unica-tools.py`
- Modify: `/Users/ingvarvilkman/Documents/git/unica/tests/ci/test_build_unica_tools.py`

**Interfaces:**
- Consumes verified release tags and hashes from Task 8.
- Produces one direct-release-asset contract for every external tool.

- [ ] **Step 1: Branch and write failing supply-contract test**

```bash
git -C /Users/ingvarvilkman/Documents/git/unica switch -c codex/use-general-toolchain-assets
```

Load `tools.lock.json`, exclude internal `unica`, and require every remaining tool to use `https://github.com/IngvarConsulting/unica-toolchain`, an independent `assetTag`, `direct-release-asset`, and lowercase 64-character SHA-256 values.

- [ ] **Step 2: Verify RED**

Run: `python3.12 -m unittest tests.ci.test_build_unica_tools`

Expected: bsl-analyzer and v8-runner violate the new contract.

- [ ] **Step 3: Update lock from verified releases**

Keep upstream `repository`, `sourceTag`, and `sourceCommit`. Add toolchain `assetRepository`, independent `assetTag`, normalized asset names, and downloaded hashes. Change v8-runner to `direct-release-asset` and remove `archiveBinary`.

- [ ] **Step 4: Remove unused archive implementation**

Delete archive imports, `extract_v8_runner`, and the `archive-release-asset` branch. Remove its traversal test; direct URL and checksum behavior remain covered.

- [ ] **Step 5: Verify GREEN locally**

Run:

```bash
python3.12 -m unittest discover -s tests/ci
python3.12 -m py_compile scripts/ci/*.py tests/ci/*.py
actionlint
git diff --check
```

Build a disposable darwin bundle, run `--help` for bsl-analyzer, v8-runner, and both RLM binaries, inspect `tools.json`, and Trash the temporary directory.

- [ ] **Step 6: Commit, push, and open PR**

```bash
git add plugins/unica/third-party/tools.lock.json scripts/ci/build-unica-tools.py tests/ci/test_build_unica_tools.py
git commit -m "Consume all external tools from unica-toolchain"
git push -u origin codex/use-general-toolchain-assets
```

Create a ready PR containing release links and verification evidence.

- [ ] **Step 7: Wait for complete CI and merge**

Run:

```bash
unica_pr=$(gh pr view codex/use-general-toolchain-assets -R IngvarConsulting/unica --json number --jq .number)
gh pr checks "$unica_pr" -R IngvarConsulting/unica --watch --interval 10
```

Expected: Rust tests, source guardrails, three Build tools jobs, packaging, probes, and CodeQL pass. Squash-merge, fast-forward local main, and wait for post-merge CodeQL.

### Task 10: Final audit

**Files:**
- No planned modifications.

- [ ] **Step 1: Verify local state**

Both repositories must be on `main`, match `origin/main`, and have empty `git status --short`.

- [ ] **Step 2: Verify remote contracts**

Confirm three releases, exact asset lists, successful release runs, merged PRs, and Unica lock tags/hashes.

- [ ] **Step 3: Report outcome and limits**

State that inputs are pinned but output bytes are not claimed reproducible, patches are supported although initial patch arrays are empty, and expensive builds occur only during independent manual releases. Report approximate token use because no exact counter is available.
