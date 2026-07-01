#!/usr/bin/env python3
"""
llm.py  —  provider-agnostic chat helpers shared by the pipeline stages.

Picks the backend from LLM_PROVIDER in .env and exposes the two calls the stages
need, so 02/03/04 never see provider-specific code:

    chat_vision(prompt, image_path, ...) -> str   (one image + prompt)
    chat_text(messages, ...)             -> str   (chat messages)

LLM_PROVIDER may be:  groq | openai | gemini | ollama
  - groq, openai and gemini all speak the OpenAI chat-completions API
    (gemini via its OpenAI-compatibility endpoint), so they share one code path
    and only need the matching SDK + API key.
  - ollama runs locally (or against a hosted OLLAMA_HOST).

Env (read from .env):
    LLM_PROVIDER          groq | openai | gemini | ollama        (default: groq)

    # --- Groq ---            (pip install groq)
    GROQ_API_KEY
    GROQ_VISION_MODEL     (default: meta-llama/llama-4-scout-17b-16e-instruct)
    GROQ_TEXT_MODEL       (default: llama-3.3-70b-versatile)

    # --- OpenAI ---          (pip install openai)
    OPENAI_API_KEY
    OPENAI_VISION_MODEL   (default: gpt-4o-mini)
    OPENAI_TEXT_MODEL     (default: gpt-4o-mini)

    # --- Gemini ---          (pip install openai; uses Google's OpenAI endpoint)
    GEMINI_API_KEY
    GEMINI_VISION_MODEL   (default: gemini-2.0-flash)
    GEMINI_TEXT_MODEL     (default: gemini-2.0-flash)

    # --- Ollama ---          (pip install ollama)
    OLLAMA_VISION_MODEL   (default: llama3.2-vision)
    OLLAMA_TEXT_MODEL     (default: llama3.1)
    OLLAMA_HOST           (optional; e.g. http://localhost:11434 or https://ollama.com)
    OLLAMA_API_KEY        (optional; bearer token for a hosted OLLAMA_HOST)
"""
import base64, io, os, random, re, sys, threading, time
from dotenv import load_dotenv

