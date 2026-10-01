# pc-decision

A shared "decision model" brick: one MCP tool, `decide`, that asks a locally served model to pick one
lettered option and returns the probability of each option.

## What it is

A thin client of `mlx-serve`, a local MLX inference server. The plugin loads no model, runs no daemon
and registers no hook: the server and the model lifecycle belong to whoever starts `mlx-serve`. When
the server is not there, the tool says so instead of failing.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `ONE_DECISION_MODEL` | `mlx-serve` model ID to query (an ID, not a path) | none — unset means unavailable |
| `ONE_DECISION_URL` | Base URL of the server | `http://127.0.0.1:11234` |

Only loopback hosts (`127.0.0.1`, `localhost`, `::1`) are accepted. Any other host is refused before a
request is sent, so the state never leaves the machine.

## The tool

`decide(state, question, options, rules=None)`

- `state`: the data to judge. It is sent as data, never as instructions.
- `question`: what to decide.
- `options`: a list of `{"letter": "A", "label": "..."}`.
- `rules`: optional text appended to the question.

Available:

```json
{"available": true, "letter": "A", "probabilities": {"A": 0.65, "B": 0.35}}
```

Unavailable:

```json
{"available": false, "reason": "unreachable"}
```

`reason` is one of `model_unset`, `non_loopback_url`, `unreachable`, `server_error`, `bad_response`.
Every failure ends in this shape: no exception, no output on stderr, and the state is never logged or
echoed back in `reason`.

Probabilities come from the top 5 `logprobs` of the first generated token, summed per option letter
and normalised over the option letters found. A response with none of them is `bad_response`.
They are relative to the option letters only, not a confidence: when a single letter is in the top 5
it reads `1.0` even if other tokens carry most of the probability mass.

## Requirements

`uv` (the server declares its dependencies inline) and a running `mlx-serve` with the model in
`ONE_DECISION_MODEL` available.
