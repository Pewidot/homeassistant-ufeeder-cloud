# Contributing

Commit subjects must start with `feat:` or `fix:`.

Run the checks with Python 3.14:

```sh
python -m pip install -r requirements-test.txt
python -m pytest
ruff check custom_components tests --select E4,E7,E9,F
ruff format --check custom_components tests
```

Tests mock cloud and device traffic. Use synthetic data in tests and issue reports; never commit credentials, session tokens or packet captures.
