#!/bin/sh
# Run from Linux/WSL; all mutable runtime data stays on the Linux filesystem.
set -eu
umask 077
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_key=$(printf '%s' "$repo_dir" | sha256sum | cut -c1-12)
venv_dir="/var/tmp/youth-precheck-venv-$(id -u)-$repo_key"
if [ -L "$venv_dir" ]; then
    printf '%s\n' 'Refusing a symlink virtual environment.' >&2
    exit 1
fi
if [ ! -d "$venv_dir" ]; then
    python3 -m venv --without-pip "$venv_dir"
fi
if [ "$(stat -c %u "$venv_dir")" != "$(id -u)" ]; then
    printf '%s\n' 'Virtual environment belongs to another user.' >&2
    exit 1
fi
chmod 700 "$venv_dir"
dependency_key=$(sha256sum "$repo_dir/requirements-dev.lock" | cut -d ' ' -f1)
if [ ! -f "$venv_dir/dependencies.sha256" ] || [ "$(cat "$venv_dir/dependencies.sha256")" != "$dependency_key" ]; then
    pip3 --python "$venv_dir/bin/python" install -r "$repo_dir/requirements-dev.lock"
    printf '%s' "$dependency_key" > "$venv_dir/dependencies.sha256"
fi
cd "$repo_dir"
exec "$venv_dir/bin/python" scripts/run_precheck_demo.py "$@"
