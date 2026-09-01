# Bootstrap a fresh pod

Podvault 0.2.0 supports Linux x86-64 and ARM64. Transfer these trusted release
files to the pod:

```text
bootstrap-linux.sh
podvault-0.2.0-py3-none-any.whl
```

They can be downloaded from the public release:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.2.0/bootstrap-linux.sh
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.2.0/podvault-0.2.0-py3-none-any.whl
```

Review the shell script, then install under `~/.local`:

```bash
bash bootstrap-linux.sh podvault-0.2.0-py3-none-any.whl
export PATH="$HOME/.local/bin:$PATH"
podvault --version
kopia --version
azcopy --version
```

The script downloads checksum-pinned Kopia 0.23.1 and AzCopy 10.32.6 release
archives from their official GitHub projects, verifies the architecture's
SHA-256 checksum, installs the transfer binaries, and installs Podvault in a
dedicated virtual environment. It does not use an unversioned latest-download
URL.

Kopia is needed only for Kopia projects; AzCopy is needed only for AzCopy
projects. The bootstrap installs both so a fresh pod can discover either engine
from its remote project record.

## Manual installation

Install Kopia and/or AzCopy using their official verified packages, then install
the wheel in a virtual environment:

```bash
python3 -m venv /opt/podvault-venv
/opt/podvault-venv/bin/pip install ./podvault-0.2.0-py3-none-any.whl
ln -s /opt/podvault-venv/bin/podvault /usr/local/bin/podvault

podvault --version
kopia --version
azcopy --version
```

Set `PODVAULT_KOPIA=/absolute/path/to/kopia` or
`PODVAULT_AZCOPY=/absolute/path/to/azcopy` when a binary is intentionally not on
`PATH`.

## Inject credentials

AzCopy projects need only the container SAS:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
podvault restore newlm
```

Kopia projects also need their original repository password:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/CONTAINER?...'
export PODVAULT_REPOSITORY_PASSWORD='the-original-repository-password'
podvault restore newlm
```

Use RunPod Secrets rather than baking credentials into an image or shell
history. Run `podvault doctor` after credentials are available.
