# Event catalog

Canonical events are **points** (`tool`, `model`, `commit`); the *timing* is a separate field
(`before` | `wrap` | `after`). LangChain and Cursor names resolve to the same event, so
`on("before_tool")`, `on("preToolUse")` and `on("tool", timing="before")` are one registration.

| point | timings | anchored to | blocks | default delivery | scope | LangChain / Cursor |
|---|---|---|---|---|---|---|
| `session` | before / after | the session lifetime | no | inline | both | sessionStart / sessionEnd |
| `agent` | before / after | one agent invocation | before only | inline | both | before_agent / after_agent |
| `turn` | before / after | one user turn | before only | inline | both | — |
| `compact` | before / after | a context compaction | no | inline | both | — |
| `subagent` | before / after | one child agent run | before only | inline | both | — |
| `model` | before / after / wrap | a model request and its response | before only | inline | both | before_model / after_model / wrap_model_call |
| `model_failure` | after | a failed model request | before only | inline | both | — |
| `tool` | before / after / wrap | one tool execution | before only | inline | both | before_tool / after_tool / wrap_tool_call |
| `tool_failure` | after | a tool that raised or returned an error | before only | inline | both | postToolUseFailure |
| `shell` | before / after | one shell command | before only | subprocess | both | beforeShellExecution / afterShellExecution |
| `file_read` | before | reading a file | before only | inline | both | beforeReadFile |
| `file_edit` | before / after | editing a file | before only | subprocess | both | beforeFileEdit / afterFileEdit |
| `prompt` | before | submitting the user prompt | before only | inline | both | beforeSubmitPrompt |
| `commit` | before / after | creating a git commit | before only | subprocess | both | — |
| `push` | before / after | pushing to a remote | before only | subprocess | both | — |
| `run` | before / after | one factory run | before only | inline | project | — |
| `stage` | before / after | one factory stage | before only | inline | project | — |
| `cron` | before | a scheduled tick | no | durable | global | — |
| `learn` | before / after | one learning pass | no | durable | global | — |
| `wiki` | before / after | one vault sync | no | inline | both | — |

## Case-class names

A name can read like a case class; it resolves to the same `(point, timing)` pair:

```
BeforeToolUse   PostModelCall   DuringToolCall   beforeFileEdit
BeforeCommit    PreShellExecution   postSession   duringModelCall
```

The camelCase suffix is matched case-insensitively against a synonym table (`ToolUse`/`ToolCall`/`Tool`
-> `tool`, `ModelCall`/`LLMCall` -> `model`, `FileEdit` -> `file_edit`, `ShellExecution` -> `shell`),
so the vocabulary is open without making the point ambiguous. The pair stays canonical because the
timing decides whether a handler may block.

## Subscribe

```python
bus.add("tool",  callback=lint,       timing="before", when=r"^git ")
bus.add("tool",  callback=on_result,  timing="after")
bus.add("model", callback=audit,      timing="wrap")
bus.add("commit", callback=size_gate, timing="before", delivery="subprocess",
        script=".githooks/check_commit_size.sh")
```

## Phase semantics

`before`, `wrap` and `after` are **relative to the point's anchor**, and the anchor differs per point:

| phase | means | may |
|---|---|---|
| `before` | the operation has not run yet | change its inputs or **block** it |
| `wrap` | around the operation | call it, skip it, or **replace** its result |
| `after` | the operation has **completed** | observe, escalate, or continue - never un-run it |

So `post_tool` is after the tool returned, `post_model` is after the model responded, and `post_commit`
is after the commit exists. An `after` handler that returns a block is recorded as an **escalation**,
because claiming to have prevented something already done is a lie the trace would repeat.

## Scopes

| scope | file | applies to | may register |
|---|---|---|---|
| **global** | `~/.prime/agent/hooks/*.json` | every project and session | any `both` or `global` event |
| **project** | `.prime/hooks/*.json` (committed) | this repository only | any `both` or `project` event |

Rules:

1. Global hooks run **first**; a global `block` cannot be overridden by a project hook. A project hook
   may add restrictions, never remove them.
2. A `global`-only event (`cron`, `learn`) cannot be registered per project - `resolve()` refuses it.
3. `project`-only events (`run`, `stage`) describe this repository's factory and are noise elsewhere.

## Aliases

Equivalent phase prefixes - all three spellings resolve to the same (point, timing):

```
pre<point>   before_<point>   pre_<point>       -> before
post<point>  after_<point>    post_<point>      -> after
during<point> wrap_<point>    during_<point>    -> wrap
```

```
before_<point>   after_<point>   wrap_<point>          # canonical shorthand
before_agent after_agent before_model after_model wrap_model_call
before_tool after_tool wrap_tool_call                  # LangChain
sessionStart sessionEnd preToolUse postToolUse postToolUseFailure
beforeShellExecution afterShellExecution beforeReadFile beforeFileEdit
afterFileEdit beforeSubmitPrompt afterAgentResponse     # Cursor
cron:<name>                                            # scheduled
```

## Payload

Every handler receives `Event(name, attrs, session, root_id, causation_id, depth)`; the payload column
lists the attributes each point carries. A point with `blocks: no` cannot stop the operation, only
observe it.
