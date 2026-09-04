# Podvault

Podvault saves named project directories from ephemeral Linux GPU pods to
Azure Blob Storage and restores them safely through a temporary directory. A
project chooses one storage engine on its first save:

- **Kopia** (default): encrypted, compressed, deduplicated, incremental
  snapshots and inexpensive history.
- **AzCopy**: direct, high-concurrency Azure transfers optimized for restore
  throughput. Every save is a complete immutable generation.

The engine is stored in both local configuration and a small remote project
record. Later commands need only the project name; Podvault refuses to open an
existing project with the other engine.

> **Status:** 0.4.0 is an alpha release. Test the complete save/restore workflow
> with non-critical data before making it your only recovery path.

## Choose an engine

Use Kopia when storage efficiency, client-side encryption, and multiple
snapshots matter most. Use AzCopy when minimizing idle GPU time during a full
restore matters more than transfer and Blob-storage efficiency.

| | Kopia | AzCopy |
|---|---|---|
| First save | `--engine kopia` or omit | `--engine azcopy` |
| Later saves/restores | project name only | project name only |
| Save format | incremental chunks | complete immutable file-tree generation |
| Encryption | Kopia client-side encryption | Azure server-side encryption |
| Compression/deduplication | yes | no |
| Restore transfer | decrypts and reconstructs chunks | direct Azure-to-filesystem copy |
| `.podvaultignore` | yes | no; the complete tree is transferred |
| Remote history | deduplicated snapshots | full generations; storage grows by the project size per save |
| Required secret | SAS + repository password | SAS only |

Projects created by Podvault 0.1 are treated as Kopia projects. An Azure
container may hold both formats, but a particular project name cannot switch
between them.

## Shortest complete workflows

Both engines need an HTTPS, container-scoped Azure SAS URL such as:

```text
https://ACCOUNT.blob.core.windows.net/CONTAINER?...
```

For saves, grant read, create, write, and list (`rcwl`). Restores need read and
list (`rl`). Deletes additionally need delete (`d`); Kopia deletion and its
maintenance pass need read, write, delete, and list (`rwld`), while AzCopy-only
deletion needs read, delete, and list (`rdl`). Podvault rejects account-level,
directory, and individual-blob SAS URLs.

### Fast direct transfers with AzCopy

On the first pod:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'

podvault save /workspace/newlm --name newlm --engine azcopy --dry-run
podvault save /workspace/newlm --name newlm --engine azcopy \
  --description 'before terminating pod'
```

After more work on the same pod:

```bash
podvault save newlm --description 'after evaluation run'
```

On a fresh pod, inject the same container SAS and run:

```bash
podvault list newlm
podvault restore newlm
```

The remote project record tells the fresh installation to use AzCopy and the
restore defaults to `/workspace/newlm`.

### Storage-efficient snapshots with Kopia

Kopia additionally needs a repository password. It is independent of the SAS
and encrypts the repository; losing it can make every Kopia snapshot
unrecoverable.

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
export PODVAULT_REPOSITORY_PASSWORD='a-long-random-secret-from-your-password-manager'

podvault save /workspace/newlm --name newlm --engine kopia --dry-run
podvault save /workspace/newlm --name newlm --engine kopia \
  --description 'before terminating pod'
```

Kopia is the default, so `--engine kopia` may be omitted. On a fresh pod:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
export PODVAULT_REPOSITORY_PASSWORD='the-same-repository-password'

podvault list newlm
podvault restore newlm
```

For either engine, terminate the pod only after a save exits with status 0 and
prints:

```text
SAFE TO TERMINATE: YES
```

## Install

Requirements are Linux and Python 3.9 or newer. Kopia projects require Kopia
0.23.1 or newer; AzCopy projects require AzCopy 10.18.0 or newer.

Install the release wheel when the transfer binaries are already available:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.4.0/podvault-0.4.0-py3-none-any.whl
python3 -m pip install podvault-0.4.0-py3-none-any.whl
podvault doctor
```

The bootstrap helper installs checksum-pinned Kopia 0.23.1, AzCopy 10.32.6,
and the supplied wheel under `~/.local`:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.4.0/bootstrap-linux.sh
bash bootstrap-linux.sh podvault-0.4.0-py3-none-any.whl
export PATH="$HOME/.local/bin:$PATH"
podvault doctor
```

See [docs/bootstrap.md](docs/bootstrap.md) for alternatives and
[docs/disaster-recovery.md](docs/disaster-recovery.md) for recovery details.

## Commands

```text
podvault save PATH --name PROJECT [--engine kopia|azcopy] [--description TEXT]
podvault save PROJECT [--description TEXT]
podvault save ... --dry-run [--no-progress]

