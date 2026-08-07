#!/usr/bin/env bash
# Idempotent, opt-in Helm setup. Default mode is checks/manual instructions.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HELM_ROOT=""
DO_INSTALL=0
DO_INIT=0
CHECK_HERDR=0
INSTALL_HERDR=0
YES=0

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [options]
  --install                 explicitly install Helm editable into PYTHON_BIN
  --root PATH               Helm root for --init (never overwrites projects)
  --init                    explicitly initialize the selected Helm root
  --check-herdr             verify Herdr CLI and managed-session availability
  --install-herdr --yes    explicitly request Homebrew Herdr install on macOS
  --yes                     confirm package-manager changes with --install-herdr
  -h, --help                show this help

Without --install or --init this script only checks prerequisites and prints
manual commands. It never changes software, projects, or an active Herdr
session.
EOF
}

while (($#)); do
  case "$1" in
    --install) DO_INSTALL=1 ;;
    --root) shift; [[ $# -gt 0 ]] || { echo "--root needs PATH" >&2; exit 2; }; HELM_ROOT="$1" ;;
    --init) DO_INIT=1 ;;
    --check-herdr) CHECK_HERDR=1 ;;
    --install-herdr) INSTALL_HERDR=1 ;;
    --yes) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Helm requires Python 3.10 or newer")
print(f"Python {sys.version.split()[0]} is suitable")
PY

if [[ "$DO_INSTALL" == 1 ]]; then
  echo "Installing Helm editable with $PYTHON_BIN (explicit --install; no sudo)."
  "$PYTHON_BIN" -m pip install --editable "$ROOT_DIR"
else
  echo "No install requested. Manual install: $PYTHON_BIN -m pip install --editable '$ROOT_DIR'"
fi

if [[ "$DO_INIT" == 1 ]]; then
  if [[ -z "$HELM_ROOT" ]]; then
    echo "--init requires --root PATH" >&2
    exit 2
  fi
  echo "Initializing Helm root (existing projects are preserved): $HELM_ROOT"
  PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m helm init "$HELM_ROOT"
fi

if [[ "$CHECK_HERDR" == 1 || "$INSTALL_HERDR" == 1 ]]; then
  if [[ "$INSTALL_HERDR" == 1 ]]; then
    if [[ "$YES" != 1 ]]; then
      echo "Refusing package-manager changes: add --yes to confirm --install-herdr." >&2
      exit 2
    fi
    if [[ "$(uname -s)" == "Darwin" && -x "$(command -v brew 2>/dev/null || true)" ]]; then
      echo "Running explicitly requested: brew install herdr"
      brew install herdr
    else
      echo "Automatic Herdr install is unsupported here; install it manually, then rerun --check-herdr."
    fi
  fi
  if command -v herdr >/dev/null 2>&1; then
    if [[ "${HERDR_ENV:-}" == "1" ]]; then
      if herdr --help >/dev/null 2>&1; then
        echo "Herdr CLI responds in a managed session; Helm may use Herdr presentation."
      else
        echo "Herdr executable exists but did not pass its live CLI check; Helm will fall back."
      fi
    else
      echo "Herdr executable found, but HERDR_ENV=1 is absent; Helm will safely fall back."
    fi
  else
    echo "Herdr executable not found; Helm will use its terminal/process fallback."
  fi
fi
