# Mac development setup

This checkout is for development only. The real `xianyu-manager` service continues to run on Windows until a separate cloud migration is completed.

## Local environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The repository currently uses a `src/` layout without package metadata, so local commands should set:

```bash
export PYTHONPATH="$PWD/src"
```

## Safe local verification

Use a fresh temporary runtime directory so tests never read or create production-style state in the repository:

```bash
export XIANYU_MANAGER_DATA_DIR="$(mktemp -d /tmp/xianyu-manager-dev.XXXXXX)"
python -m compileall -q src tests
python -m pytest
```

Do not start the application server, browser session, scheduler, automatic reply, automatic delivery, publishing scripts, or live research scripts on this Mac. Do not copy Windows production data or secrets into this checkout.

## Codex project

- Project name: `01-闲鱼自动运营-Dev`
- Project folder: `/Users/xujiazhen/Projects/side-business/闲鱼/xianyu-manager`
- First task: read `AGENTS.md`, `README.md`, this file, and the migrated project handoff documents; then produce a read-only takeover report before changing code.
