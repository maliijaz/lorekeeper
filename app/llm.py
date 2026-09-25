"""LLM access for open-weight models.

- provider "ollama": local models on the GPU through Ollama (structured output via JSON schema).
- provider "openai": any OpenAI-compatible endpoint serving open models - Groq, Hugging Face router,
  vLLM, llama.cpp server, LM Studio.
"""
import json
import re
import time

import httpx

from .config import load_settings


class LLMError(RuntimeError):
    pass


def _parse_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


class LLM:
    def __init__(self, settings: dict | None = None):
        self.s = settings or load_settings()
        self.client = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0))

    @property
    def label(self) -> str:
        if self.s["provider"] == "ollama":
            return f"ollama:{self.s['ollama_model']}"
        return f"{self.s['openai_base_url']}:{self.s['openai_model']}"

    # -------------------------------------------------------------- public API
    def json(self, system: str, user: str, schema: dict, max_tokens: int = 4096, retries: int = 2,
             think: bool = False) -> dict:
        last = None
        for attempt in range(retries + 1):
            try:
                text = self._chat(system, user, schema=schema, max_tokens=max_tokens,
                                  temperature=0.1 if attempt == 0 else 0.3, think=think)
                return _parse_json(text)
            except (json.JSONDecodeError, ValueError) as e:
                last = e
        raise LLMError(f"Model did not return valid JSON: {last}")

    def text(self, system: str, user: str, max_tokens: int = 1200, temperature: float = 0.3) -> str:
        return _strip_think(self._chat(system, user, max_tokens=max_tokens, temperature=temperature))

    def health(self) -> dict:
        try:
            if self.s["provider"] == "ollama":
                r = self.client.get(f"{self.s['ollama_url']}/api/tags", timeout=5)
                r.raise_for_status()
                names = [m["name"] for m in r.json().get("models", [])]
                ok = self.s["ollama_model"] in names or f"{self.s['ollama_model']}:latest" in names
                return {"ok": ok, "models": names,
                        "message": "" if ok else f"Run: ollama pull {self.s['ollama_model']}"}
            r = self.client.get(f"{self.s['openai_base_url'].rstrip('/')}/models",
                                headers=self._headers(), timeout=10)
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
            return {"ok": True, "models": names, "message": ""}
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            return {"ok": False, "models": [], "message": str(e)}

    # -------------------------------------------------------------- transport
    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.s.get("openai_api_key"):
            h["Authorization"] = f"Bearer {self.s['openai_api_key']}"
        return h

    def _chat(self, system, user, schema=None, max_tokens=4096, temperature=0.1, think=False) -> str:
        if self.s["provider"] == "ollama":
            return self._ollama(system, user, schema, max_tokens, temperature, think)
        return self._openai(system, user, schema, max_tokens, temperature)

    def _ollama(self, system, user, schema, max_tokens, temperature, think=False):
        body = {
            "model": self.s["ollama_model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": think,  # reasoning models (qwen3 etc.): only reason where accuracy matters more than speed
            "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": self.s["ollama_num_ctx"],
                        "num_predict": max_tokens, "num_gpu": 999},
        }
        if schema:
            body["format"] = schema
        try:
            r = self.client.post(f"{self.s['ollama_url']}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Ollama at {self.s['ollama_url']}: {e}") from e
        if r.status_code == 400 and "think" in r.text:
            body.pop("think")  # model without thinking support
            r = self.client.post(f"{self.s['ollama_url']}/api/chat", json=body)
        if r.status_code != 200:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"]

    def _openai(self, system, user, schema, max_tokens, temperature):
        if schema:
            system = f"{system}\n\nRespond with a single JSON object matching this JSON schema:\n{json.dumps(schema)}"
        body = {
            "model": self.s["openai_model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.s['openai_base_url'].rstrip('/')}/chat/completions"
        for attempt in range(8):
            try:
                r = self.client.post(url, json=body, headers=self._headers())
            except httpx.HTTPError as e:
                if attempt == 7:
                    raise LLMError(f"Cannot reach {url}: {e}") from e
                time.sleep(2 ** min(attempt, 5))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("retry-after", 0) or 0) or 2 ** min(attempt + 1, 6)
                time.sleep(min(wait, 90))
                continue
            if r.status_code == 400 and "json" in r.text.lower() and "response_format" in body:
                body.pop("response_format")  # server doesn't support JSON mode; rely on the prompt
                continue
            if r.status_code != 200:
                raise LLMError(f"LLM API error {r.status_code}: {r.text[:300]}")
            msg = r.json()["choices"][0]["message"]
            return msg.get("content") or ""
        raise LLMError("LLM API kept rate-limiting; try again later.")
