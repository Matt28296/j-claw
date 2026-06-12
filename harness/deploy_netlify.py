#!/usr/bin/env python3
"""Unattended Netlify deploy wrapper — the factory's DEPLOY_HOOK target.

A bare `netlify deploy --prod` in a fresh, unlinked project directory prompts
interactively to link or create a site, which is fatal for a hands-off build.
This wrapper makes the deploy deterministic:

  1. Auth comes from NETLIFY_AUTH_TOKEN (harness/.env) — loud exit if unset.
  2. One Netlify site per project: site name `jclaw-<project-dir-slug>`,
     found via `netlify api listSites` or created via `netlify api createSite`.
     Re-running the same project redeploys the same site (same URL).
  3. Publish dir is `dist/` when present (react-vite build output), else cwd
     (vanilla/phaser/three-js static output).
  4. `netlify deploy --prod --json` and print exactly one `https://…` line —
     handoff.deploy_project() extracts the first URL-bearing line unchanged.

Run by deploy_project() with cwd=<project output dir>. Stdlib only, except an
optional dotenv load so standalone runs see harness/.env.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_TIMEOUT = 240


def _fail(msg: str) -> None:
    print(f"deploy_netlify: {msg}", file=sys.stderr)
    sys.exit(1)


def _netlify_cli() -> str:
    """First netlify CLI that actually runs. A stale standalone install can
    shadow the working npm-global one on PATH (observed live: MODULE_NOT_FOUND
    under a bundled Node 20), so every candidate is probed with --version."""
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\netlify.cmd"),
        shutil.which("netlify.cmd"),
        shutil.which("netlify"),
    ]
    seen: set[str] = set()
    errors: list[str] = []
    for path in candidates:
        if not path or path in seen or not Path(path).exists():
            continue
        seen.add(path)
        try:
            probe = subprocess.run([path, "--version"], capture_output=True,
                                   text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
            continue
        if probe.returncode == 0:
            return path
        errors.append(f"{path}: exit {probe.returncode}")
    _fail("no working netlify CLI found (npm install -g netlify-cli). Tried: "
          + "; ".join(errors or ["nothing on PATH"]))
    raise SystemExit  # unreachable; keeps type-checkers happy


def _run(args: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "CI": "1"})


def _site_slug(project_dir: Path) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", project_dir.name.lower()).strip("-")
    return f"jclaw-{slug}"[:63].rstrip("-")


_API = "https://api.netlify.com/api/v1"


def _api_request(token: str, path: str, payload: dict | None = None):
    """GET (payload None) or POST against the Netlify REST API.

    Direct REST instead of `netlify api …` — passing JSON through the CLI's
    Windows cmd shim strips the quotes (observed live: createSite ignored the
    mangled name and minted a random one, breaking re-deploy idempotency)."""
    import urllib.error
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300]
        _fail(f"Netlify API {path} failed: HTTP {exc.code} {body!r}"
              + (" (bad token?)" if exc.code in (401, 403) else ""))
    except Exception as exc:  # noqa: BLE001
        _fail(f"Netlify API {path} failed: {exc}")


def _find_or_create_site(token: str, slug: str) -> str:
    """Return the site id for `slug`, creating the site if needed."""
    sites = _api_request(token, "/sites?per_page=100")
    for site in sites:
        if site.get("name") == slug:
            return site["id"]
    site = _api_request(token, "/sites", {"name": slug})
    site_id = site.get("id")
    if not site_id:
        _fail(f"createSite returned no id: {json.dumps(site)[:300]}")
    return site_id


def main() -> None:
    # Self-load harness/.env so the wrapper also works standalone (when run by
    # deploy_project the parent harness already exported it; when run by hand
    # for testing there is no parent to inherit from).
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    except ImportError:
        pass
    token = os.environ.get("NETLIFY_AUTH_TOKEN", "").strip()
    if not token:
        _fail("NETLIFY_AUTH_TOKEN is not set — add it to harness/.env "
              "(app.netlify.com → User settings → Applications → New access token)")

    project_dir = Path.cwd()
    publish_dir = project_dir / "dist" if (project_dir / "dist").is_dir() else project_dir
    cli = _netlify_cli()
    slug = _site_slug(project_dir)
    site_id = _find_or_create_site(token, slug)

    result = _run([cli, "deploy", "--prod", "--json", "--auth", token,
                   "--site", site_id, "--dir", str(publish_dir)])
    if result.returncode != 0:
        _fail(f"deploy failed (exit {result.returncode}): "
              f"{(result.stderr or result.stdout)[-500:]}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fall back to scraping a URL out of whatever the CLI printed.
        m = re.search(r"https://\S+", result.stdout + result.stderr)
        if not m:
            _fail(f"deploy succeeded but no URL found in output: {result.stdout[:300]}")
        print(m.group(0))
        return
    url = info.get("ssl_url") or info.get("url") or info.get("deploy_ssl_url")
    if not url:
        _fail(f"deploy JSON has no url field: {result.stdout[:300]}")
    print(url)


if __name__ == "__main__":
    main()
