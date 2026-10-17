# Event catalog

Canonical events are **points** (`tool`, `model`, `commit`); the *timing* is a separate field
(`before` | `wrap` | `after`). LangChain and Cursor names resolve to the same event, so
`on("before_tool")`, `on("preToolUse")` and `on("tool", timing="before")` are one registration.

| point | timings | LangChain | Cursor | blocks | default delivery | scope | payload |
|---|---|---|---|---|---|---|---|
| `session` | before / after | — | sessionStart / sessionEnd | no | inline | both | session, cwd, project |
| `agent` | before / after | before_agent / after_agent | afterAgentResponse | yes | inline | both | task, result |
| `turn` | before / after | — | — | yes | inline | both | turn, tokens, corrections |
| `compact` | before / after | — | — | no | inline | both | pre_tokens, post_tokens |
| `subagent` | before / after | — | — | yes | inline | both | name, model, ok |
| `model` | before / after / wrap | before_model / after_model / wrap_model_call | — | yes | inline | both | messages, model, tools |
| `model_failure` | after | — | — | yes | inline | both | error, attempt |
| `tool` | before / after / wrap | before_tool / after_tool / wrap_tool_call | preToolUse / postToolUse | yes | inline | both | tool, args |
| `tool_failure` | after | — | postToolUseFailure | yes | inline | both | tool, error |
| `shell` | before / after | — | beforeShellExecution / afterShellExecution | yes | subprocess | both | argv, cwd, exit |
| `file_read` | before | — | beforeReadFile | yes | inline | both | path |
| `file_edit` | before / after | — | beforeFileEdit / afterFileEdit | yes | subprocess | both | path, diff |
| `prompt` | before | — | beforeSubmitPrompt | yes | inline | both | prompt |
| `commit` | before / after | — | — | yes | subprocess | both | staged, loc, files, message |
| `push` | before / after | — | — | yes | subprocess | both | branch, remote |
| `run` | before / after | — | — | yes | inline | project | brief, ok |
| `stage` | before / after | — | — | yes | inline | project | stage, passed, spec |
| `cron` | before | — | — | no | durable | global | name, schedule |
| `learn` | before / after | — | — | no | durable | global | session, turns, corrections |
| `wiki` | before / after | — | — | no | inline | both | vault, created, updated, deleted, skipped |

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