podvault list [PROJECT] [--engine kopia|azcopy]
podvault tree PROJECT [--latest | --snapshot ID] [--path DIR] [--recursive]
podvault restore PROJECT [--latest | --snapshot ID] [--to PATH] [--no-progress]
podvault restore PROJECT --path DIR --to PATH
podvault restore PROJECT [--parallel N] [--durable]       # Kopia only
podvault restore PROJECT --preserve-owners               # Kopia; normally needs root
podvault verify PROJECT [--latest | --snapshot ID] [--sample-percent N]
podvault pin PROJECT [--latest | --snapshot ID] --label TEXT  # Kopia only
podvault delete PROJECT [--yes] [--no-progress]
podvault delete PROJECT --snapshot ID
podvault delete PROJECT --through ID
podvault delete PROJECT --before 2026-09-01
podvault delete PROJECT --no-maintenance  # Kopia: defer storage reclamation

podvault configure PATH --name PROJECT [--engine kopia|azcopy]
podvault credentials update [--sas-url-file FILE] [--repository-password-file FILE]
podvault repository init azure [--sas-url-file FILE] [--repository-password-file FILE]
podvault repository connect azure [--sas-url-file FILE] [--repository-password-file FILE]
podvault repository status
podvault doctor
```

`repository` commands manage Kopia repositories and are unnecessary for an
AzCopy-only workflow. The first real Kopia save can initialize an empty
container automatically.

## Browse and selectively restore

Browse the latest saved tree without downloading project files:

```bash
podvault tree newlm
podvault tree newlm --path checkpoints --recursive
```

Without `--recursive`, `tree` lists only the selected directory's immediate
children. Add `--snapshot ID` to browse an older snapshot or generation. The
same SAS read/list permissions are sufficient; Kopia projects also use the
same repository password as every other read operation.

Restore one directory and place its contents directly in a new destination:

```bash
podvault restore newlm \
  --path checkpoints/run-42 \
  --to "$HOME/run-42"
