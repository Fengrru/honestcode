# Publishing HonestCode

Two external channels, each with a one-time setup. Everything else (build,
test, tag) is already automated in this repo.

## 1. PyPI (enables the `uvx honestcode-mcp` one-liner)

The `.github/workflows/publish.yml` workflow builds the sdist/wheel and
publishes on every `v*` tag using [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — no token lives in the repo.

One-time setup on [pypi.org](https://pypi.org):

1. Account settings → **Publishing** → *Add a new pending publisher*:
   - **PyPI project name:** `honestcode`
   - **Owner:** `Fengrru`  **Repository:** `honestcode`
   - **Workflow:** `publish.yml`  **Environment:** `pypi`
2. Bump `honestcode/__init__.py` `__version__`, then tag and push:

   ```sh
   git tag v0.5.0 && git push origin v0.5.0
   ```

The workflow run creates the project and publishes it. Verify with
`pip index versions honestcode` (or `uvx honestcode-mcp --help`).

If you prefer a manual first release instead:

```sh
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/*
```

## 2. Official MCP Registry (discoverability)

`server.json` at the repo root follows the registry schema
(`io.github.Fengrru/honestcode`, PyPI package transport). Publishing needs an
interactive GitHub OAuth login, so run it locally once:

```sh
# install the publisher CLI (one-time)
# see https://github.com/modelcontextprotocol/registry for current binaries
mcp-publisher login github        # opens a browser; grants the io.github.Fengrru namespace
mcp-publisher publish             # validates server.json and submits it
```

Bump the `version` field in `server.json` (keep it in sync with the package
version) and re-run `mcp-publisher publish` for each release. After the first
publish, the server is discoverable at
[registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io).
