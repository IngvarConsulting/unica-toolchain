# Source Release Identities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support immutable official-release and nightly build identities for upstream releases, branches, tags, and exact commits.

**Architecture:** Manifest schema version 3 makes source identity explicit with `source.kind` and `source.ref`. Pure manifest helpers validate identity and generate release tags; source checkout and provenance consume the same typed fields. Checked-in manifests migrate atomically before the new v8-runner nightly release is published.

**Tech Stack:** Python 3.12, `unittest`, Git, GitHub Actions, JSON manifests.

## Global Constraints

- Official releases use `<tool>-v<version>-build.<revision>`.
- Nightly builds use `<tool>-nightly-<source-label>-build.<revision>`.
- Stable and prerelease semantic versions are official releases.
- Every source is pinned to a full lowercase 40-character commit SHA.
- Direct commit labels use the first 12 SHA characters.
- Schema version 2 manifests are rejected, not guessed or silently migrated.
- Existing immutable release tags are never overwritten.

---

### Task 1: Manifest identity model

**Files:**
- Modify: `toolchain/manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Produces: `SourceSpec.kind: Literal["release", "nightly"]`
- Produces: `SourceSpec.ref: str`
- Produces: `ToolManifest.version: str | None`
- Produces: `nightly_source_label(manifest: ToolManifest) -> str`
- Produces: `release_tag(manifest: ToolManifest) -> str`

- [ ] **Step 1: Write failing schema and naming tests**

Add cases proving:

```python
self.assertEqual(release_tag(release_manifest("0.5.2-pre.1")), "v8-runner-v0.5.2-pre.1-build.1")
self.assertEqual(release_tag(nightly_manifest("master")), "v8-runner-nightly-master-build.1")
self.assertEqual(release_tag(nightly_manifest("feature/foo")), "v8-runner-nightly-feature-foo-build.1")
self.assertEqual(
    release_tag(nightly_manifest("72d346c0a8fcf8373d9388257d11e6bef0ad70b2")),
    "v8-runner-nightly-72d346c0a8fc-build.1",
)
```

Add rejection cases for invalid kind, release without version, nightly with
version, release `ref != f"v{version}"`, invalid prerelease syntax, empty
normalized nightly label, and direct commit ref unequal to `source.commit`.

- [ ] **Step 2: Verify RED**

Run:

```sh
python3.12 -m unittest tests.test_manifest -v
```

Expected: failures because schema 3, `kind`, `ref`, optional version, and
nightly naming are not implemented.

- [ ] **Step 3: Implement schema 3 and pure naming helpers**

Use a semantic-version pattern accepting `1.2.3` and `1.2.3-pre.1`, validate
the release/ref invariant, normalize nightly refs with lowercase ASCII
alphanumerics and hyphens, and shorten exact commit refs to 12 characters.

- [ ] **Step 4: Verify GREEN**

Run:

```sh
python3.12 -m unittest tests.test_manifest -v
```

Expected: all manifest tests pass.

- [ ] **Step 5: Commit**

```sh
git add toolchain/manifest.py tests/test_manifest.py
git -c commit.gpgsign=false commit -m "feat: model release and nightly source identities"
```

### Task 2: Source checkout and provenance

**Files:**
- Modify: `toolchain/source.py`
- Modify: `toolchain/provenance.py`
- Modify: `tests/test_source.py`
- Modify: `tests/test_provenance.py`

**Interfaces:**
- Consumes: `SourceSpec.kind`, `SourceSpec.ref`, `SourceSpec.commit`
- Produces: provenance source object with `kind`, `ref`, `commit`, and tree

- [ ] **Step 1: Write failing source and provenance tests**

Cover release tag, branch, non-release tag, and full commit refs. Assert that
checkout fetches the declared ref, detaches at the pinned commit, and rejects
a ref that does not contain the pinned commit. Assert provenance contains:

```python
"source": {
    "kind": "nightly",
    "repository": "https://github.com/example/tool",
    "ref": "master",
    "commit": "72d346c0a8fcf8373d9388257d11e6bef0ad70b2",
    "tree": expected_tree,
}
```

- [ ] **Step 2: Verify RED**

Run:

```sh
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false \
python3.12 -m unittest tests.test_source tests.test_provenance -v
```

Expected: failures referencing removed `source.tag` and missing provenance
fields.

- [ ] **Step 3: Implement ref checkout and provenance**

Fetch `source.ref` at depth one. For a direct SHA, fetch that SHA explicitly.
Checkout `source.commit`, verify HEAD exactly, and report errors using the
neutral term `upstream ref`. Write `kind` and `ref` into provenance.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command again. Expected: all source and provenance tests pass.

- [ ] **Step 5: Commit**

```sh
git add toolchain/source.py toolchain/provenance.py tests/test_source.py tests/test_provenance.py
git -c commit.gpgsign=false commit -m "feat: checkout and attest arbitrary source refs"
```

### Task 3: Repository migration and contract documentation

**Files:**
- Modify: `manifests/bsl-analyzer.json`
- Modify: `manifests/rlm-tools-bsl.json`
- Modify: `manifests/v8-runner.json`
- Modify: `tests/test_repository_contract.py`
- Modify: `tests/test_toolchain_cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: schema version 3 identity model
- Produces: v8-runner identity `v8-runner-nightly-master-build.1`