load_dotenv()   # Reads .env

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Hosted vision endpoints cap request size (Groq rejects >~4MB images with HTTP
# 413). Downscale the long edge and JPEG-encode so the request fits; the on-disk
# high-res render is untouched (Stage E still crops it for figures).
VISION_MAX_PX = int(os.environ.get("VISION_MAX_PX", "2048"))
VISION_MAX_BYTES = int(os.environ.get("VISION_MAX_BYTES", "2800000"))  # ~3.7MB once base64'd


def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


# ---- model tiers -----------------------------------------------------------
# Route each prompt by importance. Each tier picks a PROVIDER; the actual model
# is that provider's existing {PROVIDER}_VISION_MODEL / {PROVIDER}_TEXT_MODEL.
#   strong : highest-stakes, selective  (visual refine compare)        -> gemini/openai
#   mid    : bulk core work             (transcription, correction)    -> groq
#   cheap  : quick low-stakes calls     (rotation detect, face-id)     -> ollama
# Legacy vars still work: LLM_PROVIDER=mid, REFINE_PROVIDER=strong, ROTATE_PROVIDER=cheap.
TIER_PROVIDER = {
    "strong": _env("STRONG_PROVIDER", "REFINE_PROVIDER", default="gemini").lower(),
    "mid":    _env("MID_PROVIDER", "LLM_PROVIDER", default="groq").lower(),
    "cheap":  _env("CHEAP_PROVIDER", "ROTATE_PROVIDER", default="ollama").lower(),
}

PROVIDER = TIER_PROVIDER["mid"]   # back-compat alias: the "main" (bulk) provider

_VISION_DEFAULT = {"ollama": "llama3.2-vision", "openai": "gpt-4o-mini",
                   "gemini": "gemini-2.0-flash",
                   "groq": "meta-llama/llama-4-scout-17b-16e-instruct"}
_TEXT_DEFAULT = {"ollama": "llama3.1", "openai": "gpt-4o-mini",
                 "gemini": "gemini-2.0-flash", "groq": "llama-3.3-70b-versatile"}


def tier_provider(tier):
    return TIER_PROVIDER.get(tier, PROVIDER)


def vision_model(provider=None):
    provider = provider or PROVIDER
    return os.environ.get(provider.upper() + "_VISION_MODEL",
                          _VISION_DEFAULT.get(provider, _VISION_DEFAULT["groq"]))


def text_model(provider=None):
    provider = provider or PROVIDER
    return os.environ.get(provider.upper() + "_TEXT_MODEL",
                          _TEXT_DEFAULT.get(provider, _TEXT_DEFAULT["groq"]))


def _require(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"ERROR: {name} is not set. Add it to your .env (or environment).")
    return v


# --- lazy, cached clients -------------------------------------------------
_ollama_client = None


_clients = {}   # provider -> OpenAI-style client (cached)


def _client(provider=None):
    """Return an OpenAI-style client for an OpenAI-compatible provider.
    Groq uses the 'groq' SDK; openai/gemini use the 'openai' SDK (gemini points
    it at Google's OpenAI-compatibility endpoint). All expose the same
    chat.completions.create(...), so the request code is shared."""
    provider = provider or PROVIDER
    if provider in _clients:
        return _clients[provider]
    if provider == "groq":
        try:
            from groq import Groq
        except ImportError:
            sys.exit("ERROR: the 'groq' package is not installed. Run:  pip install groq")
        client = Groq(api_key=_require("GROQ_API_KEY"))
    elif provider == "openai":
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("ERROR: the 'openai' package is not installed. Run:  pip install openai")
        client = OpenAI(api_key=_require("OPENAI_API_KEY"))
    elif provider == "gemini":
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("ERROR: the 'openai' package is not installed. Run:  pip install openai\n"
                     "(Gemini is used through its OpenAI-compatible endpoint.)")
        client = OpenAI(api_key=_require("GEMINI_API_KEY"), base_url=GEMINI_OPENAI_BASE)
    else:
        sys.exit(f"ERROR: unknown provider '{provider}'. Use groq | openai | gemini | ollama.")
    _clients[provider] = client
    return client


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
    """Return a base64 JPEG data URI, downscaled to stay within the provider's
    request-size limit. Falls back to sending the raw file if PIL is missing."""
    try:
        from PIL import Image
    except ImportError:
        with open(image_path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    im = Image.open(image_path)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    longest = max(w, h)
    if longest > VISION_MAX_PX:
        s = VISION_MAX_PX / longest
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
    quality = 85
    while True:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= VISION_MAX_BYTES or quality <= 40:
            break
        quality -= 15
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


# --- rate limiting + retry for cloud APIs ---------------------------------
# Two layers: (1) proactive per-provider RPM spacing so we don't burst into the
# provider's request cap; (2) retry on 429 / transient 5xx that honors the
# provider's Retry-After (or "try again in Xms") and otherwise backs off
# exponentially with jitter. Ollama (local) is exempt.
API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "2"))
API_RETRY_CAP = float(os.environ.get("API_RETRY_CAP", "30"))   # max single sleep (s)
_DEFAULT_RPM = {"groq": 28, "gemini": 14, "openai": 60}        # 0 / unset = unlimited

_limiters = {}
_limiter_lock = threading.Lock()


class _Limiter:
    """Minimum-interval throttle (requests/min -> seconds between requests)."""
    def __init__(self, rpm):
        self.interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
                now = time.monotonic()
            self.next_t = now + self.interval


def _limiter(provider):
    with _limiter_lock:
        lim = _limiters.get(provider)
        if lim is None:
            rpm = int(_env(provider.upper() + "_RPM", "API_RPM",
                           default=str(_DEFAULT_RPM.get(provider, 0))))
            lim = _limiters[provider] = _Limiter(rpm)
        return lim


