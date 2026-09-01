# Security

## Report a vulnerability

Do not include SAS URLs, repository passwords, Kopia reconnect tokens, storage
keys, or recovered private data in a public issue. Contact the maintainer
privately through the repository's security-reporting channel. Until a public
repository location is assigned, keep the report private and include only
redacted logs.

## Secrets

Podvault needs two independent credentials:

- The Azure container SAS authorizes storage access and can be rotated.
- The Kopia repository password encrypts repository data and generally cannot
  be recovered or replaced without the old password.

Store both in a password manager outside the pod. A RunPod secret exposed as
`PODVAULT_AZURE_SAS_URL` or `PODVAULT_REPOSITORY_PASSWORD` is preferred for
repeatable fresh-pod recovery. `KOPIA_PASSWORD` is accepted as a compatibility
fallback.

Podvault does not accept a literal SAS or password flag. SAS connection material
is sent to Kopia over standard input, repository passwords are supplied through
the child process environment, subprocesses never use a shell, and command
errors are redacted. Local credential and connection files must be mode 0600;
directories are mode 0700.

Environment secrets and local mode bits do not protect against root or another
process running as the same Unix user. Do not share a pod account with untrusted
workloads. Avoid enabling shell tracing while exporting secrets, and never add
the Podvault config directory to a project snapshot.

## SAS scope

Use a dedicated container, HTTPS-only transport, the shortest practical expiry,
and minimum permissions. Saving needs read, write, and list; read-only recovery
needs read and list. Podvault requires a container SAS (`sr=c`) because Kopia
stores a repository as many blobs. An account-wide SAS is broader than needed.

Rotating a stored SAS removes the local, reproducible Kopia connection file so
the next command reconnects with the new credential. It never modifies the
remote repository. An environment SAS always overrides the stored value.

## Repository safety

Kopia performs client-side repository encryption and deduplication. Podvault
does not weaken or replace the format. Ordinary Podvault commands disable
Kopia's automatic maintenance and update checks, and 0.1 contains no delete or
prune operation. This prevents a routine save from unexpectedly expanding into
a destructive maintenance task, but means an operator must plan deliberate
repository maintenance separately.

`SAFE TO TERMINATE: YES` means the snapshot command and required structural
verification succeeded. It does not mean an application was stopped cleanly,
that every mutable file represented a consistent training checkpoint, or that
the Azure account itself has independent redundancy against account deletion.

## Recovery-key drill

Before relying on Podvault:

1. Store the SAS regeneration procedure and repository password outside the
   pod.
2. Save a test project.
3. Create a fresh pod with no copied config.
4. Reinstall Podvault and Kopia and restore with only those secrets.
5. Open representative files and, for high-value data, run a 100% content
   verification or a complete test restore.
