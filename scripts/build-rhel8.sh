#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source /etc/os-release
if [[ "${ID:-}" != rhel || "${VERSION_ID:-}" != 8.10 ]]; then
  echo 'Release build requires RHEL 8.10; use make build only for local smoke tests.' >&2
  exit 1
fi
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info[:3] == (3,9,10), "Use Python 3.9.10 for the target release"'
"$python_bin" -m pytest -q
"$python_bin" -m PyInstaller --clean --noconfirm qc-folder-manager.spec
dist/qc-folder-manager --config config.example.yml --check-config
"$python_bin" tests/smoke_binary.py dist/qc-folder-manager
file dist/qc-folder-manager
sha256sum dist/qc-folder-manager > dist/qc-folder-manager.sha256
echo 'RHEL 8.10 binary built. Complete OAuth/SSH UAT before production deployment.'
