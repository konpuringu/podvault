# Bootstrap on a fresh Linux pod

Podvault deliberately separates installation from backup operations. `save`
and `restore` never download, install, or update executables.

## Release artifacts

Transfer these trusted release files to the pod:

```text
podvault-0.1.1-py3-none-any.whl
bootstrap-linux.sh
```

They can be downloaded from the public release:

```bash
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.1.1/bootstrap-linux.sh
curl -fLO https://github.com/konpuringu/podvault/releases/download/v0.1.1/podvault-0.1.1-py3-none-any.whl
```

Then run:

```bash
bash bootstrap-linux.sh podvault-0.1.1-py3-none-any.whl
export PATH="$HOME/.local/bin:$PATH"
podvault --version
kopia --version
```

The script supports Linux x86-64 and ARM64. It downloads the pinned Kopia
0.23.1 release from the official GitHub project, verifies the architecture's
embedded SHA-256 checksum, installs it under `~/.local/bin`, creates a private
Python virtual environment, and installs the local Podvault wheel. It does not
configure credentials or contact Azure.

Review the script before execution. It requires `python3`, the Python `venv`
module, `curl`, `tar`, and `sha256sum`. On a Debian/Ubuntu image where `venv` is
missing:

```bash
apt-get update
apt-get install -y python3-venv curl ca-certificates
```

## Manual installation

Install Kopia using its official package instructions or verified release
archive, make sure `kopia` is on `PATH`, and then install the Podvault wheel in
an environment of your choice:

```bash
python3 -m venv /opt/podvault-venv
/opt/podvault-venv/bin/pip install ./podvault-0.1.1-py3-none-any.whl
ln -s /opt/podvault-venv/bin/podvault /usr/local/bin/podvault

kopia --version
podvault doctor
```

Podvault requires Kopia 0.23.1 or newer and rejects older versions. Set
`PODVAULT_KOPIA=/absolute/path/to/kopia` if the executable is intentionally not
on `PATH`.

## Secret injection

The repeatable approach is to configure the pod template with environment
variables sourced from your secret manager:

```text
PODVAULT_AZURE_SAS_URL
PODVAULT_REPOSITORY_PASSWORD
```

Verify that neither value appears in image layers, shell history, startup logs,
project `.env` files, or notebook output. After injection, `podvault doctor`
validates the local dependency, SAS shape/expiry, file permissions, and
repository access without printing either secret.
