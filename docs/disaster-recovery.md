# Fresh-pod disaster recovery

This procedure assumes the previous pod and all of its local files are gone.

## What must exist outside the old pod

- The Azure storage account and Blob container.
- A valid HTTPS container SAS URL, or authority to generate a replacement.
- The original Kopia repository password.
- The Podvault wheel/bootstrap files, or another trusted copy of this source.
- The project name, for example `newlm`.

The old source path, hostname, Podvault config, and receipt are useful but not
required. A replacement SAS may have a different signature and expiry as long
as it points to the same account and container. A replacement repository
password will not substitute for the original encryption password.

## Recovery checklist

1. Create a pod with enough free disk space for the restored project plus
   temporary overhead.
2. Install the pinned Kopia and Podvault releases as described in
   [bootstrap.md](bootstrap.md).
3. Inject the two secrets without putting them on a command line:

   ```bash
   export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
   export PODVAULT_REPOSITORY_PASSWORD='original-repository-password'
   ```

4. Confirm access without allowing repository creation:

   ```bash
   podvault repository connect azure
   podvault repository status
   podvault list newlm
   ```

5. Restore. The default is the latest snapshot and `/workspace/<project>`:

   ```bash
   podvault restore newlm
   ```

   To recover a particular version or location:

   ```bash
   podvault restore newlm --snapshot PODVAULT_OR_KOPIA_ID --to /workspace/newlm-old
   ```

6. Read the restore receipt, inspect key files, and run the project's own
   integrity checks before resuming work.

Never run `save` when intending only to recover from an uncertain container:
`restore`, `list`, `verify`, and `repository connect` do not initialize an empty
repository.

## Expired SAS

Generate a new container SAS for the same storage account and container. A
read/list SAS is sufficient for recovery. Replace the pod environment secret
and reconnect. The encrypted repository contents do not need to be copied or
rewritten.

An authentication failure with a valid SAS most often means the scope,
permissions, start/expiry time, container, or storage account is wrong. Compare
the non-secret account/container shown by `podvault doctor`; do not publish the
URL while troubleshooting.

## Interrupted restore

An interruption leaves entries beside the requested destination:

```text
.newlm.podvault-restore-<random>/
.newlm.podvault-restore-state-<random>.json
```

The JSON marker identifies the project, selected Kopia manifest/root object,
destination, and staging path. Preserve it while investigating. Podvault 0.1
does not resume or merge partial restores. Move the partial tree to a diagnostic
location or remove it only after inspection, ensure the final destination is
absent or empty, then rerun the restore.

## No Podvault executable available

The data remains an ordinary Kopia repository. Follow
[direct-kopia-recovery.md](direct-kopia-recovery.md) to create a Kopia
connection without exposing the SAS in the process list, list the Podvault tags,
and restore the selected root object.
