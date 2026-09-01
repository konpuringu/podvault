# Architecture and Kopia mapping

Podvault is orchestration around a local Kopia CLI process. There is no Podvault
server, database, agent, scheduler, or storage format.

## Stable project identity

A real source might be `/workspace/newlm` on one pod and `/mnt/work/newlm` on
another. Podvault snapshots it using Kopia's source override:

```text
podvault@podvault:/projects/newlm
```

Each manifest also carries these tags:

```text
podvault.schema:1
podvault.project:newlm
podvault.snapshot:<random stable UUID>
podvault.version:<Podvault version>
podvault.kopia-version:<Kopia version>
podvault.actual-host:<observed hostname>
podvault.actual-source:<observed path>
```

The virtual source and project tag define history. Host and path are diagnostic
metadata only. The random Podvault snapshot ID remains stable if Kopia rewrites
a manifest while adding a pin; users may also select the current Kopia manifest
ID.

## Repository connection

Podvault parses an HTTPS Azure container SAS URL into account, container,
storage domain, and query token. It constructs Kopia's standard `azureBlob`
storage configuration in memory and passes a reconnect token to
`repository create/connect from-config --token-stdin`. The SAS never appears in
the process argument vector.

The non-secret account/container tuple is stored in Podvault config. The SAS and
repository password come from environment secrets first, then the mode-0600
credential file, then a hidden interactive prompt. Kopia's connection config is
also mode 0600 because it contains sensitive connection material.

## Save transaction

1. Validate a narrow source directory outside Podvault state.
2. Apply the global policy that recognizes `.podvaultignore`.
3. Create an incrementally deduplicated, system-pinned Kopia snapshot using the
   stable virtual source and tags.
4. Reject a manifest reporting failed entries.
5. Run `kopia snapshot verify` for structural verification.
6. Atomically write a success receipt.
7. Only the CLI result layer may print `SAFE TO TERMINATE: YES`.

Any exception after the snapshot starts produces a failure receipt when
possible. A failed verification never reaches the success output path.

## Restore transaction

1. Find project manifests by tags and select latest or an explicit stable/current
   ID.
2. Structurally verify the chosen manifest.
3. Create a sibling recovery-state marker.
4. Ask Kopia to restore the manifest's root object into a randomized sibling
   directory, with atomic file writes, flushes, and all overwrite modes disabled.
5. Compare restored regular-file count, symlink count, directory count, and
   logical size with the signed snapshot summary.
6. Recheck that the final destination is absent or empty.
7. Rename the staged directory into place and write a receipt.

The staging directory and state marker remain on failure so an operator has
evidence and partial data. Podvault never merges into or replaces a nonempty
tree.

## Retention and maintenance

Every 0.1 snapshot has the system pin `podvault.retain-v1`. A user pin adds a
label but is not the only retention mechanism. Ordinary commands pass
`--no-auto-maintenance`; pruning and destructive maintenance are intentionally
outside this release. Kopia still owns encryption, compression, content
addressing, deduplication, repository indexes, and any operator-invoked
maintenance.

## Direct Kopia access

After Podvault connects, its standard Kopia config can be used directly:

```bash
export KOPIA_PASSWORD="$PODVAULT_REPOSITORY_PASSWORD"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/podvault/kopia.repository.config"

kopia --config-file="$CFG" snapshot list --all --show-identical \
  --tags=podvault.project:newlm --json
```

Read `rootEntry.obj` from the selected JSON manifest, then restore it:

```bash
kopia --config-file="$CFG" snapshot restore ROOT_OBJECT_ID /workspace/newlm-recovered
```

This path uses only Kopia's repository format. Podvault-specific tags are plain
Kopia metadata. For reconnecting without an existing config, reinstalling
Podvault is the safest way to feed the SAS without exposing it in a process
listing; a standard-library token generator for a Kopia-only emergency is in
[direct-kopia-recovery.md](direct-kopia-recovery.md).
