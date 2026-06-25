#!/usr/bin/env python3
"""
llm.py  —  provider-agnostic chat helpers shared by the pipeline stages.

Picks the backend from LLM_PROVIDER in .env ("groq" or "ollama") and exposes the
two calls the stages need, so 02/03/04 never see provider-specific code:

    chat_vision(prompt, image_path, ...) -> str   (one image + prompt)
    chat_text(messages, ...)             -> str   (chat messages)

Env (read from .env):
    LLM_PROVIDER          groq | ollama                 (default: groq)

    # --- Groq ---
    GROQ_API_KEY          (required when LLM_PROVIDER=groq)
    GROQ_VISION_MODEL     (default: meta-llama/llama-4-scout-17b-16e-instruct)
    GROQ_TEXT_MODEL       (default: llama-3.3-70b-versatile)

    # --- Ollama ---
    OLLAMA_VISION_MODEL   (default: llama3.2-vision)
    OLLAMA_TEXT_MODEL     (default: llama3.1)
    OLLAMA_HOST           (optional; e.g. http://localhost:11434 or https://ollama.com)
    OLLAMA_API_KEY        (optional; bearer token for a hosted OLLAMA_HOST)
"""
import base64, os, sys
from dotenv import load_dotenv

load_dotenv()   # Reads .env

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()


def vision_model():
    if PROVIDER == "ollama":
        return os.environ.get("OLLAMA_VISION_MODEL", "llama3.2-vision")
    return os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def text_model():
    if PROVIDER == "ollama":
        return os.environ.get("OLLAMA_TEXT_MODEL", "llama3.1")
    return os.environ.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")


# --- lazy, cached clients -------------------------------------------------
_groq_client = None
_ollama_client = None


def _groq():
    global _groq_client
    if _groq_client is None:
        try:
            from groq import Groq
        except ImportError:
            sys.exit("ERROR: the 'groq' package is not installed. Run:  pip install groq")
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            sys.exit("ERROR: GROQ_API_KEY is not set. Add it to your .env (or environment).")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def _ollama():
    global _ollama_client
    if _ollama_client is None:
        try:
            import ollama
        except ImportError:
            sys.exit("ERROR: the 'ollama' package is not installed. Run:  pip install ollama")
        host = os.environ.get("OLLAMA_HOST")
        api_key = os.environ.get("OLLAMA_API_KEY")
        if host and api_key:
            _ollama_client = ollama.Client(host=host, headers={"Authorization": "Bearer " + api_key})
        elif host:
            _ollama_client = ollama.Client(host=host)
        else:
            _ollama_client = ollama.Client()
    return _ollama_client


def _data_uri(image_path):
    with open(image_path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


# --- the two public calls -------------------------------------------------
def chat_vision(prompt, image_path, temperature=0, max_tokens=None):
    """Send one prompt + one image to the configured vision model; return text."""
    if PROVIDER == "ollama":
        client = _ollama()
        opts = {"temperature": temperature}
        if max_tokens:
            opts["num_predict"] = max_tokens
        resp = client.chat(
            model=vision_model(),
            messages=[{"role": "user", "content": prompt, "images": [image_path]}],
            options=opts,
        )
        return resp["message"]["content"]

    client = _groq()
    kw = {"max_tokens": max_tokens} if max_tokens else {}
    resp = client.chat.completions.create(
        model=vision_model(),
        temperature=temperature,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
            ],
        }],
        **kw,
    )
    return resp.choices[0].message.content


def chat_text(messages, temperature=0, max_tokens=None, json_object=False):
    """Send chat `messages` to the configured text model; return text.
    json_object=True asks the backend to emit a single JSON object."""
    if PROVIDER == "ollama":
        client = _ollama()
        opts = {"temperature": temperature}
        if max_tokens:
            opts["num_predict"] = max_tokens
        kw = {"format": "json"} if json_object else {}
        resp = client.chat(model=text_model(), messages=messages, options=opts, **kw)
        return resp["message"]["content"]

    client = _groq()
    kw = {}
    if max_tokens:
        kw["max_tokens"] = max_tokens
    if json_object:
        kw["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(
        model=text_model(), messages=messages, temperature=temperature, **kw)
    return resp.choices[0].message.content
