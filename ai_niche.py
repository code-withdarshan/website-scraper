"""
AI-powered niche classification via NVIDIA Build API.

NVIDIA's Build endpoints are OpenAI-compatible, so a single requests.post
call is enough — no need for the openai SDK as a dependency.

Use classify_niche() for one site or classify_niches_parallel() for a batch.
Both gracefully degrade to None / unchanged dict when:
  - API key is missing or malformed
  - The HTTP call fails
  - The model returns unparseable JSON

The caller decides whether to overwrite the heuristic niche or keep both.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Friendly label → model ID. Used to populate the sidebar dropdown.
MODELS = [
    ("Llama 3.3 70B (recommended)",  "meta/llama-3.3-70b-instruct"),
    ("Llama 3.1 70B",                "meta/llama-3.1-70b-instruct"),
    ("Llama 3.1 8B (fastest)",       "meta/llama-3.1-8b-instruct"),
    ("Nemotron 70B",                 "nvidia/llama-3.1-nemotron-70b-instruct"),
    ("Mistral Large 2",              "mistralai/mistral-large-2-instruct"),
]

SYSTEM_PROMPT = (
    "You classify small-business websites into niches. "
    "Read the supplied page context and return ONLY a JSON object with these keys: "
    '{"niche": "<short label, 1-4 words>", '
    '"confidence": <integer 1-5>, '
    '"reasoning": "<one short sentence>"}. '
    "Examples of niche labels: 'Dentist', 'Diamond Jeweler', 'SaaS / Fintech', "
    "'Wedding Photographer', 'Industrial Equipment'. "
    "Be concrete — don't return generic labels like 'Business' or 'E-commerce'."
)


def _build_user_prompt(record: dict) -> str:
    """Build the user-turn prompt from a scrape result + optional context blob."""
    company  = (record.get("company")  or "").strip()
    niche    = (record.get("niche")    or "").strip()
    tech     = (record.get("tech")     or "").strip()
    city     = (record.get("city")     or "").strip()
    url      = (record.get("url")      or "").strip()
    context  = (record.get("_ai_context") or "").strip()

    lines = [f"URL:     {url}"]
    if company: lines.append(f"Company: {company}")
    if city:    lines.append(f"City:    {city}")
    if tech:    lines.append(f"Tech:    {tech}")
    if niche:   lines.append(f"Heuristic-guessed niche: {niche}")
    lines.append("")
    if context:
        lines.append("Page context (title, meta description, body excerpt):")
        lines.append(context[:1500])
    else:
        lines.append("(no page context available — use URL + company name only)")
    lines.append("")
    lines.append("Classify the niche of this business. Return JSON only.")
    return "\n".join(lines)


def classify_niche(api_key: str, model: str, record: dict, timeout: int = 30) -> dict | None:
    """Return {"niche", "confidence", "reasoning"} or None on any failure."""
    if not api_key or not api_key.startswith("nvapi-"):
        return None
    if not model:
        return None
    try:
        resp = requests.post(
            f"{NVIDIA_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            json={
                "model":    model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": _build_user_prompt(record)},
                ],
                "max_tokens":  200,
                "temperature": 0.0,
                # NVIDIA's endpoint supports OpenAI-style JSON response format
                # for the Llama models. If a particular model rejects it the
                # call still succeeds with plain text — we strip code fences below.
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        if not resp.ok:
            log.debug("AI niche HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.debug("AI niche call failed: %s", exc)
        return None

    # Strip ```json ... ``` fences if the model wrapped its output
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(content)
    except Exception:
        # Try to extract the first {...} block as a fallback
        m = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None

    return {
        "niche":      str(data.get("niche", "")).strip()[:60],
        "confidence": int(data.get("confidence") or 0),
        "reasoning":  str(data.get("reasoning", "")).strip()[:200],
    }


def classify_niches_parallel(
    api_key: str,
    model: str,
    records: list[dict],
    max_workers: int = 5,
    progress_cb=None,
) -> list[dict | None]:
    """Classify N records in parallel. Order preserved. progress_cb(done, total) optional."""
    results: list[dict | None] = [None] * len(records)
    if not records or not api_key or not model:
        return results
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(classify_niche, api_key, model, rec): i
            for i, rec in enumerate(records)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                results[idx] = None
            done += 1
            if progress_cb:
                try:
                    progress_cb(done, len(records))
                except Exception:
                    pass
    return results