```

The selection must be one project-relative directory. Absolute paths, control
characters, and `..` traversal are rejected. `--to` is required for a
selective restore, and Podvault does not replace the project's remembered
full-restore directory with the selective destination. Multiple selections in
one command are intentionally not supported yet.

Kopia resolves and restores the selected directory object. AzCopy lists and
downloads only the selected generation prefix. Both engines still use an
isolated sibling staging directory and compare the resulting subtree with
remote metadata before exposing it at `--to`.

## Delete snapshots or a project

Delete every remote snapshot or generation for a project with:

```bash
podvault delete newlm
```

Podvault shows the selected engine and requires the exact project name as
confirmation. Use `--yes` for deliberate non-interactive deletion. The command
never deletes the local project directory, but it does remove the project's
local Podvault registration after the final remote version is deleted.

Delete exactly one historical version, or an inclusive prefix of history:

```bash
podvault delete newlm --snapshot 8da83b8bfa65
podvault delete newlm --through 8da83b8bfa65
```

`--through` deletes the selected version and every version with the same or an
earlier timestamp. A short Podvault or Kopia snapshot ID is accepted when it is
unambiguous. To use a time cutoff instead, `--before` is strict:

```bash
podvault delete newlm --before 2026-09-01
podvault delete newlm --before 2026-09-01T12:00:00-07:00
```

A date without a time means midnight UTC. The project record and remembered
local path remain when at least one version survives. If selective deletion
removes the current AzCopy generation, Podvault first repoints the project to
the newest surviving generation; that case requires SAS write permission in
addition to read, list, and delete.

For AzCopy projects, deletion removes the complete project prefix, including
orphaned generations from interrupted saves, and then removes the remote
project record. For Kopia projects, Podvault explicitly deletes every tagged
complete or incomplete snapshot through Kopia; it never removes shared
repository blobs directly. If orphaned AzCopy data has lost its project record,
select it explicitly with `podvault delete PROJECT --engine azcopy`.
Safe full Kopia maintenance runs afterward to reclaim content that is no
longer referenced by any project. Kopia safety windows can defer physical blob
reclamation. Use `--no-maintenance` to make the command finish sooner and run
full maintenance separately later.

Do not save or restore the same project concurrently with deletion. Azure Blob
soft-delete, versioning, or immutability policies can retain deleted data or
prevent immediate physical reclamation.

Add global `--json` for a machine-readable final result and `--config PATH` to
isolate local Podvault state:

```bash
podvault --json save newlm
podvault --config /secure/podvault/config.json list newlm
```

## Faster Kopia restores in 0.2.0

Normal Kopia restores now use adaptive parallelism up to 32 and no longer
force per-file flushes or temporary-file renames. Podvault already restores
into an isolated staging directory, validates the resulting tree, and promotes
the directory only after success, so partially restored data is never exposed
at the requested destination.

Override concurrency when benchmarking a particular pod:

```bash
podvault restore newlm --parallel 16
podvault restore newlm --parallel 32
```

`--durable` restores the conservative 0.1 behavior (`--flush-files` and
`--write-files-atomically`). It is substantially slower on some filesystems
and is intended only when surviving a host power failure during the final
filesystem writeback is more important than restore time.

Receipts separately report repository verification, data transfer, final tree
scan, and total elapsed time.

## Restoring without root

Kopia restores normally skip the saved UID and GID, so files are owned by the
account running Podvault. Permissions (including executable bits), timestamps,
contents, directories, and symbolic links are still restored as far as the
destination filesystem supports them. This makes a project saved by root on
RunPod usable when restored by an ordinary cluster account:

```bash
podvault restore newlm --to "$HOME/newlm"
```

The destination's parent must be writable. Administrators who explicitly need
the original UID/GID can opt in with `--preserve-owners`; restoring owners that
differ from the current account normally requires root.

## AzCopy format and safety model

AzCopy data is stored under:

```text
.podvault/azcopy/v1/projects/PROJECT/snapshots/SNAPSHOT/data/...
```

Each save uses a new snapshot ID. Podvault uploads the complete tree, writes an
immutable manifest, then atomically replaces the small remote project record
that points to the current generation. If an upload fails, the old pointer is
unchanged and the command never prints `SAFE TO TERMINATE: YES`.

AzCopy uploads Content-MD5 values. Restores ask AzCopy to fail on a mismatched
MD5, then Podvault compares logical byte, file, directory, and symbolic-link
counts against the committed manifest before renaming the staging directory
into place. POSIX metadata and symbolic links are preserved using AzCopy's Blob
metadata support.

AzCopy generations are not compressed or deduplicated and Podvault does not
automatically prune them. Each successful save therefore adds approximately
one complete project tree to Azure storage. Use selective `podvault delete`
options to remove old generations, or omit a selector to remove the complete
project. Use a dedicated container and monitor its size.

AzCopy v10 accepts SAS credentials only as part of its Azure URL. Podvault
redacts the SAS from console output and errors, but while an AzCopy child
process is running the URL can be visible to other sufficiently privileged
processes on the same pod through the process table. Use a dedicated pod,
least-privilege container SAS, HTTPS-only access, and a bounded expiry.

Tune AzCopy with its supported environment variables when necessary. Podvault
defaults `AZCOPY_CONCURRENCY_VALUE=AUTO`, disables redundant per-file download
temporary paths because it already stages the whole tree, and keeps AzCopy job
plans and error-only logs in Podvault's protected state directory. Explicit
environment values override these defaults.

## Kopia saves and verification

Kopia snapshots use a stable virtual source and Podvault tags. Every save runs
structural verification and records stable Podvault snapshot, Kopia manifest,
and root-object IDs. Kopia owns encryption, compression, deduplication,
content-defined chunking, and retention pins.

`podvault verify newlm --sample-percent 10` downloads and validates a sample of
Kopia file content; `100` checks all file content. AzCopy projects validate
Content-MD5 during restore and support only structural remote verification.

`podvault pin` is Kopia-only. AzCopy generations are already immutable and
retained until removed manually.

Podvault cannot prove that a training process is quiescent. It warns about
recently modified and incomplete-looking files but does not stop jobs or flush
application-level checkpoint state.

## Restore behavior

Both engines refuse a file, symbolic-link destination, or nonempty directory.
Both restore into a random temporary sibling directory, compare the restored
tree with committed metadata, and only then rename it into place.

For a selective restore, `--path` must identify a directory in the selected
snapshot and `--to` is mandatory. The restored destination contains that
directory's contents, not an additional copy of its parent path.

An interrupted or failed restore preserves the staging directory and a sibling
`.podvault-restore-state-*.json` marker. See the disaster-recovery guide before
removing them.

Live progress is enabled by default and sent to standard error in JSON mode.
Use `--no-progress` for quiet automation. The final receipt contains no SAS or
repository password.

## `.podvaultignore`

`.podvaultignore` applies only to Kopia projects. Rules use Kopia's pattern
syntax: one rule per line, `#` comments, `!` negation, `*`, `**`, `?`, character
ranges, and a leading `/` for the current ignore-file root.

AzCopy projects intentionally transfer the entire source tree. Remove caches
or generated data from the source before saving if they should not be stored.

## Credentials

Do not put a SAS directly in a Podvault command. Prefer a RunPod secret exposed
as `PODVAULT_AZURE_SAS_URL`, a mode-0600 secret file where offered, or the
hidden interactive prompt. For Kopia, expose the repository password as
`PODVAULT_REPOSITORY_PASSWORD`.

When a SAS expires, replace the environment secret. For protected local
credentials:

```bash
chmod 600 /run/secrets/new-podvault-sas
podvault credentials update --sas-url-file /run/secrets/new-podvault-sas
```

An environment value takes precedence over a protected local credential.

## Local files

Default locations follow XDG conventions:

- Config: `~/.config/podvault/config.json`
- Protected credentials: `~/.config/podvault/credentials.json`
- Kopia connection: `~/.config/podvault/kopia.repository.config`
- Kopia cache: `~/.cache/podvault/kopia/`
- Receipts and AzCopy job state: `~/.local/state/podvault/`

The source validator refuses broad roots and a project that would contain
Podvault's configuration, credentials, cache, state, or a local test
repository.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 setup.py sdist bdist_wheel
```

The automated Kopia integration test uses a local filesystem repository. The
Azure integration checklist is in
[docs/azure-integration-test.md](docs/azure-integration-test.md).

Podvault is licensed under Apache-2.0. See [LICENSE](LICENSE).
