# Demo: catching an invented API

This is the README demo made concrete.

## Files

* `app/client.py` — the real `UserClient` (has `refresh()` and `refresh_access_token()`).
* `app/login_broken.py` — agent-generated code that calls `client.refresh_token()`.
* `app/login_fixed.py` — the corrected code after verification.

## Run

```bash
cd demos/invented-api
python run.py
```

Expected output:

```text
============================================================
Agent writes code with an invented API...
============================================================
✗ verification failed — 1 issue(s) in app/login_broken.py

  [invented_api] app/login_broken.py:9
  `UserClient.refresh_token()` does not exist.
  did you mean: refresh_access_token()?
  available methods:
    - refresh()
    - refresh_access_token()
  confidence: deterministic · action: revise

============================================================
Agent fixes the code...
============================================================
✓ verification passed — app/login_fixed.py
  1 call site(s) grounded in the repository · deterministic
```

## Record the GIF

The checked-in `demo.gif` was generated with Pillow so it does not require
`ttyd`/`ffmpeg`. To regenerate it:

```bash
python render_gif.py
```

If you prefer the authentic [vhs](https://github.com/charmbracelet/vhs) recording:

```bash
vhs record.tape
```
