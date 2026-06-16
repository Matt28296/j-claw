import sys, json, shutil, time
import worker as w

# Speed the live call up: low effort overrides codex's configured "high".
w.CODEX_EFFORT = "low"

print("exe resolved:", shutil.which("codex") or shutil.which("codex.cmd") or "codex")
print("model:", w.CODEX_MODEL, "| effort:", w.CODEX_EFFORT, "| timeout:", w.CODEX_TIMEOUT, flush=True)

system = ('You are a code-writing worker. Output ONLY a single JSON object, no prose, no '
          'markdown fences, matching exactly: {"files":[{"path":"<relative path>",'
          '"content":"<full file content>"}]}. No text outside the JSON.')
user = ('Create a file named hello.html: a minimal valid HTML5 document whose body '
        'contains exactly <h1>Hello from Codex</h1>.')

t0 = time.monotonic()
raw = w._call_codex(w.CODEX_MODEL, system, user)
dt = time.monotonic() - t0
print(f"\n--- _call_codex returned in {dt:.1f}s, {len(raw)} chars ---")
print(raw[:800])

# Validate it parses as the worker contract.
print("\n--- parse check ---")
try:
    obj = json.loads(raw)
    files = obj.get("files")
    assert files and files[0].get("path") and files[0].get("content"), "missing files/path/content"
    print("PARSE OK: path =", files[0]["path"], "| content len =", len(files[0]["content"]))
except Exception as e:
    print("PARSE via json.loads failed:", e)

# Telemetry check
from cost import cost_summary
print("\n--- oauth telemetry ---")
print(json.dumps(cost_summary().get("oauth", {}), indent=2))
