# Backend install

## Railway (production)

Railway reads `.python-version` and provisions Python 3.11, which has a
prebuilt `numpy==1.26.3` wheel, so a plain `pip install -r requirements.txt`
works without any workarounds.

## Local dev on Python 3.13

`python-jobspy` pins `numpy==1.26.3`, which has no Python 3.13 wheel on
Windows/macOS and falls back to a source build that requires a C compiler.
Two options:

**Option A — use a Python 3.11 or 3.12 venv** (matches Railway exactly):
```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Option B — stay on Python 3.13, install jobspy without deps**:
```bash
pip install -r requirements.txt   # installs everything except jobspy
pip install python-jobspy --no-deps
```
`pip check` will warn that numpy doesn't match jobspy's `==1.26.3` pin, but
jobspy runs fine on numpy 2.x.

## Run locally
```bash
uvicorn app.main:app --reload
```
