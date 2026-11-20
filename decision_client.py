"""Client wrapper for Gemini Decisions API (gemini-decision-1, v1beta)."""

from __future__ import annotations

from typing import Any, Optional

from google import genai


def _extract_attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_decision_answers(result: Any) -> dict[str, dict[str, Any]]:
    """Normalize SDK or dict response from gemini-decision-1 into plain dicts."""
    raw_answers = _extract_attr(result, "answers", None)
    if raw_answers is None and isinstance(result, dict):
        raw_answers = result

    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_answers, dict):
        return normalized

    for q_key, ans in raw_answers.items():
        ans_type = _extract_attr(ans, "type", "")
        entry: dict[str, Any] = {
            "type": ans_type,
            "confidence": float(_extract_attr(ans, "confidence", 0.0) or 0.0),
        }
        if ans_type == "score" or _extract_attr(ans, "score", None) is not None:
            entry["score"] = float(_extract_attr(ans, "score", 0.0) or 0.0)
        if ans_type == "noul" or _extract_attr(ans, "noul", None) is not None:
            noul_val = _extract_attr(ans, "noul", None)
            if noul_val is None:
                noul_val = _extract_attr(ans, "probability", 0.0)
            entry["noul"] = float(noul_val or 0.0)
        if ans_type == "choice" or _extract_attr(ans, "choice", None) is not None:
            choice_val = _extract_attr(ans, "choice", None)
            if choice_val is None:
                choice_val = _extract_attr(ans, "answer", "")
            entry["choice"] = str(choice_val or "")
            probs = _extract_attr(ans, "probabilities", None)
            if isinstance(probs, dict):
                entry["probabilities"] = {str(k): float(v) for k, v in probs.items()}
                if not entry["choice"] and probs:
                    entry["choice"] = max(probs, key=lambda k: probs[k])
        normalized[q_key] = entry

    return normalized


class GeminiDecisionClient:
    """Zero-output-token Decisions API client pinned to v1beta / gemini-decision-1."""

    def __init__(self, api_key: str, model: str = "gemini-decision-1"):
        self.api_key = api_key
        self.model = model
        self._client: Optional[genai.Client] = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options={"api_version": "v1beta"},
            )
        return self._client

    async def decide(
        self,
        *,
        input_text: str,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Execute a single batched gemini-decision-1 (v1beta) call."""
        client = self._get_client()
        result = await client.aio.decisions.create(
            model=self.model,
            input=input_text,
            questions=questions,
        )
        return normalize_decision_answers(result)
