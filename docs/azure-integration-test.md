# Optional Azure integration test

The automated suite uses a local filesystem repository and never needs cloud
credentials. Run an Azure smoke test only against a dedicated, disposable
container: Podvault 0.1 intentionally has no remote cleanup command, so the test
repository remains until you delete the container through Azure.

1. Create an empty test container and an HTTPS container SAS with read, write,
   and list permissions.
2. Use a unique test repository password and export both secrets.
3. Isolate local state and create a fixture:

   ```bash
   export PODVAULT_AZURE_SAS_URL='https://ACCOUNT.blob.core.windows.net/TEST-CONTAINER?...'
   export PODVAULT_REPOSITORY_PASSWORD='unique-disposable-test-password'
   mkdir -p /tmp/podvault-azure-test/source/sub
   printf 'hello\n' >/tmp/podvault-azure-test/source/file.txt
   ln -s file.txt /tmp/podvault-azure-test/source/link.txt
   ```

4. Exercise initialization, incremental save, content verification, and staged
   restore:

   ```bash
   podvault --config /tmp/podvault-azure-test/pod1/config.json \
     save /tmp/podvault-azure-test/source --name azure-smoke
   printf 'changed\n' >/tmp/podvault-azure-test/source/file.txt
   podvault --config /tmp/podvault-azure-test/pod1/config.json save azure-smoke

   podvault --config /tmp/podvault-azure-test/pod2/config.json list azure-smoke
   podvault --config /tmp/podvault-azure-test/pod2/config.json \
     verify azure-smoke --sample-percent 100
   podvault --config /tmp/podvault-azure-test/pod2/config.json \
     restore azure-smoke --to /tmp/podvault-azure-test/restored

   cmp /tmp/podvault-azure-test/source/file.txt \
       /tmp/podvault-azure-test/restored/file.txt
   test -L /tmp/podvault-azure-test/restored/link.txt
   ```

5. Review both local receipt directories, then delete the dedicated Azure test
   container using Azure's own tooling if it is no longer needed.

Never point this procedure at a production container. An empty container is
required because `save` is allowed to initialize a repository.
