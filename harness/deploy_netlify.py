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

Run by deploy_project() with cwd=<project output dir>. Stdlib only.
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
    for name in ("netlify.cmd", "netlify"):
        path = shutil.which(name)
        if path:
            return path
    _fail("netlify CLI not found on PATH (npm install -g netlify-cli)")
    raise SystemExit  # unreachable; keeps type-checkers happy


def _run(args: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "CI": "1"})


def _site_slug(project_dir: Path) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", project_dir.name.lower()).strip("-")
    return f"jclaw-{slug}"[:63].rstrip("-")


def _find_or_create_site(cli: str, token: str, slug: str) -> str:
    """Return the site id for `slug`, creating the site if needed."""
    result = _run([cli, "api", "listSites", "--auth", token])
    if result.returncode != 0:
        _fail(f"listSites failed (bad token?): {(result.stderr or result.stdout)[-300:]}")
    try:
        sites = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail(f"listSites returned non-JSON: {result.stdout[:300]}")
    for site in sites:
        if site.get("name") == slug:
            return site["id"]

    result = _run([cli, "api", "createSite", "--data", json.dumps({"name": slug}),
                   "--auth", token])
    if result.returncode != 0:
        _fail(f"createSite failed: {(result.stderr or result.stdout)[-300:]}")
    try:
        return json.loads(result.stdout)["id"]
    except (json.JSONDecodeError, KeyError):
        _fail(f"createSite returned unexpected output: {result.stdout[:300]}")
    raise SystemExit  # unreachable


def main() -> None:
    token = os.environ.get("NETLIFY_AUTH_TOKEN", "").strip()
    if not token:
        _fail("NETLIFY_AUTH_TOKEN is not set — add it to harness/.env "
              "(app.netlify.com → User settings → Applications → New access token)")

    project_dir = Path.cwd()
    publish_dir = project_dir / "dist" if (project_dir / "dist").is_dir() else project_dir
    cli = _netlify_cli()
    slug = _site_slug(project_dir)
    site_id = _find_or_create_site(cli, token, slug)

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
