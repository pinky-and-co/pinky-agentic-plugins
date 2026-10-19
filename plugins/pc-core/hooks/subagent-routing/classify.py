#!/usr/bin/env python3
"""Rank a task into a routing-grid category with a local decision model and print its (model, effort).

Usage: `classify.py [--model <decision model id>] [--repo-root DIR] [--timeout N] < task.txt`

The task goes, whole, to the oMLX `POST /v1/systemone` endpoint as a `choice` question whose
criteria are the grid's category descriptions; the top-probability choice is the category. The
classifier only ranks the task against the grid; it never judges its own ability. Prints one JSON
line `{"category", "model", "effort", "model_id", "probabilities"}`. Any failure (server down, HTTP error such
as 507 when memory is short, unparsable answer, category outside the grid) prints the grid's
default category with an `error` field and exits 0, so a caller always gets a usable routing.

Server: `LOCAL_INFERENCE_BASE_URL` (`http://host:port` without `/v1`, loopback hosts only, no built-in default; unset or empty means
no server), redirects refused. Model: `--model`, else `ONE_DECISION_MODEL`, else `clef-flash-4bit`. No timeout unless
`--timeout` is given.

The grid is `grid.json` next to this file; a repo overrides it with `subagent_routing.grid` in its
`.claude/settings.pinky.json`. A missing or invalid override falls back to the plugin grid.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

GRID_PATH = Path(__file__).resolve().parent / "grid.json"
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high")
MODEL_ENV = "ONE_DECISION_MODEL"
URL_ENV = "LOCAL_INFERENCE_BASE_URL"
DEFAULT_MODEL = "clef-flash-4bit"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_RESPONSE_BYTES = 1_000_000
INSTRUCTIONS = (
    "Which category of this grid fits the coding task described in the state, "
    "judging only how demanding the task itself is?"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a loopback server cannot send the request elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Decline every redirect.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            None, which makes urllib raise the redirect response as an HTTP error.
        """
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def valid_grid(grid: object) -> bool:
    """Whether `grid` has a known default and only well-formed categories.

    Args:
        grid: a decoded JSON value.

    Returns:
        True when the grid can be used for classification.
    """
    if not isinstance(grid, dict):
        return False
    cats = grid.get("categories")
    if not isinstance(cats, dict) or not cats:
        return False
    if grid.get("default") not in cats:
        return False
    for name, cat in cats.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][\w-]*", name):
            return False
        if not isinstance(cat, dict):
            return False
        if cat.get("model") not in MODELS or cat.get("effort") not in EFFORTS:
            return False
        if not isinstance(cat.get("description"), str) or not cat["description"]:
            return False
    return True


def load_grid(repo_root: Path) -> dict:
    """Return the repo's valid override grid, else the plugin grid.

    Args:
        repo_root: the repo whose `.claude/settings.pinky.json` may override the grid.

    Returns:
        A valid grid dict.
    """
    policies = load_policies(repo_root) or {}
    routing = policies.get("subagent_routing")
    override = routing.get("grid") if isinstance(routing, dict) else None
    if override is not None and valid_grid(override):
        return override
    return json.loads(GRID_PATH.read_text())


def resolve_base_url() -> str | None:
    """Read the local inference server address from `LOCAL_INFERENCE_BASE_URL`, no default.

    Returns:
        The variable's value, or None when it is unset or empty.
    """
    return os.environ.get(URL_ENV) or None


def is_loopback(url: str) -> bool:
    """Tell whether a base URL is an http(s) URL on a loopback host.

    Same check as `pc-privacy`'s `decision_gate._is_loopback`: plugins are installed apart, so
    one cannot import the other's module.

    Args:
        url: The base URL.

    Returns:
        True when the scheme is http or https and the host is a loopback host.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def build_request_body(task: str, model: str, grid: dict) -> dict[str, Any]:
    """Build the systemone body asking which grid category fits the task.

    Args:
        task: the full task text, never truncated.
        model: the decision-model id.
        grid: a valid grid.

    Returns:
        The JSON-serialisable request body.
    """
    return {
        "model": model,
        "state": task,
        "questions": {
            "cat": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": {
                    name: cat["description"] for name, cat in grid["categories"].items()
                },
            }
        },
        "truncate": False,
    }


@dataclass(frozen=True)
class Answer:
    """The classifier's choice and the probability of each category."""

    category: str
    probabilities: dict[str, float]


def parse_answer(payload: Any, grid: dict) -> Answer:
    """Read the chosen category and its probabilities from a systemone response.

    Args:
        payload: the decoded response.
        grid: a valid grid.

    Returns:
        The chosen category and the probabilities.

    Raises:
        ValueError: when the response has another shape or the category is not in the grid.
    """
    try:
        answer = payload["answers"]["cat"]
        category = answer["choice"]
        probabilities = {str(k): float(v) for k, v in answer["probabilities"].items()}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("unparsable classifier answer") from exc
    if category not in grid["categories"]:
        raise ValueError(f"category {category!r} not in the grid")
    if not all(math.isfinite(p) for p in probabilities.values()):
        raise ValueError("unparsable classifier answer")
    return Answer(category, probabilities)


def post(url: str, body: dict[str, Any], timeout: int | None) -> Any:
    """Send the request and decode the JSON response.

    Args:
        url: the systemone endpoint, already checked as loopback.
        body: the request body.
        timeout: seconds before giving up, or None for no cap.

    Returns:
        The decoded response.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read(MAX_RESPONSE_BYTES))


def classify(
    task: str,
    model: str,
    grid: dict,
    base_url: str | None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Classify `task` and return the routing result, falling back to the grid default.

    Args:
        task: the task text.
        model: the decision-model id.
        grid: a valid grid.
        base_url: the oMLX server, loopback hosts only; None when no address could be resolved.
        timeout: seconds before giving up; None (default) means no cap.

    Returns:
        `{"category", "model", "effort", "model_id"}` plus `probabilities` on success, or `error` when the
        default was used.
    """
    category = None
    probabilities: dict[str, float] = {}
    error = None
    if base_url is None:
        error = "no local inference server address configured"
    elif not is_loopback(base_url):
        error = "server URL is not a loopback http(s) URL"
    else:
        try:
            payload = post(
                f"{base_url.rstrip('/')}/v1/systemone",
                build_request_body(task, model, grid),
                timeout,
            )
            parsed = parse_answer(payload, grid)
            category, probabilities = parsed.category, parsed.probabilities
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
        except (OSError, ValueError, http.client.HTTPException) as exc:
            error = f"{type(exc).__name__}: {exc}"
    result_cat = category or grid["default"]
    cat = grid["categories"][result_cat]
    result: dict[str, Any] = {
        "category": result_cat,
        "model": cat["model"],
        "effort": cat["effort"],
        "model_id": model,
    }
    if error:
        result["error"] = error
    else:
        result["probabilities"] = probabilities
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: task on stdin, one JSON line on stdout, always exit 0.

    Args:
        argv: arguments, defaults to `sys.argv[1:]`.

    Returns:
        The exit code, always 0.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="decision-model id of the classifier")
    parser.add_argument("--repo-root", help="repo whose grid override applies")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="opt-in cap in seconds; no cap by default",
    )
    args = parser.parse_args(argv)
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = Path(args.repo_root) if args.repo_root else resolve_repo_root(project, None)
    grid = load_grid(root)
    model = args.model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    result = classify(sys.stdin.read(), model, grid, resolve_base_url(), args.timeout)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
