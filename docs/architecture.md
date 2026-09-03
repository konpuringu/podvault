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
2. If `--path` is present, walk Kopia directory metadata to prove that the
   normalized relative path exists and is a directory, and capture its summary.
3. Validate the destination and create a randomized sibling staging path.
4. Restore the root object or `ROOT_OBJECT_ID/relative/path` with adaptive
   parallelism up to 32 and overwrite modes disabled.
5. Walk the staged tree and compare logical bytes and entry counts with the
   selected root or subtree summary.
6. Rename the staged directory into place and write a receipt.

Normal restores intentionally leave Kopia's per-file atomic-write and flush
options disabled. The entire tree is already isolated until validation and
promotion. `--durable` enables both slower options for users who explicitly
need them.

### Kopia deletion

Podvault lists complete and incomplete snapshots by the exact project tag,
deletes each manifest using Kopia's confirmed snapshot-deletion operation, and
verifies that no tagged snapshot remains. It then runs full maintenance with
Kopia's default `full` safety level. Maintenance failure is reported separately
because the project is no longer restorable even though unreferenced physical
blobs may remain. Shared content is never deleted directly from Azure.

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
Orphaned completed generations are harmless. Podvault does not prune
generations automatically.

### AzCopy restore

1. Resolve the current or explicitly requested generation and validate its
   manifest.
2. If `--path` is present, validate the remote directory prefix and derive a
   subtree summary from blob names, sizes, and AzCopy's directory/symlink
   metadata.
3. Download the generation root or selected directory prefix into a randomized
   sibling staging path with AzCopy's MD5 mismatch behavior set to fail.
4. Compare the restored tree with the full manifest or subtree summary.
5. Rename the staged directory into place and write a receipt.

`AZCOPY_CONCURRENCY_VALUE` defaults to `AUTO`. Because the whole tree is staged,
`AZCOPY_DOWNLOAD_TO_TEMP_PATH` defaults to `false` to avoid redundant per-file
temporary paths. Users may override either environment variable.

### AzCopy deletion

Podvault recursively removes the project's entire AzCopy prefix, verifies that
the prefix is empty, and deletes the remote project record last. This includes
committed generations and orphaned generations left by interrupted uploads.
The local working directory is outside this flow and is never removed.

Unlike the Kopia reconnect token, SAS authentication must appear in AzCopy's
Azure URL and is therefore transiently visible in the AzCopy child process's
arguments. Output and errors are redacted.

## Remote tree browsing

`podvault tree` uses the selected snapshot or generation without restoring
file contents. For Kopia, immediate directory metadata comes from directory
objects and recursive output comes from Kopia's repository listing. For
AzCopy, non-recursive output uses Azure Blob's delimiter-based hierarchical
listing, while recursive output enumerates only the selected data prefix.

Every user path is normalized as a POSIX project-relative path before it is
combined with a Kopia object ID or Azure blob prefix. Absolute paths, control
characters, and any `..` component are rejected.

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