def _status(e):
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)


def _is_retryable(e):
    s = _status(e)
    if s in (429, 500, 502, 503, 504):
        return True
    m = str(e).lower()
    return any(k in m for k in ("rate limit", "rate_limit", "429", "timeout",
                                "overloaded", "temporarily", "503", "502"))


def _retry_after_raw(e):
    """Provider-suggested wait in seconds (UNCAPPED), from a Retry-After header
    or a 'try again in 6m22.752s' / 'in 389ms' message; None if no hint."""
    resp = getattr(e, "response", None)
    try:
        hdr = resp.headers.get("retry-after") if resp is not None and getattr(resp, "headers", None) else None
        if hdr:
            return float(hdr)
    except (ValueError, AttributeError):
        pass
    m = re.search(r"try again in\s*(?:(\d+)m)?([\d.]+)?\s*(ms|s)?", str(e))
    if m and (m.group(1) or m.group(2)):
        mins = float(m.group(1) or 0)
        secs = float(m.group(2) or 0)
        if m.group(3) == "ms":
            secs /= 1000.0
        return mins * 60 + secs + 0.1
    return None


def _hint_seconds(e):
    """Suggested wait, capped at API_RETRY_CAP for in-call sleeping."""
    r = _retry_after_raw(e)
    return None if r is None else min(r, API_RETRY_CAP)


