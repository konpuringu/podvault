# Podvault

Podvault is a small, safety-focused command-line wrapper around
[Kopia](https://kopia.io/). It incrementally saves named project directories
from ephemeral Linux GPU pods to Azure Blob Storage, verifies each save, and
restores through a temporary directory before putting files in place.

Podvault does not define a backup format or run a server. The repository is an
ordinary, encrypted Kopia repository in your Azure container, so it remains
accessible with Kopia itself.

> **Status:** 0.1.1 is an alpha release. Test the workflow with non-critical
> data before making it your only recovery path.

## The shortest complete workflow

You need two secrets:

1. An **HTTPS, container-scoped Azure SAS URL**, for example
   `https://ACCOUNT.blob.core.windows.net/CONTAINER?...`. It must include read,
   write, and list permissions for saving (`rwl`); read and list are sufficient
   for a read-only restore. Podvault rejects an individual-blob SAS because a
   Kopia repository consists of many blobs.
2. A **Kopia repository password**. This is independent of the SAS and encrypts
   the repository. Losing it can make every snapshot unrecoverable.

On the first pod:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
export PODVAULT_REPOSITORY_PASSWORD='a-long-random-secret-from-your-password-manager'

podvault save /workspace/newlm --name newlm --dry-run
podvault save /workspace/newlm --name newlm --description 'before terminating pod'
```

The first real save derives the account and container from the SAS URL,
connects to the repository, or initializes it if the container does not yet
contain one. It remembers `newlm -> /workspace/newlm` locally. After more work:

```bash
podvault save newlm --description 'after evaluation run'
```

Terminate the pod only when that command exits with status 0 and prints the
exact line:

```text
SAFE TO TERMINATE: YES
```

On a fresh pod, install Podvault and Kopia, inject the same two secrets, then:

```bash
podvault list newlm
podvault restore newlm
```

With no prior local configuration, `restore newlm` reconstructs the Azure
connection from the SAS URL, finds the stable project history, and restores to
`/workspace/newlm`. No source path or old hostname is needed.

For RunPod, put the values in secrets named `PODVAULT_AZURE_SAS_URL` and
`PODVAULT_REPOSITORY_PASSWORD` and expose them as environment variables in each
pod. Then normal saves and restores require only the project name. If you enter
credentials at Podvault's hidden interactive prompts instead, they are stored
in a local mode-0600 file; that is convenient on the current pod but does not
survive deleting it.

## Install

Requirements are Linux, Python 3.9 or newer, and Kopia 0.23.1 or newer.
Podvault never installs or upgrades Kopia during a save or restore.

Install the release wheel:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.1.1/podvault-0.1.1-py3-none-any.whl
python3 -m pip install podvault-0.1.1-py3-none-any.whl
kopia --version
podvault doctor
```

For a fresh Linux pod, the explicit bootstrap helper installs a checksum-pinned
Kopia binary and the supplied wheel under `~/.local`:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.1.1/bootstrap-linux.sh
bash bootstrap-linux.sh podvault-0.1.1-py3-none-any.whl
export PATH="$HOME/.local/bin:$PATH"
podvault doctor
```

See [docs/bootstrap.md](docs/bootstrap.md) for package-manager alternatives and
[docs/disaster-recovery.md](docs/disaster-recovery.md) for a no-old-pod recovery
checklist.

## Azure SAS details

Use a dedicated Azure Blob container when possible. Generate a **service SAS
for the container**, restricted to HTTPS and with a useful expiry time.

- Normal save: read (`r`), write (`w`), list (`l`).
- Restore/list/verify: read (`r`), list (`l`).
- Future repository maintenance may also need delete (`d`). Podvault 0.1 does
  not prune or delete repository data automatically.

Do not paste a SAS into a command-line option. Use an environment secret, a
mode-0600 secret file where offered, or the hidden interactive prompt. To
initialize explicitly instead of allowing the first save to do it:

```bash
podvault repository init azure
```

To connect to an existing repository:

```bash
podvault repository connect azure
podvault repository status
```

When a SAS expires, replace the injected environment secret. For protected
file-based credentials:

```bash
chmod 600 /run/secrets/new-podvault-sas
podvault credentials update --sas-url-file /run/secrets/new-podvault-sas
```

An environment value takes precedence over the protected local credential. If
`PODVAULT_AZURE_SAS_URL` is set, update that environment value (or restart the
pod with the updated RunPod secret) rather than expecting a local update to
override it.

## Commands

```text
podvault repository init azure [--sas-url-file FILE] [--repository-password-file FILE]
podvault repository connect azure [--sas-url-file FILE] [--repository-password-file FILE]
podvault repository status

podvault configure PATH --name PROJECT
podvault save PATH --name PROJECT [--description TEXT] [--dry-run] [--no-progress]
podvault save PROJECT [--description TEXT] [--dry-run] [--no-progress]
podvault list [PROJECT]
podvault restore PROJECT [--latest | --snapshot ID] [--to PATH] [--no-progress]
podvault verify PROJECT [--latest | --snapshot ID] [--sample-percent N] [--no-progress]
podvault pin PROJECT [--latest | --snapshot ID] --label TEXT
podvault credentials update [--sas-url-file FILE] [--repository-password-file FILE]
podvault doctor
```

Add global `--json` for a machine-readable final result and `--config PATH` to
isolate all local Podvault state for a particular setup:

```bash
podvault --json save newlm
podvault --config /secure/podvault/config.json list newlm
```

Live Kopia progress is enabled by default for saves, dry runs, restores, and
verification. During a large save it reports hashing, cached logical data,
uploaded bytes, a rolling estimated total, completion percentage, and ETA once
Kopia has scanned enough content to estimate them. Early updates say
`estimating...`; percentages and ETAs can move as more files are discovered and
deduplication or upload throughput changes. Progress goes to standard error, so
`--json` keeps standard output valid JSON. Use `--no-progress` when a quiet
automation log is preferable.

The filesystem repository commands exist for development and air-gapped tests:

```bash
podvault repository init filesystem /srv/test-repository
podvault repository connect filesystem /srv/test-repository
```

## Saves and verification

`save --dry-run` delegates the scan to Kopia and shows included and excluded
examples, logical sizes, size buckets (which expose unusually large content),
and an upload-time estimate. It does not create a snapshot. On an entirely new,
empty container it may initialize the Kopia repository and ignore policy so the
estimate uses the same rules as the subsequent save.

A real save uses a stable virtual source, uploads incrementally, checks that the
snapshot contains no failed entries, and runs structural verification. The
receipt records the stable Podvault snapshot ID, current Kopia manifest and
root-object IDs, versions, timestamps, source metadata, summary, warnings, and
verification result. Receipts contain no SAS or repository password.

Verification levels are deliberately distinct:

- Every save performs **structural verification**: referenced repository
  objects must be present and readable as repository structures.
- `podvault verify newlm --sample-percent 10` additionally downloads and
  validates content for a sample of files. `100` checks all file contents and
  can incur Azure egress.
- A restore to a separate disk or machine is the strongest migration test. It
  also needs full destination space and may incur egress.

Podvault cannot prove that a training process is quiescent. It warns about
recently modified and incomplete-looking files but does not stop jobs or flush
application-level checkpoint state.

All Podvault 0.1 snapshots receive a system retention pin. `podvault pin` adds a
human label. There is intentionally no pruning command in this release, so
repository usage will grow until maintenance is performed deliberately with
Kopia.

## Restores

Podvault first structurally verifies the selected snapshot, restores it into a
random temporary sibling directory, compares restored file/directory/symlink
counts and logical byte size with the snapshot summary, flushes restored files,
and only then renames the directory into place.

It refuses files, symbolic-link destinations, or nonempty directories. It has
no overwrite mode. An interrupted or failed restore preserves both the staging
directory and a sibling `.podvault-restore-state-*.json` recovery marker; see
the disaster-recovery guide before cleaning those up.

## `.podvaultignore`

Podvault registers `.podvaultignore` as a Kopia dot-ignore file. Rules use
Kopia's pattern syntax: one rule per line, `#` comments, `!` negation, `*`,
`**`, `?`, character ranges, and a leading `/` for the current ignore-file
root. Ignore files may appear at the project root or below it.

Start by copying [examples/podvaultignore](examples/podvaultignore) to your
project as `.podvaultignore`, edit it carefully, and always inspect a dry run.
There are no implicit ML exclusions: a broad checkpoint rule can exclude the
only state you needed to recover.

## Local files

Default locations follow XDG conventions:

- Config: `~/.config/podvault/config.json`
- Protected credentials: `~/.config/podvault/credentials.json`
- Kopia connection config: `~/.config/podvault/kopia.repository.config`
- Cache: `~/.cache/podvault/kopia/`
- Receipts/state: `~/.local/state/podvault/`

The source path validator refuses broad roots and refuses a project that would
contain Podvault's config, credentials, cache, state, or a local test repository.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 setup.py sdist bdist_wheel
```

The default automated integration suite creates a local filesystem-backed
Kopia repository and requires no Azure account. The explicitly opt-in Azure
procedure is documented in [docs/azure-integration-test.md](docs/azure-integration-test.md).

Podvault is licensed under Apache-2.0. See [LICENSE](LICENSE).
