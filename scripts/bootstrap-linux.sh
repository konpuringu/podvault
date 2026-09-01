#!/bin/sh
set -eu

KOPIA_VERSION=0.23.1
INSTALL_PREFIX=${PODVAULT_INSTALL_PREFIX:-"$HOME/.local"}
DATA_ROOT=${PODVAULT_INSTALL_DATA_ROOT:-"$HOME/.local/share/podvault"}
WHEEL_PATH=${1:-}

if [ -z "$WHEEL_PATH" ] || [ ! -f "$WHEEL_PATH" ]; then
  echo "usage: $0 /path/to/podvault-0.1.1-py3-none-any.whl" >&2
  exit 2
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64)
    KOPIA_ARCH=x64
    KOPIA_SHA256=416d0f84a3dbb321a8b2d8f0997b1a0a6e915babe79ee76fa6e4d2bd1e1c5178
    ;;
  Linux:aarch64|Linux:arm64)
    KOPIA_ARCH=arm64
    KOPIA_SHA256=a4ffbc019e0b0f932e2632054e73ec521dc1e80172a00095369c53ecf4e5a6cb
    ;;
  *)
    echo "unsupported platform: $(uname -s) $(uname -m)" >&2
    exit 4
    ;;
esac

for command_name in python3 curl tar sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "required command not found: $command_name" >&2
    exit 4
  fi
done

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/podvault-bootstrap.XXXXXX")
trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM

ARCHIVE="kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz"
DOWNLOAD_URL="https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/${ARCHIVE}"
curl --fail --location --proto '=https' --tlsv1.2 "$DOWNLOAD_URL" \
  --output "$TEMP_ROOT/$ARCHIVE"
printf '%s  %s\n' "$KOPIA_SHA256" "$TEMP_ROOT/$ARCHIVE" | sha256sum --check --status
tar -xzf "$TEMP_ROOT/$ARCHIVE" -C "$TEMP_ROOT"

mkdir -p "$INSTALL_PREFIX/bin" "$DATA_ROOT"
install -m 0755 \
  "$TEMP_ROOT/kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}/kopia" \
  "$INSTALL_PREFIX/bin/kopia"

python3 -m venv "$DATA_ROOT/venv"
"$DATA_ROOT/venv/bin/python" -m pip install --disable-pip-version-check "$WHEEL_PATH"
ln -sfn "$DATA_ROOT/venv/bin/podvault" "$INSTALL_PREFIX/bin/podvault"

"$INSTALL_PREFIX/bin/kopia" --version
"$INSTALL_PREFIX/bin/podvault" --version
echo "Installed. Add $INSTALL_PREFIX/bin to PATH if it is not already present."
