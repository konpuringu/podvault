# Architecture and storage formats

Podvault is local orchestration around either Kopia or AzCopy. It runs no
server and stores no credentials remotely. A container may hold both formats,
but each project name is permanently associated with one engine.

## Engine discovery

Podvault writes a small record at:

```text
.podvault/projects/PROJECT/project.json
```

It contains the project name, engine, timestamps, and—for AzCopy—the currently
committed generation. A fresh pod reads this record before choosing a backend.
Projects created by 0.1 have no record and default to Kopia for compatibility.
Local and remote engine disagreements are fatal.

The record is written using a minimal standard-library Azure Blob REST client.
Large transfers are always delegated to Kopia or AzCopy.

## Kopia format

Kopia projects use a stable virtual source:

```text
podvault@podvault:/projects/PROJECT
```

Snapshots carry these tags:

```text
podvault.schema:1
podvault.project:PROJECT
podvault.snapshot:RANDOM_STABLE_ID
podvault.version:PODVAULT_VERSION
podvault.kopia-version:KOPIA_VERSION
podvault.actual-host:HOSTNAME
podvault.actual-source:LOCAL_PATH
```

The virtual source and project tag define history. Host and path are diagnostic
metadata. Kopia owns repository encryption, compression, deduplication,
content-defined chunking, manifests, and pins.

For Azure, Podvault parses a container-scoped SAS and constructs Kopia's
standard `azureBlob` reconnect token. The token is sent over standard input;
the repository password is passed in `KOPIA_PASSWORD`. Neither secret is put in
the Kopia command arguments.

### Kopia save

1. Validate the source and scan for recently changing files.
2. Create an incrementally deduplicated, pinned snapshot.
3. Reject failed entries or a missing manifest/root object.
4. Run structural repository verification.
5. Write the success receipt and remote engine record.

### Kopia restore

1. Select and structurally verify the requested snapshot.
2. Validate the destination and create a randomized sibling staging path.
3. Restore with adaptive parallelism up to 32 and overwrite modes disabled.
4. Walk the staged tree and compare logical bytes and entry counts.
5. Rename the staged directory into place and write a receipt.

Normal 0.2 restores intentionally leave Kopia's per-file atomic-write and flush
options disabled. The entire tree is already isolated until validation and
promotion. `--durable` enables both slower options for users who explicitly
need them.

## AzCopy format

Every AzCopy save creates a complete immutable generation:

```text
.podvault/azcopy/v1/projects/PROJECT/snapshots/SNAPSHOT/data/...
.podvault/azcopy/v1/projects/PROJECT/snapshots/SNAPSHOT/manifest.json
```

The manifest records source metadata, description, logical tree summary,
Podvault/AzCopy versions, and the exact data prefix. It contains no SAS.

### AzCopy save commit protocol

1. Validate and scan the complete source tree.
2. Upload to a never-before-used generation using AzCopy recursive copy,
   POSIX-property preservation, symbolic-link preservation, and Content-MD5.
3. Write the immutable generation manifest.
4. Replace the small project record so it points to the new generation.
5. Write a success receipt.

A failure before step 4 leaves the prior current-generation pointer unchanged.
Orphaned completed generations are harmless. Podvault 0.2 does not delete or
prune generations.

### AzCopy restore

1. Resolve the current or explicitly requested generation and validate its
   manifest.
2. Download into a randomized sibling staging path with AzCopy's MD5 mismatch
   behavior set to fail.
3. Compare the restored tree with the manifest.
4. Rename the staged directory into place and write a receipt.

`AZCOPY_CONCURRENCY_VALUE` defaults to `AUTO`. Because the whole tree is staged,
`AZCOPY_DOWNLOAD_TO_TEMP_PATH` defaults to `false` to avoid redundant per-file
temporary paths. Users may override either environment variable.

Unlike the Kopia reconnect token, SAS authentication must appear in AzCopy's
Azure URL and is therefore transiently visible in the AzCopy child process's
arguments. Output and errors are redacted.

## Restore failure state

Both engines write a sibling `.podvault-restore-state-*.json` marker before
transfer and preserve the marker and staging directory on interruption or
failure. The requested destination is not exposed until verification passes.

## Receipts

Receipts live under the XDG state directory and include the engine, operation,
stable snapshot ID, tree summary, timing information, and engine-specific
identifiers. They never include SAS URLs or repository passwords.

## Direct Kopia access

Kopia projects remain ordinary repositories. After Podvault connects:

```bash
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/podvault/kopia.repository.config"
kopia --config-file="$CFG" snapshot list --all --show-identical \
  --tags=podvault.schema:1 --tags=podvault.project:newlm --json
```

See [direct-kopia-recovery.md](direct-kopia-recovery.md) for reconstructing a
connection without Podvault.