- [ ] **Step 1: Write failing repository-contract expectations**

Expect bsl-analyzer and rlm-tools-bsl to remain official releases. Expect
v8-runner to be:

```python
("nightly", "master", "72d346c0a8fcf8373d9388257d11e6bef0ad70b2",
 "v8-runner-nightly-master-build.1")
```

- [ ] **Step 2: Verify RED**

Run:

```sh
python3.12 -m unittest tests.test_repository_contract tests.test_toolchain_cli -v
```

Expected: failures because checked-in manifests still use schema 2/tag.

- [ ] **Step 3: Migrate manifests and README**

Set all manifests to schema 3. Release manifests use `kind: release` and
`ref: v<version>`. v8-runner removes `version`, uses `kind: nightly`,
`ref: master`, exact commit `72d346c0a8fcf8373d9388257d11e6bef0ad70b2`,
`buildRevision: 1`, and AGPL-3.0-only.

Document both naming forms and examples for release, prerelease, branch,
non-release tag, and direct commit builds.

- [ ] **Step 4: Verify repository**

Run:

```sh
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false \
python3.12 -m unittest discover -s tests -v
python3.12 -m py_compile scripts/*.py toolchain/*.py toolchain/builders/*.py tests/*.py
python3.12 scripts/toolchain.py validate-source \
  --manifest manifests/v8-runner.json --repo-root . \
  --work-dir .build/source-v8-runner --out-dir dist/source-v8-runner
```

Expected: 27 or more tests pass, compilation exits zero, and source
validation reports the pinned commit and AGPL license asset.

- [ ] **Step 5: Commit**

```sh
git add manifests README.md tests/test_repository_contract.py tests/test_toolchain_cli.py
git -c commit.gpgsign=false commit -m "build: migrate manifests to source identities"
```

### Task 4: Publish and consume v8-runner nightly

**Files:**
- Modify in Unica: `plugins/unica/third-party/tools.lock.json`
- Test in Unica: attribution, packaging, and tool download checks selected by existing CI scripts

**Interfaces:**
- Consumes: toolchain release `v8-runner-nightly-master-build.1`
- Produces: three immutable binaries and their SHA-256 pins in Unica

- [ ] **Step 1: Push toolchain branch and open PR**

Push `codex/master-release-identity`, open a PR, and wait for all checks.

- [ ] **Step 2: Merge and dispatch release workflow**

After CI passes, squash-merge and dispatch `release-tool.yml` on `main` with
`tool=v8-runner`. Wait for metadata, all three build jobs, attestation, and
release publication.

- [ ] **Step 3: Verify release assets**

Download the three binaries and checksum files from
`v8-runner-nightly-master-build.1`. Recompute SHA-256 locally, compare with
published checksums and provenance, and smoke-test the local macOS binary.

- [ ] **Step 4: Update Unica lock**

Set v8-runner fields to:

```json
"version": "nightly-master",
"sourceTag": "master",
"sourceCommit": "72d346c0a8fcf8373d9388257d11e6bef0ad70b2",
"assetTag": "v8-runner-nightly-master-build.1"
```

Replace all three asset SHA-256 values with the verified release digests.
Preserve `AGPL-3.0-only` and the packaged license path.

- [ ] **Step 5: Verify Unica consumption**

Run the focused lock, attribution, package, and current-host tool bundle
checks available in the Unica repository. Confirm the downloaded runner
reports help/version successfully and `git diff --check` passes.

- [ ] **Step 6: Report**

Report the toolchain PR, workflow, release URL, exact upstream commit, verified
asset hashes, Unica files changed, tests executed, unrelated dirty files left
untouched, and token usage.
