from __future__ import annotations
import json
import time
from pathlib import Path
import anthropic
from rich.console import Console
from rich.syntax import Syntax

from config import ORCHESTRATOR_MODEL, ANTHROPIC_API_KEY, ORCHESTRATOR_PROMPT_PATH
from validator import validate_response, OrchestratorOutputError

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
                response = self._client.messages.create(
                    model=ORCHESTRATOR_MODEL,
                    max_tokens=8096,
                    system=self._system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                text = response.content[0].text.strip()
                text = _strip_fences(text)
                parsed = json.loads(text)
                validate_response(state, parsed)
                return parsed

            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                last_error = exc
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Orchestrator output invalid (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{exc}  — retrying...[/yellow]"
                    )
                    time.sleep(1 + attempt)

        raise RuntimeError(f"Orchestrator failed after {max_retries + 1} attempts: {last_error}") from last_error


def _sanitize(text: str) -> str:
    """Strip control characters that Notepad or copy-paste can silently insert."""
    import re
    # Keep only tab (\x09), newline (\x0a), carriage return (\x0d), and printable chars
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


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
