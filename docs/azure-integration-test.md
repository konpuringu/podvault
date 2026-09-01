# Optional Azure integration tests

The automated suite does not require Azure. Run these smoke tests only against
a dedicated disposable container because Podvault does not prune remote data.

Create an HTTPS container SAS with read, create, write, and list (`rcwl`)
permissions, export it, and create a fixture:

```bash
export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/TEST-CONTAINER?...'
mkdir -p /tmp/podvault-azure-test/source/sub
printf 'hello\n' >/tmp/podvault-azure-test/source/file.txt
ln -s file.txt /tmp/podvault-azure-test/source/link.txt
```

## AzCopy smoke test

```bash
podvault --config /tmp/podvault-azure-test/az1/config.json \
  save /tmp/podvault-azure-test/source --name az-smoke --engine azcopy

podvault --config /tmp/podvault-azure-test/az2/config.json list az-smoke
podvault --config /tmp/podvault-azure-test/az2/config.json \
  restore az-smoke --to /tmp/podvault-azure-test/az-restored

cmp /tmp/podvault-azure-test/source/file.txt \
    /tmp/podvault-azure-test/az-restored/file.txt
test -L /tmp/podvault-azure-test/az-restored/link.txt
```

Confirm that the second config discovered `azcopy` without an `--engine`
argument.

## Kopia smoke test

Use a separate empty container, or a container whose Kopia repository you are
prepared to retain:

```bash
export PODVAULT_REPOSITORY_PASSWORD='unique-disposable-test-password'

podvault --config /tmp/podvault-azure-test/kopia1/config.json \
  save /tmp/podvault-azure-test/source --name kopia-smoke --engine kopia
printf 'changed\n' >/tmp/podvault-azure-test/source/file.txt
podvault --config /tmp/podvault-azure-test/kopia1/config.json save kopia-smoke

podvault --config /tmp/podvault-azure-test/kopia2/config.json list kopia-smoke
podvault --config /tmp/podvault-azure-test/kopia2/config.json \
  verify kopia-smoke --sample-percent 100
podvault --config /tmp/podvault-azure-test/kopia2/config.json \
  restore kopia-smoke --to /tmp/podvault-azure-test/kopia-restored
```

Review receipts and delete the disposable container with Azure tooling when it
is no longer needed. Never point this procedure at production data.
