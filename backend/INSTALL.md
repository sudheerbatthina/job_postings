# Backend install

`python-jobspy` pins `numpy==1.26.3`, which has no Python 3.13 wheel on
Windows/macOS. Install in two steps so pip never tries to resolve that pin:

```bash
pip install -r requirements.txt
pip install python-jobspy --no-deps
```

Run locally:
```bash
uvicorn app.main:app --reload
```

On Railway this is handled automatically by `nixpacks.toml`.