def _err_param(e):
    """The request parameter an API 400 complained about, if any."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        p = (body.get("error") or {}).get("param")
        if p:
            return p
    m = re.search(r"'([A-Za-z_]+)' is not supported", str(e)) or \
        re.search(r"[Uu]nsupported parameter: '([A-Za-z_]+)'", str(e))
    return m.group(1) if m else None


def _adapt_params(kwargs, e):
    """Adapt request kwargs to model-specific quirks named in a 400 error
    (e.g. OpenAI gpt-5/o-series: max_tokens -> max_completion_tokens, or
    temperature/response_format unsupported). Returns True if it changed
    something (so the call can be retried). Each fix is one-shot."""
    msg = str(e).lower()
    if _status(e) != 400 and not any(k in msg for k in
                                     ("unsupported", "is not supported", "invalid_request")):
        return False
    param = _err_param(e)
    if "max_tokens" in kwargs and (param == "max_tokens" or "max_completion_tokens" in msg):
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        return True
    if param and param in kwargs:           # drop any other unsupported param we sent
        kwargs.pop(param, None)
        return True
    return False


def _oai_create(provider, **kwargs):
    """client.chat.completions.create with RPM spacing, 429/transient retry, and
    one-shot adaptation to model-specific parameter quirks (gpt-5 etc.)."""
    lim = _limiter(provider)
    last = None
    attempt = 0
    while True:
        lim.wait()
        try:
            return _client(provider).chat.completions.create(**kwargs)
        except Exception as e:
            if _adapt_params(kwargs, e):     # fixed a bad param -> retry, no penalty
                continue
            last = e
            if attempt >= API_MAX_RETRIES or not _is_retryable(e):
                raise
            wait = _hint_seconds(e)
            if wait is None:
                wait = min(2.0 * (2 ** attempt), API_RETRY_CAP)
            wait = min(wait + random.uniform(0, 0.5), API_RETRY_CAP)
            print(f"    {provider} rate/transient ({_status(e) or 'err'}); "
                  f"retry in {wait:.1f}s [{attempt+1}/{API_MAX_RETRIES}]", flush=True)
            time.sleep(wait)
            attempt += 1
    raise last


# --- provider fail-over ----------------------------------------------------
# Each tier can list fallback providers ({TIER}_FALLBACK, comma-separated). When
# the primary rate-limits (e.g. Groq tokens-per-day), the SAME call is retried on
# the next provider using ITS model. A provider that returns a long cooldown
# (TPD/daily) is parked for that long so we don't keep hammering it each page.
_TIER_FALLBACK_ENV = {"strong": "STRONG_FALLBACK", "mid": "MID_FALLBACK", "cheap": "CHEAP_FALLBACK"}
_blocked = {}            # provider -> monotonic deadline until which to skip it
_blocked_lock = threading.Lock()


def tier_chain(tier):
    """Provider try-order for a tier: primary first, then {TIER}_FALLBACK."""
    chain = [tier_provider(tier)]
    for p in (_env(_TIER_FALLBACK_ENV.get(tier, ""), default="") or "").split(","):
        p = p.strip().lower()
        if p and p not in chain:
            chain.append(p)
    return chain


def _is_rate_limited(e):
    s = _status(e)
    m = str(e).lower()
    return s == 429 or any(k in m for k in ("rate limit", "rate_limit", "quota")) or \
        ("429" in m and "rate" in m)


def _is_blocked(p):
    with _blocked_lock:
        return p in _blocked and time.monotonic() < _blocked[p]


def _maybe_block(p, e):
    """If a 429 carries a wait longer than we'd ever sleep in-call, park the
    provider for that duration so later calls skip straight to the fallback."""
    raw = _retry_after_raw(e)
    if raw and raw > API_RETRY_CAP:
        with _blocked_lock:
            _blocked[p] = time.monotonic() + raw
        print(f"    {p} rate-limited for ~{raw/60:.1f} min; parking it and failing over", flush=True)


def _run_with_fallback(tier, fn):
    """Try fn(provider) across the tier's chain, failing over on rate limits.
    Skips providers currently parked from an earlier long cooldown."""
    chain = tier_chain(tier)
    order = [p for p in chain if not _is_blocked(p)] or chain   # if all parked, still try
    last = None
    for i, p in enumerate(order):
        try:
            return fn(p)
        except Exception as e:
            last = e
            if _is_rate_limited(e):
                _maybe_block(p, e)
                if i + 1 < len(order):
                    print(f"    {p} rate-limited; failing over to {order[i+1]}", flush=True)
                    continue
            raise
    raise last


# --- the two public calls -------------------------------------------------
def chat_vision(prompt, image_path, tier="mid", temperature=0, max_tokens=None):
    """Send one prompt + one image to the tier's vision model; return text.
    Fails over to the tier's fallback provider(s) on rate limits."""
    def call(provider):
        model = vision_model(provider)
        if provider == "ollama":
            opts = {"temperature": temperature}
            if max_tokens:
                opts["num_predict"] = max_tokens
            resp = _ollama().chat(
                model=model,
                messages=[{"role": "user", "content": prompt, "images": [image_path]}],
                options=opts)
            return resp["message"]["content"]
        kw = {"max_tokens": max_tokens} if max_tokens else {}
        resp = _oai_create(
            provider, model=model, temperature=temperature,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
            ]}], **kw)
        return resp.choices[0].message.content
    return _run_with_fallback(tier, call)


def chat_text(messages, tier="mid", temperature=0, max_tokens=None, json_object=False):
    """Send chat `messages` to the tier's text model; return text. json_object
    asks for a single JSON object. Fails over to fallback provider(s) on limits."""
    def call(provider):
        model = text_model(provider)
        if provider == "ollama":
            opts = {"temperature": temperature}
            if max_tokens:
                opts["num_predict"] = max_tokens
            kw = {"format": "json"} if json_object else {}
            resp = _ollama().chat(model=model, messages=messages, options=opts, **kw)
            return resp["message"]["content"]
        kw = {}
        if max_tokens:
            kw["max_tokens"] = max_tokens
        if json_object:
            kw["response_format"] = {"type": "json_object"}
        resp = _oai_create(
            provider, model=model, messages=messages, temperature=temperature, **kw)
        return resp.choices[0].message.content
    return _run_with_fallback(tier, call)


# --- orientation pre-pass (CHEAP tier: a quick, low-stakes classification) --
_ROT_PROMPT = ("This scanned page may be rotated. How many degrees CLOCKWISE must it be "
               "rotated so the text reads upright? Reply with ONLY one number: 0, 90, 180 or 270.")

