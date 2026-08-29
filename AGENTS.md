# xianyu-manager repository instructions

## Environment boundary

- This Mac checkout is a development-only copy.
- Windows remains the only real runtime environment until the separately planned cloud migration is completed.
- The future cloud server is currently scoped only to running `xianyu-manager` for automatic replies and automatic delivery.
- Do not treat this Mac as a replacement production node.

## Never do on this Mac

- Do not start the real Xianyu service, scheduler, automatic reply, or automatic delivery.
- Do not log in to a real Xianyu account or launch a real account browser profile.
- Do not access Xianyu, SiliconFlow, Baidu Netdisk, or other live business services for verification.
- Do not copy or use Windows production `manager.db`, browser profiles, cookies, storage state, tokens, DPAPI files, API keys, logs, locks, or scheduler state.
- Do not run the Windows `.cmd`/PowerShell startup paths as a substitute for local tests.
- Do not run live browser/research/publishing scripts under `scripts/` unless the user explicitly expands the scope and confirms the target environment.

## Allowed development work

- Read and edit source, tests, documentation, and configuration examples.
- Run static checks and unit/local tests that use mocks, fakes, temporary directories, or local loopback test clients.
- Keep test runtime data outside the repository by setting `XIANYU_MANAGER_DATA_DIR` to a fresh temporary directory.
- Use `PYTHONPATH=src` with the project virtual environment unless packaging is added later.
- Any test that might make a real network request must be reviewed first and replaced with a mock or skipped.

## Repository boundaries

- `src/`, `tests/`, `scripts/`, `static/`, and `docs/` are project assets.
- `research/github/` contains read-only third-party reference clones and is intentionally ignored by this repository.
- Runtime state belongs under ignored `data/`; it is not source code.
- The Windows `.cmd` files are migration/reference artifacts and are not Mac startup instructions.

## Safe verification sequence

1. Compile/import checks that do not start the application service.
2. Focused unit tests using temporary data and mocked external calls.
3. Full local test suite only after confirming it does not launch a browser or contact live services.

If a requested verification would cross these boundaries, stop and ask the user before proceeding.
