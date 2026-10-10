# Reproducible installation and deliberate upgrades

Supported CI matrix: Python 3.12/3.13 on Windows and macOS, Node 22. `.python-version`
selects Python 3.12; `frontend/.nvmrc` selects Node 22. Local validation has also
used Node 24; CI's declared baseline remains 22.

Create a clean virtual environment with a supported Python, then install:

```text
python -m venv .venv
# Activate .venv using your shell's activation command.
python -m pip install -r requirements-dev.txt -c constraints.txt
python -m pip check
python scripts/lock_dependencies.py
cd frontend
npm ci
npm test
npm run build
```

For runtime-only installations use `requirements.txt` instead. The exact
constraints govern direct and transitive Python versions; npm's committed lock
governs frontend dependencies. Do not replace `npm ci` with `npm install` during
routine setup. Playwright Chromium is installed with
`python -m playwright install chromium` when browser features/tests are needed.

To upgrade, create a separate clean environment and explicitly run
`python -m pip install --upgrade -r requirements-dev.txt`. Inspect changes and
release notes, run `pip check`, then `python scripts/lock_dependencies.py --write`.
The script captures exact installed versions without resolving or upgrading
anything itself. It rejects direct-URL installations and retains pins for
dependencies absent on the current OS. Review obsolete pins manually; test the
full OS/Python CI matrix before accepting the new constraints. Run the complete
pytest suite and frontend build/tests before shipping an upgrade. Never generate
constraints from a shared global Python installation.

The check mode fails for installed application packages absent from constraints
or with different versions. Packaging tools (`pip`, `setuptools`, `wheel`, `uv`)
are excluded. Constraints pin releases, not artifact hashes; reproducibility
assumes a trusted package index serving immutable published artifacts.
