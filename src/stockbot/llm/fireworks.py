"""Fireworks AI client (OpenAI-compatible chat completions).

This client is deliberately dumb: it sends messages and returns text. It has no
knowledge of signals, prices, or confidence. The two callers that use it are
constrained at their own boundary:

  * news.sentiment  -> return value is parsed into a 3-value enum, or dropped.
  * explain.explainer -> return value is prose stored in a field nothing reads.

If you find yourself wanting to parse a number out of a response here, that is
the design principle being violated. Compute it in code instead.
"""

from __future__ import annotations

import time

import requests

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("llm.fireworks")


class LLMError(RuntimeError):
    """The model was reachable but the exchange failed."""


class LLMUnavailable(LLMError):
    """No API key, or the endpoint could not be reached at all.

    Callers must degrade gracefully: the pipeline still produces signals
    without any LLM, because no signal depends on one.
    """


class FireworksClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_url = str(cfg.get("llm.base_url", "https://api.fireworks.ai/inference/v1")).rstrip("/")
        self.api_key = cfg.secrets.fireworks_api_key
        self.timeout = int(cfg.get("llm.timeout_seconds", 60))
        self.max_retries = int(cfg.get("llm.max_retries", 3))
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        if not self.available:
            raise LLMUnavailable("FIREWORKS_API_KEY is not set")

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("fireworks request failed (attempt %d/%d): %s", attempt, self.max_retries, exc)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()["choices"][0]["message"]["content"].strip()
                    except (KeyError, IndexError, ValueError) as exc:
                        raise LLMError(f"unexpected response shape: {resp.text[:300]}") from exc

                if resp.status_code in (401, 403):
                    raise LLMUnavailable(f"auth rejected by Fireworks ({resp.status_code})")

                # 429 / 5xx are worth another try.
                last_error = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                log.warning("fireworks HTTP %d (attempt %d/%d)", resp.status_code, attempt, self.max_retries)

            if attempt < self.max_retries:
                time.sleep(2 ** (attempt - 1))

        raise LLMError(f"fireworks call failed after {self.max_retries} attempts: {last_error}")
