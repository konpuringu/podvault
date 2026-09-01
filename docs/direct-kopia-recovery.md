# Emergency recovery using Kopia directly

Podvault stores standard Kopia manifests, objects, policies, and tags. This
procedure is for recovery when Podvault cannot be installed.

If a Podvault-created Kopia config survived, use it directly:

```bash
export KOPIA_PASSWORD='original-repository-password'
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/podvault/kopia.repository.config"
kopia --config-file="$CFG" repository status
```

On a completely fresh machine, Kopia needs an `azureBlob` reconnect token. The
following standalone standard-library snippet reads the full container SAS URL
without echoing, validates its basic shape, and writes the token only into
Kopia's standard input. It does not import Podvault:

```bash
export KOPIA_PASSWORD='original-repository-password'
export KOPIA_CONFIG_PATH="$HOME/.config/podvault-emergency.repository.config"

python3 -c '
import base64, getpass, json, sys
from urllib.parse import parse_qs, unquote, urlsplit
u = urlsplit(getpass.getpass("Azure container SAS URL: ").strip())
p = [unquote(x) for x in u.path.split("/") if x]
q = parse_qs(u.query)
if u.scheme != "https" or not u.hostname or ".blob." not in u.hostname or len(p) != 1:
    raise SystemExit("expected an HTTPS Azure container SAS URL")
if q.get("sr") != ["c"] or not q.get("sig"):
    raise SystemExit("expected a signed container SAS (sr=c)")
account, domain = u.hostname.split(".", 1)
value = {"version":"1","storage":{"type":"azureBlob","config":{
    "container":p[0], "storageAccount":account,
    "storageDomain":domain, "sasToken":u.query}}}
sys.stdout.write(base64.urlsafe_b64encode(json.dumps(value,separators=(",",":")).encode()).decode().rstrip("="))
' | kopia --config-file="$KOPIA_CONFIG_PATH" --no-persist-credentials \
    repository connect from-config --token-stdin --no-check-for-updates
```

The password is in the environment and the SAS goes through a pipe; neither is
in the process argument vector. Protect the resulting config as sensitive:

```bash
chmod 600 "$KOPIA_CONFIG_PATH"
```

List all manifests for a project:

```bash
kopia --config-file="$KOPIA_CONFIG_PATH" snapshot list --all --show-identical \
  --tags=podvault.schema:1 --tags=podvault.project:newlm --json
```

Choose a manifest, note its `rootEntry.obj`, and restore that object to a new,
empty location:

```bash
kopia --config-file="$KOPIA_CONFIG_PATH" snapshot verify MANIFEST_ID
kopia --config-file="$KOPIA_CONFIG_PATH" snapshot restore ROOT_OBJECT_ID \
  /workspace/newlm-emergency
```

Do not use direct maintenance, expiration, or delete commands during an
emergency recovery. First copy out and validate the data you need.
