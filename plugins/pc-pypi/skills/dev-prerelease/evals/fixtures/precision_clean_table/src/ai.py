# Cortex AI wrapper with semantic cache, model routing, and token compression.
# Co-authored with CoCo
"""Cortex AI wrapper with semantic cache, model routing, and token compression."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pinky_core.security import validate_identifier


@dataclass(frozen=True)
class RoutingRules:
    """Task type -> Cortex model routing table.

    Frozen dataclass — safe to use as a class-level constant.
    Override at instantiation time for products with different quality/cost needs.
    See ADR-0003 for model sovereignty rationale (Mistral EU-first, no Meta for minor data).
    """

    factual_simple: str = "mistral-7b"
    explanation: str = "claude-haiku-4-5"
    complex_reason: str = "claude-sonnet-4-6"
    scoring_batch: str = "mistral-large2"

    def resolve(self, task_type: str, default: str = "claude-haiku-4-5") -> str:
        """Return the Cortex model for *task_type*, falling back to *default*."""
        return getattr(self, task_type, default)


@dataclass(frozen=True)
class ModelMap:
    """Cortex model name -> local/EAI equivalent lookup.

    Field names use underscores; call `.get()` with the hyphenated Cortex name.
    """

    mistral_7b: str = "mistral"
    mistral_large2: str = "mistral"
    claude_haiku_4_5: str = "mistral"
    claude_sonnet_4_6: str = "mistral"

    def get(self, model: str, default: str = "mistral") -> str:
        """Return the local/EAI equivalent for *model*."""
        return getattr(self, model.replace("-", "_").replace(".", "_"), default)


ROUTING_RULES: RoutingRules = RoutingRules()
MODEL_MAP: ModelMap = ModelMap()

_VALID_BACKENDS = {"cortex", "ollama", "groq", "task"}

_COMPRESSION_PROMPT = (
    "Summarise the following conversation history into a concise recap that "
    "preserves all key facts, decisions, and context needed to continue the "
    "conversation. Output only the summary, no preamble."
)


class PinkyAI:
    """Cortex AI wrapper for Layer 4 cost optimisation.

    Intercepts every LLM call with three levers:
    - **Semantic cache** — cosine-similarity lookup over AI_EMBED vectors.
    - **Model router** — resolves the cheapest adequate model from task_type.
    - **Token compression** — summarises oldest 2/3 of history at max_tokens.
    """

    def __init__(
        self,
        session: Any,
        backend: str = "cortex",
        cache_table: str | None = None,
        similarity_threshold: float = 0.92,
        cache_ttl_seconds: int | None = None,
        routing: RoutingRules | None = None,
        max_tokens: int = 4096,
        query_tag: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if backend not in _VALID_BACKENDS:
            msg = f"Invalid backend {backend!r}. Must be one of {_VALID_BACKENDS}"
            raise ValueError(msg)
        self._session = session
        self._backend = backend
        self._cache_table = (
            self._validate_qualified(cache_table) if cache_table else None
        )
        self._similarity_threshold = similarity_threshold
        # None → cache entries never expire (lookup ignores age). A positive value bounds
        # both the lookup window and purge_expired().
        self._cache_ttl_seconds = cache_ttl_seconds
        self._routing = routing or ROUTING_RULES
        self._max_tokens = max_tokens
        self._query_tag = query_tag
        self._base_url = kwargs.get("base_url", "http://localhost:11434")
        self._api_key = kwargs.get("api_key", "")
        self._client: httpx.Client | None = None

    @staticmethod
    def _validate_qualified(name: str) -> str:
        """Validate a potentially dotted identifier (e.g. SCHEMA.TABLE)."""
        for part in name.split("."):
            validate_identifier(part)
        return name

    # ── Public API ───────────────────────────────────────────────────────────

    def complete(
        self,
        prompt: str,
        model: str = "mistral-7b",
        task_type: str | None = None,
        history: list[dict[str, str]] | None = None,
        use_cache: bool = True,
        **kwargs: Any,
    ) -> str:
        """Call COMPLETE with semantic cache lookup and optional model routing."""
        resolved_model = self._routing.resolve(task_type) if task_type else model

        if use_cache and self._cache_table:
            cached = self._cache_lookup(prompt)
            if cached is not None:
                return cached

        messages = self._build_messages(prompt, history)
        response = self._call_complete(messages, resolved_model, **kwargs)

        if use_cache and self._cache_table:
            self._cache_store(prompt, response, resolved_model)

        return response

    def extract(
        self, source: str, schema: dict[str, str], *, from_file: bool = False
    ) -> dict[str, Any]:
        """Extract structured fields from text or a staged file using AI_EXTRACT.

        Args:
            source: Text to extract from, or a stage file path
                (``@db.schema.stage/kbis.pdf``) when ``from_file`` is True.
            schema: ``{field_name: description}``. The description guides the extraction
                (e.g. "Legal form, one of SARL/SAS/…") and is passed through as the
                AI_EXTRACT question — it is **not** discarded.
            from_file: Treat ``source`` as a staged file (``TO_FILE``) rather than literal text.

        Returns:
            The parsed ``{"error": …, "response": {field: value}}`` result.

        Raises:
            RuntimeError: If ``from_file`` is set on a non-Cortex backend.
        """
        # AI_EXTRACT wants [[field, question], …] pairs — keep the descriptions.
        response_format = [[key, desc] for key, desc in schema.items()]
        if self._backend == "cortex":
            self._set_query_tag()
            from snowflake.snowpark.functions import ai_extract, lit, to_file

            input_col = to_file(source) if from_file else lit(source)
            row = (
                self._session.range(1)
                .select(ai_extract(input_col, response_format))
                .collect()[0]
            )
            result = row[0]
            return (
                cast(dict[str, Any], json.loads(result))
                if isinstance(result, str)
                else result
            )

        if from_file:
            msg = "extract(from_file=True) requires backend='cortex'"
            raise RuntimeError(msg)
        prompt = (
            f"Extract the following fields from the text below.\n"
            f"Fields: {json.dumps(schema)}\n\nText:\n{source}\n\n"
            f"Return a JSON object with the extracted fields only."
        )
        raw = self._call_complete(
            [{"role": "user", "content": prompt}], "mistral-large2"
        )
        return cast(dict[str, Any], json.loads(raw))

    def classify(self, text: str, categories: list[str]) -> str:
        """Classify text into one of the provided categories using AI_CLASSIFY."""
        if self._backend == "cortex":
            self._set_query_tag()
            from snowflake.snowpark.functions import ai_classify, lit, parse_json

            categories_json = json.dumps(categories)
            row = (
                self._session.range(1)
                .select(
                    # AI_CLASSIFY returns {"labels": [<label>]} (plural array), not {"label": …}.
                    ai_classify(lit(text), parse_json(lit(categories_json)))["labels"][
                        0
                    ].cast("string")
                )
                .collect()[0]
            )
            return str(row[0])

        prompt = (
            f"Classify the following text into exactly one of these categories: "
            f"{categories}\n\nText: {text}\n\nReturn only the category label."
        )
        return self._call_complete(
            [{"role": "user", "content": prompt}], "mistral-7b"
        ).strip()

    def filter(self, text: str, condition: str) -> bool:
        """Check whether text satisfies a natural-language condition using AI_FILTER."""
        if self._backend == "cortex":
            self._set_query_tag()
            from snowflake.snowpark.functions import ai_filter, lit

            # AI_FILTER takes a single natural-language predicate, not (text, condition).
            predicate = (
                f"Text: {text}\nCondition: {condition}\n"
                f"Does the text satisfy the condition?"
            )
            row = (
                self._session.range(1)
                .select(ai_filter(lit(predicate)).cast("boolean"))
                .collect()[0]
            )
            return bool(row[0])

        prompt = (
            f"Does the following text satisfy this condition?\n"
            f"Condition: {condition}\nText: {text}\n\n"
            f"Answer only 'true' or 'false'."
        )
        raw = self._call_complete([{"role": "user", "content": prompt}], "mistral-7b")
        return raw.strip().lower() == "true"

    def embed(self, text: str) -> list[float]:
        """Embed text into a 768-dimension vector using AI_EMBED."""
        if self._backend == "cortex":
            self._set_query_tag()
            from snowflake.snowpark.functions import ai_embed, lit

            row = (
                self._session.range(1)
                .select(ai_embed("e5-base-v2", lit(text)).cast("array"))
                .collect()[0]
            )
            result = row[0]
            return json.loads(result) if isinstance(result, str) else list(result)

        resp = self._http_client().post(
            f"{self._base_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
        )
        resp.raise_for_status()
        return cast(list[float], resp.json()["embedding"])

    def parse_document(self, file_url: str) -> str:
        """Extract text from a staged document using AI_PARSE_DOCUMENT (OCR).

        Args:
            file_url: Stage path to the document (e.g. ``@db.schema.stage/kbis.pdf``).

        Returns:
            The document's text content (LAYOUT mode → Markdown-structured).

        Raises:
            RuntimeError: If the backend is not ``cortex``.
        """
        if self._backend != "cortex":
            msg = "parse_document requires backend='cortex'"
            raise RuntimeError(msg)
        self._set_query_tag()
        # AI_PARSE_DOCUMENT takes a FILE (via TO_FILE), not a string path.
        from snowflake.snowpark.functions import ai_parse_document, to_file

        row = (
            self._session.range(1)
            .select(
                ai_parse_document(to_file(file_url), mode="LAYOUT")["content"].cast(
                    "string"
                )
            )
            .collect()[0]
        )
        return str(row[0])

    def count_tokens(self, text: str, model: str = "mistral-7b") -> int:
        """Count tokens in text using ``SNOWFLAKE.CORTEX.COUNT_TOKENS``."""
        if self._backend == "cortex":
            self._set_query_tag()
            from snowflake.snowpark.functions import call_builtin, lit

            # COUNT_TOKENS(model, text) — AI_COUNT_TOKENS returns NULL. The model must
            # support token counting (mistral-*, llama-*; claude-* is rejected).
            row = (
                self._session.range(1)
                .select(
                    call_builtin("SNOWFLAKE.CORTEX.COUNT_TOKENS", lit(model), lit(text))
                )
                .collect()[0]
            )
            return int(row[0])

        return len(text) // 4

    # ── Semantic cache ───────────────────────────────────────────────────────

    def _cache_lookup(self, prompt: str) -> str | None:
        """Query cache table for a semantically similar, non-expired prompt."""
        if not self._cache_table or self._backend != "cortex":
            return None
        from snowflake.snowpark.functions import (
            ai_embed,
            call_builtin,
            col,
            current_timestamp,
            lit,
        )

        table = self._session.table(self._cache_table)
        # TTL pre-filter: drop entries older than the window before scoring, so a stale
        # response is never returned (and never wins the similarity sort).
        if self._cache_ttl_seconds is not None:
            cutoff = call_builtin(
                "DATEADD",
                lit("second"),
                lit(-self._cache_ttl_seconds),
                current_timestamp(),
            )
            table = table.filter(col("CREATED_AT") >= cutoff)

        rows = (
            table.select(
                col("RESPONSE"),
                call_builtin(
                    "VECTOR_COSINE_SIMILARITY",
                    col("EMBEDDING"),
                    ai_embed("e5-base-v2", lit(prompt)),
                ).alias("SCORE"),
            )
            .filter(col("SCORE") >= self._similarity_threshold)
            .sort(col("SCORE").desc())
            .limit(1)
            .collect()
        )
        if rows:
            return str(rows[0]["RESPONSE"])
        return None

    def _cache_store(self, prompt: str, response: str, model: str) -> None:
        """Store prompt embedding and response in cache table."""
        if not self._cache_table or self._backend != "cortex":
            return
        from snowflake.snowpark.functions import (
            ai_embed,
            current_timestamp,
            lit,
            sha2,
        )

        (
            self._session.range(1)
            .select(
                ai_embed("e5-base-v2", lit(prompt)).alias("EMBEDDING"),
                sha2(lit(prompt), 256).alias("PROMPT_HASH"),
                lit(response).alias("RESPONSE"),
                lit(model).alias("MODEL"),
                current_timestamp().alias("CREATED_AT"),
            )
            .write.mode("append")
            .save_as_table(self._cache_table)
        )

    def purge_expired(self) -> int:
        """Delete cache entries older than the configured TTL (eviction).

        Companion to the TTL lookup filter: call periodically (e.g. from a scheduled task)
        to bound cache-table growth. No-op unless a semantic cache and a positive
        ``cache_ttl_seconds`` are configured on a Cortex backend.

        Returns:
            The number of rows deleted.
        """
        if (
            not self._cache_table
            or self._cache_ttl_seconds is None
            or self._backend != "cortex"
        ):
            return 0
        from snowflake.snowpark.functions import (
            call_builtin,
            col,
            current_timestamp,
            lit,
        )

        cutoff = call_builtin(
            "DATEADD",
            lit("second"),
            lit(-self._cache_ttl_seconds),
            current_timestamp(),
        )
        result = self._session.table(self._cache_table).delete(
            col("CREATED_AT") < cutoff
        )
        return int(result.rows_deleted)

    # ── Token compression ────────────────────────────────────────────────────

    def _compress_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """Summarise the oldest 2/3 of history when total exceeds max_tokens."""
        total_text = " ".join(m["content"] for m in history)
        token_estimate = self.count_tokens(total_text)

        if token_estimate <= self._max_tokens:
            return history

        split_idx = (len(history) * 2) // 3
        old_part = history[:split_idx]
        recent_part = history[split_idx:]

        old_text = "\n".join(f"{m['role']}: {m['content']}" for m in old_part)
        summary_prompt = f"{_COMPRESSION_PROMPT}\n\n{old_text}"

        summary = self._call_complete(
            [{"role": "user", "content": summary_prompt}],
            "mistral-7b",
        )

        return [
            {"role": "system", "content": f"[Conversation summary] {summary}"},
            *recent_part,
        ]

    # ── QUERY_TAG ────────────────────────────────────────────────────────────

    def _set_query_tag(self) -> None:
        """Set structured JSON QUERY_TAG on the session before a Cortex call."""
        if not self._query_tag or not self._session:
            return
        self._session.query_tag = json.dumps(self._query_tag)

    # ── Backend dispatch ─────────────────────────────────────────────────────

    def _build_messages(
        self,
        prompt: str,
        history: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        """Build message list, applying compression if needed."""
        if history:
            messages = self._compress_history(history)
            messages.append({"role": "user", "content": prompt})
        else:
            messages = [{"role": "user", "content": prompt}]
        return messages

    def _call_complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> str:
        """Dispatch to the active backend."""
        if self._backend == "cortex":
            return self._call_cortex(messages, model, **kwargs)
        return self._call_openai_compat(messages, model, **kwargs)

    def _call_cortex(
        self,
        messages: list[dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> str:
        """Call Cortex COMPLETE via snowflake.snowpark.functions.ai_complete."""
        self._set_query_tag()
        from snowflake.snowpark.functions import ai_complete

        messages_json = json.dumps(messages)
        row = (
            self._session.range(1)
            .select(
                ai_complete(
                    model=model,
                    prompt=messages_json,
                    model_parameters=kwargs or None,
                )
            )
            .collect()[0]
        )
        result = row[0]
        return str(result) if result else ""

    def _call_openai_compat(
        self,
        messages: list[dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> str:
        """Call OpenAI-compatible API (ollama, groq, task)."""
        client = self._http_client()

        if self._backend == "ollama":
            url = f"{self._base_url}/v1/chat/completions"
            headers = {}
            local_model = MODEL_MAP.get(model)
        elif self._backend == "groq":
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            local_model = model
        else:  # task — EAI direct
            url = f"{self._base_url}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            local_model = model

        payload: dict[str, Any] = {"model": local_model, "messages": messages, **kwargs}
        resp = client.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        return cast(str, resp.json()["choices"][0]["message"]["content"])

    def _http_client(self) -> httpx.Client:
        """Lazily create a shared httpx client."""
        if self._client is None:
            self._client = httpx.Client()
        return self._client


def get_ai(session: Any, **kwargs: Any) -> PinkyAI:
    """Resolve the active backend from environment variables and return a PinkyAI instance."""
    backend = os.getenv("PINKY_AI_BACKEND", "cortex")
    return PinkyAI(
        session,
        backend=backend,
        base_url=os.getenv("PINKY_AI_URL", "http://localhost:11434"),
        api_key=os.getenv("PINKY_AI_KEY", ""),
        **kwargs,
    )
