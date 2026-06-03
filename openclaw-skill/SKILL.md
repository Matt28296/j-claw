---
name: j-claw
description: >
  Build complete software projects autonomously — websites, apps, DApps, games, films, dashboards, and more.
  Generates a full project from a plain-English description using a local Ollama coding model.
  Supports 14 stacks: vanilla, react-vite, fastapi, full-stack, phaser, web3, react-native,
  socket-io, three-js, electron, film/explainer, tauri, godot4, realtime-dashboard.
version: 1.0.0
skillKey: build

metadata:
  openclaw:
    requires:
      bins:
        - python
      env:
        - ANTHROPIC_API_KEY
    envVars:
      - name: ANTHROPIC_API_KEY
        description: Anthropic API key for the orchestrator (Claude)
      - name: WORKER_MODEL
        description: Ollama model to use for code writing (default qwen2.5-coder:14b)
        optional: true
      - name: OLLAMA_HOST
        description: Ollama server URL (default http://localhost:11434)
        optional: true
---

# J-Claw — Autonomous Software Builder

J-Claw is a local-first autonomous coding pipeline. You describe what you want; it generates a complete, working project using a local Ollama model as the code writer.

## Location

The pipeline lives at: `C:\Users\Tyler\Desktop\Jarvis-Claw`

Run script: `C:\Users\Tyler\Desktop\Jarvis-Claw\run.bat`

## How to use this skill

When the user asks you to **build**, **create**, **generate**, or **make** a software project, invoke j-claw.

### Build a new project

```
cd C:\Users\Tyler\Desktop\Jarvis-Claw && .\run.bat --yes "<user's description>"
```

**Examples:**
- `.\run.bat --yes "A snake game in the browser"`
- `.\run.bat --yes "A task manager with React frontend and FastAPI backend"`
- `.\run.bat --yes "A Solidity NFT contract with a minting frontend"`
- `.\run.bat --yes "A real-time multiplayer drawing game"`
- `.\run.bat --yes "build me a 30-second explainer about machine learning"`
- `.\run.bat --yes "make a Tauri desktop app that shows system CPU usage"`
- `.\run.bat --yes "create a Godot 4 platformer game"`
- `.\run.bat --yes "build a real-time dashboard showing live WebSocket data"`

**Typical build times:**
- Code / app / website projects: 5–15 minutes
- Games: 10–20 minutes
- Film / video projects: 30–60 minutes (SD frame generation + ffmpeg)

### Add a feature to an existing project

```
cd C:\Users\Tyler\Desktop\Jarvis-Claw && .\run.bat --continue "harness\projects\<project-folder>" "<what to add>"
```

### List recent projects

```
dir C:\Users\Tyler\Desktop\Jarvis-Claw\harness\projects /od /b
```

### Check pipeline status (during a run)

```
type C:\Users\Tyler\Desktop\Jarvis-Claw\harness\mission_control.json
```

## What gets built

Each run produces a complete project in `harness/projects/<name>/` with:
- All source files (frontend + backend as needed)
- Auto-verified by the harness (npm build, Python install, etc.)
- Final OpenClaw review stamp (pass/fail)
- Git commit of the output

## Supported stacks

| You say | Stack used |
|---|---|
| "web app", "landing page", "static site" | vanilla (HTML/CSS/JS + Tailwind) |
| "React app", "dashboard", "SPA" | react-vite |
| "API", "backend", "FastAPI" | fastapi |
| "full-stack", "React + backend", "with a database" | full-stack |
| "game", "browser game" | phaser or three-js |
| "smart contract", "DApp", "NFT", "Web3" | web3 |
| "mobile app" | react-native (Expo) |
| "multiplayer", "real-time", "chat app" | socket-io |
| "desktop app", "Electron app" | electron |
| "explainer video", "animated explainer", "short film" | film/explainer (ffmpeg + SD frames + Coqui TTS, 30–90 seconds) |
| "Tauri app", "lightweight desktop app" | tauri (Rust + WebView, lighter than Electron) |
| "Godot game", "Godot 4", "GDScript game" | godot4 (GDScript, headless export) |
| "live dashboard", "real-time dashboard", "WebSocket dashboard" | realtime-dashboard (Node.js + WebSocket or SSE) |

## Output

After the build completes, report:
- Project folder path
- Number of tasks completed
- Pass/fail from the OpenClaw review
- How to run it (e.g. `cd harness/projects/<name> && npm run dev`)
