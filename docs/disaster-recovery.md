# Fresh-pod disaster recovery

This procedure assumes the previous pod and all local Podvault state are gone.

## What must exist outside the old pod

- The Azure storage account and Blob container.
- A valid HTTPS container SAS URL, or authority to generate one.
- The project name, for example `newlm`.
- For a Kopia project, the original repository password.
- A trusted Podvault release or source checkout.

The remote project record lets Podvault discover the engine and current
AzCopy generation. Projects created with 0.1 have no record and default to
Kopia. The old source path, hostname, local configuration, and receipt are not
required.

## Recovery checklist

1. Create a pod with enough free disk space for the restored project and its
   staging directory.
2. Install Podvault, Kopia, and AzCopy as described in
   [bootstrap.md](bootstrap.md).
3. Inject the SAS without putting it on a Podvault command line:

   ```bash
   export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
   ```

   If the project uses Kopia, also inject the original repository password:

   ```bash
   export PODVAULT_REPOSITORY_PASSWORD='original-repository-password'
   ```

4. Discover and restore:

   ```bash
   podvault list newlm
   podvault restore newlm
   ```

   Kopia restores keep ownership with the current user by default, so this
   works without root when the destination parent is writable. Use
   `--preserve-owners` only when exact saved UID/GID restoration is required
   and the invoking account has permission to change ownership.

   To recover another generation or location:

   ```bash
   podvault restore newlm --snapshot PODVAULT_SNAPSHOT_ID --to /workspace/newlm-old
   ```

   When only one subtree is needed, inspect it first and restore only that
   directory:

   ```bash
   podvault tree newlm --path checkpoints --recursive
   podvault restore newlm --path checkpoints/run-42 --to /workspace/run-42
   ```

   Selective restore requires an explicit `--to`. It uses the same credentials
   as a full restore and does not change the remembered full-project path.

5. Review the receipt, inspect key files, and run the project's own integrity
   checks before resuming work.

Never run `save` when intending only to inspect an uncertain container.

## Expired SAS

Generate a new container SAS for the same storage account and container. A
read/list SAS is sufficient for recovery. Replace the pod environment secret;
the stored data does not need to be rewritten.

An authentication failure with a valid SAS usually means the scope,
permissions, start/expiry time, container, or account is wrong. Do not publish
the SAS URL while troubleshooting.

## Interrupted restore

An interruption leaves entries beside the requested destination:

```text
.newlm.podvault-restore-<random>/
.newlm.podvault-restore-state-<random>.json
```

The JSON marker identifies the engine, project, snapshot, destination, and
staging path. Podvault does not merge a partial tree into a later restore. Keep
the files while investigating, then move or remove the partial tree only after
inspection and rerun the restore into an absent or empty destination.

AzCopy also keeps job plan files in Podvault's protected state directory. They
can help diagnose or manually resume a transfer while the pod still exists,
but they are not expected to survive deletion of an ephemeral pod.

## No Podvault executable available

For Kopia projects, follow
[direct-kopia-recovery.md](direct-kopia-recovery.md).

For AzCopy projects, the direct data prefix is recorded in:

```text
.podvault/projects/PROJECT/project.json
.podvault/azcopy/v1/projects/PROJECT/snapshots/SNAPSHOT/manifest.json
```

Download the selected snapshot's `data/` virtual directory recursively with
AzCopy, enabling symbolic-link and POSIX-property preservation. Verify the
result against the manifest summary before using it.