# PIL transpose to rotate an image D degrees CLOCKWISE to upright
_CW_TRANSPOSE = {90: "ROTATE_270", 180: "ROTATE_180", 270: "ROTATE_90"}


def _vision_once(prompt, image_path, provider, model, max_tokens=40):
    """One-off vision call against an explicit provider+model (not the global
    LLM_PROVIDER). Used by the orientation pre-pass."""
    if provider == "ollama":
        client = _ollama()
        kw = dict(model=model,
                  messages=[{"role": "user", "content": prompt, "images": [image_path]}],
                  options={"temperature": 0, "num_predict": max_tokens})
        try:                      # thinking models must be told not to think, or they
            return client.chat(think=False, **kw)["message"]["content"]   # burn the budget
        except TypeError:         # older ollama clients have no 'think' kwarg
            return client.chat(**kw)["message"]["content"]
    resp = _oai_create(
        provider, model=model, temperature=0, max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
        ]}])
    return resp.choices[0].message.content


def rotation_degrees(image_path):
    """Best-effort page-orientation detector (CHEAP tier) -> 0/90/180/270
    (clockwise to upright). Returns 0 on any error so ingestion never fails."""
    import re
    provider = tier_provider("cheap")
    model = _env("ROTATE_MODEL") or vision_model(provider)
    try:
        txt = _vision_once(_ROT_PROMPT, image_path, provider, model)
        m = re.search(r"\b(0|90|180|270)\b", txt or "")
        return int(m.group(1)) if m else 0
    except Exception as e:
        print(f"    (rotation detect unavailable: {e}; assuming 0)")
        return 0


def autorotate(image_path):
    """Detect orientation and rotate the file in place to upright. Returns the
    applied clockwise degrees (0 if none/unavailable)."""
    d = rotation_degrees(image_path)
    if d in _CW_TRANSPOSE:
        try:
            from PIL import Image
            im = Image.open(image_path)
            im.transpose(getattr(Image.Transpose, _CW_TRANSPOSE[d])).save(image_path)
            return d
        except Exception as e:
            print(f"    (rotate failed: {e})")
    return 0


# --- refine loop (STRONG tier): a premium model compares src vs reconstruction
# Visual self-correction is selective (only flagged pages, 1-2 passes), so a
# premium model is affordable here even when bulk OCR uses a cheap one.
REFINE_PROVIDER = TIER_PROVIDER["strong"]   # alias for prints / back-compat


def refine_model(provider=None):
    provider = provider or REFINE_PROVIDER
    return _env("REFINE_MODEL") or vision_model(provider)


def vision_multi(prompt, image_paths, provider=None, model=None,
                 max_tokens=2000, json_object=False, temperature=0):
    """Send a prompt + SEVERAL images to a vision model (default the STRONG
    tier). Used to compare a source page against its reconstruction. Fails over
    across the strong tier's fallback chain unless `provider` is pinned."""
    image_paths = list(image_paths)

    def call(prov):
        mdl = model or (_env("REFINE_MODEL") if prov == tier_provider("strong") else None) or vision_model(prov)
        if prov == "ollama":
            kw = dict(model=mdl,
                      messages=[{"role": "user", "content": prompt, "images": image_paths}],
                      options={"temperature": temperature, "num_predict": max_tokens})
            if json_object:
                kw["format"] = "json"
            try:
                return _ollama().chat(think=False, **kw)["message"]["content"]
            except TypeError:
                return _ollama().chat(**kw)["message"]["content"]
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": _data_uri(p)}} for p in image_paths]
        kw = {"max_tokens": max_tokens}
        if json_object:
            kw["response_format"] = {"type": "json_object"}
        resp = _oai_create(prov, model=mdl, temperature=temperature,
                           messages=[{"role": "user", "content": content}], **kw)
        return resp.choices[0].message.content

    if provider:                       # pinned provider: no fail-over
        return call(provider)
    return _run_with_fallback("strong", call)
