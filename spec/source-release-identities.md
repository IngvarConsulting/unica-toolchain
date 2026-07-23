# Source release identities

## Goal

The toolchain must build immutable native assets from:

- an upstream version published from an official release tag;
- any upstream branch or non-release tag;
- one exact upstream commit.

Release names must state whether the source is an official release or a
nightly source snapshot. Every source remains pinned to a full commit SHA.

## Manifest contract

The manifest schema advances to version 3. `source.tag` is replaced by
`source.kind` and `source.ref`.

An official release source has:

```json
{
  "version": "0.5.2-pre.1",
  "source": {
    "kind": "release",
    "repository": "https://github.com/example/tool",
    "ref": "v0.5.2-pre.1",
    "commit": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

For `kind: release`:

- `version` is required;
- `version` accepts stable and prerelease semantic versions;
- `source.ref` must equal `v` followed by `version`;
- the generated release tag is
  `<tool>-v<version>-build.<buildRevision>`.

A branch or non-release tag source has:

```json
{
  "source": {
    "kind": "nightly",
    "repository": "https://github.com/example/tool",
    "ref": "master",
    "commit": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

For `kind: nightly`:

- `version` is absent;
- `source.ref` may name any branch, tag, or full commit SHA accepted by Git;
- the generated release tag is
  `<tool>-nightly-<source-label>-build.<buildRevision>`.

The manifest continues to require a full lowercase 40-character
`source.commit`. Builds check out `source.ref` and fail unless the resolved
HEAD equals the pinned commit.

## Nightly source labels

Branch and tag refs are normalized into a release-safe slug:

- ASCII letters and digits are lowercased;
- runs of other characters become one hyphen;
- leading and trailing hyphens are removed;
- an empty result is rejected.

Examples:

- `master` becomes `master`;
- `feature/foo` becomes `feature-foo`;
- `refs/tags/test_1` becomes `refs-tags-test-1`.

When `source.ref` is a full commit SHA equal to `source.commit`, the label is
the first 12 hexadecimal characters. For example:

```text
v8-runner-nightly-72d346c0a8fc-build.1
```

The full SHA remains present in the manifest and provenance.

## Migration

All checked-in manifests migrate together to schema version 3:

- existing official release pins use `kind: release`, retain their version,
  and rename `source.tag` to `source.ref`;
- the v8-runner master snapshot uses `kind: nightly`, removes `version`, and
  produces `v8-runner-nightly-master-build.1`.

Nightly and official release revision counters are independent identities.
The first nightly build of a ref starts at `buildRevision: 1`, even when an
official release of the same tool already has later build revisions.

Schema version 2 manifests are rejected rather than guessed or silently
migrated.

## Validation and failure behavior

Manifest loading rejects:

- unknown `source.kind` values;
- missing `version` for release sources;
- a `version` on nightly sources;
- release refs that do not exactly match `v<version>`;
- invalid semantic versions;
- invalid or empty normalized nightly labels;
- a commit ref that differs from `source.commit`.

The release workflow keeps its immutable-tag checks. A normalized-name
collision therefore fails before publishing instead of overwriting an
existing release.

## Tests

Unit and repository-contract tests cover:

- stable and prerelease official release names;
- branch, tag, and exact-commit nightly names;
- ref normalization;
- every invalid manifest combination listed above;
- migration of every checked-in manifest;
- v8-runner identity
  `v8-runner-nightly-master-build.1`;
- source checkout and pinned-commit verification for release, branch, tag,
  and direct-commit refs.

CI source validation and the three-target release workflow remain unchanged
apart from consuming the schema version 3 manifest.
