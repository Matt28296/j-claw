from __future__ import annotations
import json
import time
from pathlib import Path
import anthropic
from rich.console import Console
from rich.syntax import Syntax

from config import (
    ORCHESTRATOR_MODEL, ANTHROPIC_API_KEY, ORCHESTRATOR_PROMPT_PATH,
    ORCHESTRATOR_API_MODEL, ORCHESTRATOR_FALLBACK_MODELS, OPENROUTER_API_KEY,
    ORCHESTRATOR_MAX_TOKENS, EXECUTION_ERROR_MODEL, ORCHESTRATOR_TIMEOUT,
)
from validator import validate_response, OrchestratorOutputError
from cache_telemetry import log_cache_usage

console = Console()


_RESPONSE_FILE = Path("orchestrator_response.json")
_INPUT_FILE = Path("orchestrator_input.json")


class ManualOrchestrator:
    """
    You are the orchestrator.
    For each state the harness writes the input to orchestrator_input.json,
    opens orchestrator_response.json in Notepad for you to fill in,
    then reads and validates your response when you press Enter.
    """

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        state = payload.get("system_state", "INIT")

        _INPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _RESPONSE_FILE.write_text("{}\n", encoding="utf-8")

        console.rule(f"[bold magenta]ORCHESTRATOR INPUT — {state}[/bold magenta]")
        console.print(Syntax(json.dumps(payload, indent=2), "json", theme="monokai"))
        console.rule()
        console.print(
            f"\n[bold magenta]Your turn — state: {state}[/bold magenta]\n"
            f"  Input written to: {_INPUT_FILE.resolve()}\n"
            f"  Write your JSON response to: {_RESPONSE_FILE.resolve()}\n"
            "  Then press Enter here to continue.\n"
        )

        while True:
            input("  >> Press Enter once orchestrator_response.json is ready...")
            try:
                raw = _RESPONSE_FILE.read_text(encoding="utf-8-sig").strip()
                raw = _sanitize(raw)
                raw = _strip_fences(raw)
                parsed = json.loads(raw)
                validate_response(state, parsed)
                return parsed
            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                console.print(f"\n  [red]Invalid response: {exc}[/red]")
                console.print(f"  Fix {_RESPONSE_FILE} and press Enter again:\n")


class Orchestrator:
    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        """
        Send payload to the orchestrator, validate the response, and return parsed JSON.
        Retries up to max_retries times on invalid output before raising.
        """
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                _model = EXECUTION_ERROR_MODEL if state == "EXECUTION_ERROR" else ORCHESTRATOR_MODEL
                response = self._client.messages.create(
                    model=_model,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                    system=[{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_message}],
                    timeout=ORCHESTRATOR_TIMEOUT,
                )
                log_cache_usage(response.usage, f"orch:{state}")
                if response.stop_reason == "max_tokens":
                    console.print(
                        f"[yellow]⚠ Orchestrator hit max_tokens ({ORCHESTRATOR_MAX_TOKENS}) — "
                        "response truncated. Raise ORCHESTRATOR_MAX_TOKENS in .env or shorten DAG.[/yellow]"
                    )
                text = response.content[0].text.strip()
                text = _strip_fences(text)
                text = _fix_json_strings(text)
                parsed = json.loads(text)
                validate_response(state, parsed)
                return parsed

            except anthropic.APITimeoutError as exc:
                last_error = exc
                console.print(
                    f"[yellow]Orchestrator timed out after {ORCHESTRATOR_TIMEOUT}s "
                    f"(attempt {attempt + 1}/{max_retries + 1}) — retrying...[/yellow]"
                )
                if attempt < max_retries:
                    time.sleep(2 + attempt)

            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                last_error = exc
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Orchestrator output invalid (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{exc}  — retrying...[/yellow]"
                    )
                    time.sleep(1 + attempt)

        raise RuntimeError(f"Orchestrator failed after {max_retries + 1} attempts: {last_error}") from last_error


class OpenRouterOrchestrator:
    def __init__(self) -> None:
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to harness/.env.")
        from openai import OpenAI
        self._client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"X-Title": "J-Claw"},
        )
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")

    def call(self, payload: dict, max_retries: int = 3) -> dict:
        from openai import RateLimitError
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)
        last_error: Exception | None = None

        model_chain = [ORCHESTRATOR_API_MODEL] + ORCHESTRATOR_FALLBACK_MODELS
        current_model = model_chain[0]
        model_idx = 0

        for attempt in range(max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=current_model,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                )
                text = response.choices[0].message.content.strip()
                text = _strip_fences(text)
                text = _fix_json_strings(text)
                parsed = json.loads(text)
                validate_response(state, parsed)
                return parsed

            except RateLimitError as exc:
                last_error = exc
                # Try next fallback model before waiting
                model_idx += 1
                if model_idx < len(model_chain):
                    current_model = model_chain[model_idx]
                    console.print(f"[yellow]Orchestrator rate limited — switching to fallback: {current_model}[/yellow]")
                else:
                    # All models exhausted for this attempt — wait then reset
                    model_idx = 0
                    current_model = model_chain[0]
                    try:
                        wait = int(exc.response.json()["error"]["metadata"]["retry_after_seconds"]) + 2
                    except Exception:
                        wait = 35 * (attempt + 1)
                    console.print(f"[yellow]All orchestrator models rate limited — waiting {wait}s (attempt {attempt + 1}/{max_retries + 1})…[/yellow]")
                    if attempt < max_retries:
                        time.sleep(wait)

            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                last_error = exc
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Orchestrator output invalid (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{exc}  — retrying...[/yellow]"
                    )
                    time.sleep(2 + attempt)

        raise RuntimeError(
            f"OpenRouterOrchestrator failed after {max_retries + 1} attempts: {last_error}"
        ) from last_error


def _sanitize(text: str) -> str:
    """Strip control characters that Notepad or copy-paste can silently insert."""
    import re
    # Keep only tab (\x09), newline (\x0a), carriage return (\x0d), and printable chars
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _fix_json_strings(text: str) -> str:
    """Replace literal newlines/tabs inside JSON string values that break json.loads."""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            pass  # strip bare CR
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _strip_fences(text: str) -> str:
    """Remove accidental ```json ... ``` wrapping that the model sometimes adds."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    # drop first line (```json or ```) and last line (```)
    inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
