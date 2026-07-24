# -*- encoding: utf-8 -*-
"""Generation backends.

``llamacpp``       -- in-process quantized model (the CPU profile default).
``openai_compat``  -- any OpenAI-compatible server (vLLM / Ollama / TGI), which
                      is how an accelerated deployment (DGX Spark or similar) is
                      plugged in: same contract, different base URL.
``none``           -- retrieval only; the pipeline returns the best chunk
                      verbatim. Useful for debugging retrieval and for hosts too
                      small to generate at all.

Switching profile is configuration, never code -- see doc/rag-assistant-design.md.
"""
import json
import time
import urllib.error
import urllib.request


class GenerationError(Exception):
    """Backend failed or timed out; the caller degrades gracefully."""


class NullGenerator:
    """Retrieval only. ``generate`` is never called; the pipeline short-circuits."""

    name = "none"
    extractive = True

    def warmup(self):
        return True

    def generate(self, messages, max_tokens=320, temperature=0.2, timeout_s=40):
        raise GenerationError("generation is disabled (RAG_LLM_BACKEND=none)")


class LlamaCppGenerator:
    """Local GGUF model through llama-cpp-python.

    Streamed on purpose: it is the only way to enforce a real wall-clock
    deadline on a CPU generation, and a request that has already spent its
    budget must be cut short rather than tie up the single slot.
    """

    extractive = False

    def __init__(self, model_path, n_ctx=4096, n_threads=0):
        if not model_path:
            raise RuntimeError("RAG_LLM_MODEL_PATH is required for the llamacpp backend")
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.name = model_path.rstrip("/").rsplit("/", 1)[-1]
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from llama_cpp import Llama  # lazy: heavy import, optional dependency

            kwargs = {"model_path": self.model_path, "n_ctx": self.n_ctx, "verbose": False}
            if self.n_threads:
                kwargs["n_threads"] = self.n_threads
            self._llm = Llama(**kwargs)
        return self._llm

    def warmup(self):
        return self.llm is not None

    def generate(self, messages, max_tokens=320, temperature=0.2, timeout_s=40):
        deadline = time.monotonic() + timeout_s
        parts, truncated, tokens = [], False, 0
        try:
            stream = self.llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            for piece in stream:
                delta = piece["choices"][0].get("delta", {})
                text = delta.get("content")
                if text:
                    parts.append(text)
                    tokens += 1
                if time.monotonic() > deadline:
                    truncated = True
                    break
        except Exception as exc:  # llama_cpp raises a variety of runtime errors
            raise GenerationError(f"llamacpp generation failed: {exc}") from exc
        return "".join(parts).strip(), {"completion_tokens": tokens, "truncated": truncated}


class OpenAICompatGenerator:
    """Remote OpenAI-compatible chat endpoint (vLLM, Ollama, TGI, ...)."""

    extractive = False

    def __init__(self, base_url, model_name="local", api_key=""):
        if not base_url:
            raise RuntimeError("RAG_LLM_BASE_URL is required for the openai_compat backend")
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.name = f"{model_name}@{self.base_url}"

    def warmup(self):
        return True

    def generate(self, messages, max_tokens=320, temperature=0.2, timeout_s=40):
        payload = json.dumps(
            {
                "model": self.model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=payload, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GenerationError(f"openai_compat generation failed: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GenerationError(f"unexpected response shape: {exc}") from exc
        usage = data.get("usage") or {}
        return (text or "").strip(), {
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "truncated": False,
        }


def build_generator(settings):
    backend = (settings.llm_backend or "").lower()
    if backend == "none":
        return NullGenerator()
    if backend == "llamacpp":
        return LlamaCppGenerator(
            settings.llm_model_path, n_ctx=settings.n_ctx, n_threads=settings.n_threads
        )
    if backend == "openai_compat":
        return OpenAICompatGenerator(
            settings.llm_base_url, settings.llm_model_name, settings.llm_api_key
        )
    raise RuntimeError(f"unknown RAG_LLM_BACKEND: {settings.llm_backend!r}")
