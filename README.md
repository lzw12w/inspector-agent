# iOS Inspector Agent

Conversational agent that drives a running iOS app via the in-process
**SAInspector HTTP server**. It wraps the inspector with typed errors,
session-level state, an action registry, and an LLM tool-use loop so you can
say things like *"open the home page, find the 立即购买 button, and tap it"*
and see what happens.

This is the **initial scaffold** — focus is on making the chat loop usable.
Figma comparison and patrol planner are not implemented yet (architecture
designed in `docs/architecture.md` of the parent design).

## Install

```bash
cd ios-inspector-agent
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

## Prerequisites

The target iOS app must:

1. Be running in Debug with `SAInspectorHTTPServer` listening on port 8765.
2. For real devices: `iproxy 8765 8765` to forward the port over USB.

Verify connectivity:

```bash
ios-inspector-agent doctor
```

## Use

```bash
# Interactive REPL
ios-inspector-agent chat

# One-shot
ios-inspector-agent chat -m "what page is currently shown? give a short summary"

# List available tools
ios-inspector-agent tools
```

### REPL slash commands

| command   | effect |
|-----------|--------|
| `/reset`  | clear conversation history |
| `/trace`  | show path of trace JSONL for this run |
| `/workdir`| show current run's working directory |
| `/quit`   | exit (also Ctrl-D) |

Every run gets its own workdir under `~/.ios-inspector/runs/run_<ts>/`
containing screenshots and `trace.jsonl`.

## Configuration

`~/.ios-inspector/config.toml` (optional):

```toml
inspector_host = "localhost"
inspector_port = 8765
llm_provider = "anthropic"
llm_model = "claude-sonnet-4-5"
```

Or use env vars: `INSPECTOR_HOST`, `INSPECTOR_PORT`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_MODEL`.

## Safety notes

- `view_modify` is automatically rolled back when the session exits.
- `open_url` and `view_modify` require interactive confirmation by default
  (use `-y` to disable, but be careful with route URLs containing
  `wipe`/`logout`/`delete`/etc).
- Agent has built-in budgets: max 30 taps, max 8 modifications per session.

## Architecture

```
cli.py                     ← REPL + one-shot
agent/loop.py              ← think → act → observe loop
llm/{base, anthropic, scripted}.py   ← provider-agnostic LLM adapter
actions/{base, inspect, interact}.py ← tool implementations + JSON schemas
session/session.py         ← TTL cache, screenshot archiving, undo stack
core/{client, transport, models, errors}.py
                           ← typed HTTP client returning dataclasses
trace/recorder.py          ← per-step JSONL log
```

Adding a new tool: subclass `Action` in `actions/`, declare `name`/
`description`/`schema`, implement `_execute`, register in
`actions/__init__.py::_ALL_ACTIONS`. The LLM will see it on the next run.
