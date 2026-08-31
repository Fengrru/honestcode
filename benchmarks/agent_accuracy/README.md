# Agent-accuracy benchmark

A deterministic benchmark that measures how well HonestCode catches common
AI-coding hallucinations.

Each task is a tiny agent episode:

1. The agent writes a file containing a known mistake.
2. `verify_file` is run; we expect it to report the issue.
3. The file is replaced with the corrected version.
4. `verify_file` is run again; we expect silence.

Because the inputs are checked-in source files, the benchmark is fully
reproducible and does not require an LLM API key.

## Run

```bash
cd benchmarks/agent_accuracy
python run.py
```

Output formats:

```bash
python run.py --format markdown   # paste into README
python run.py --format json       # machine-readable
```

## Add a task

Create a directory under `dataset/` with:

```text
dataset/my_task/
  task.json          # name, description, target_file, expected_issue_kind, expected_symbol
  context/           # existing files the agent is supposed to use
    lib/
      foo.py
  broken/
    app.py           # the agent-generated buggy file
  fixed/
    app.py           # the corrected file
```

Then run `python run.py` again.

## Dataset

| task | scenario |
|---|---|
| `invented_method` | Agent calls `UserClient.refresh_token()` which does not exist. |
| `undefined_import` | Agent imports `delete_user` from a module that only exports `create_user`. |
| `wrong_signature` | Agent calls `add(1, 2, 3)` when `add(a, b)` takes two arguments. |
| `invented_module_attr` | Agent calls `Connection.query()` when the class only has `execute()`. |
