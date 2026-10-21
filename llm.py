"""Talks to Google Gemini using standard API calls."""
import json
import os
import re
import time
from dotenv import load_dotenv
import httpx
from openai import OpenAI

load_dotenv()
_client = None

# Create and reuse the configured Gemini API client.
def _get_client():
    global _client
    if _client is None:
        # A single SDK retry can use the timeout twice; keep the default total
        # wait comfortably below the chat page's 60-second response window.
        timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "15"))
        if timeout <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be greater than zero")
        # Bypass the dead local proxy found in the shell environment so Gemini
        # is reached directly.
        http_client = httpx.Client(trust_env=False, timeout=timeout)
        _client = OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.environ["GEMINI_API_KEY"],
            timeout=timeout,
            max_retries=1,
            http_client=http_client,
        )
    return _client

# Request a JSON response from Gemini and record usage when available.
def ask_json(system, history, cur=None):
    # Gemini 3.5 Flash-Lite was returning 503s; 3.6 Flash passed the live chat check.
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1500"))
    if max_tokens <= 0:
        raise ValueError("LLM_MAX_TOKENS must be greater than zero")
    reasoning_effort = os.getenv("GEMINI_REASONING_EFFORT")
    if reasoning_effort is None and "flash-lite" not in model:
        reasoning_effort = "low"
    total_tokens_in = total_tokens_out = 0
    usage_seen = False
    parsed = None

    for attempt in range(2):
        request_history = history
        if attempt:
            request_history = history + [{
                "role": "user",
                "content": "Return ONLY a valid JSON object containing 'reply' and 'order' keys. No extra markdown text.",
            }]
        started = time.perf_counter()
        try:
            request_options = dict(
                model=model,
                messages=[{"role": "system", "content": system}] + request_history,
                max_tokens=max_tokens,
            )
            # Flash-Lite defaults to minimal thinking; omit the option unless overridden.
            if reasoning_effort:
                request_options["reasoning_effort"] = reasoning_effort
            resp = _get_client().chat.completions.create(**request_options)
        except Exception:
            if cur is not None:
                cur.execute(
                    "insert into agent_runs (agent,model,tokens_in,tokens_out,latency_ms) values (%s,%s,%s,%s,%s)",
                    ("sales", model, None, None, int((time.perf_counter() - started) * 1000))
                )
            raise

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
        tokens_out = getattr(usage, "completion_tokens", None) if usage else None
        if tokens_in is not None:
            total_tokens_in += tokens_in
            usage_seen = True
        if tokens_out is not None:
            total_tokens_out += tokens_out
            usage_seen = True

        if cur is not None:
            cur.execute(
                "insert into agent_runs (agent,model,tokens_in,tokens_out,latency_ms) values (%s,%s,%s,%s,%s)",
                ("sales", model, tokens_in, tokens_out, int((time.perf_counter() - started) * 1000))
            )

        text = resp.choices[0].message.content or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        
        # Clean markdown code blocks if Gemini wraps json in them
        text = re.sub(r"^```json\s*", "", text, flags=re.M)
        text = re.sub(r"^```\s*$", "", text, flags=re.M)
        
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                candidate = json.loads(text[start:end + 1])
                if isinstance(candidate, dict) and "reply" in candidate and "order" in candidate:
                    parsed = candidate
                    break
            except json.JSONDecodeError:
                pass

    return {
        "result": parsed,
        "tokens_in": total_tokens_in if usage_seen else None,
        "tokens_out": total_tokens_out if usage_seen else None,
    }
