# macOS agent

Same polling contract as the Linux agent, launched via **launchd** as the
logged-in user (so desktop automation, when enabled, runs in that user's
session and can use Screen Recording / Accessibility TCC grants).

## Install (LaunchAgent)

```sh
cp deployment/macos/com.aibridge.agent.plist ~/Library/LaunchAgents/
# Edit the plist: REPO_PATH, CONTROL_URL, DEVICE_TOKEN (from enrollment)
launchctl load ~/Library/LaunchAgents/com.aibridge.agent.plist
```

Or use the built-in installer from the agent CLI:

```sh
python agents/macos/agent_main.py enroll --admin-token <ADMIN_TOKEN>
python agents/macos/agent_main.py service install
python agents/macos/agent_main.py service start
```

Logs: `/tmp/ai-bridge-agent.out` and `/tmp/ai-bridge-agent.err`.

## Interactive Terminal Studio (TUI)

Full-screen curses dashboard (or line REPL fallback) with overview, poller,
enrollment, policy/launchd, and doctor/TCC tabs.

```sh
python agents/macos/agent_main.py tui
python agents/macos/agent_main.py tui --snapshot          # ASCII dashboard once
python agents/macos/agent_main.py tui --snapshot --json # CI / headless state
```

GitHub Releases may ship a zipapp bundle `ai-bridge-macos-tui` (same subcommands).

## TCC permission doctor

`doctor` includes a **TCC** section (Screen Recording via
`CGPreflightScreenCaptureAccess`, Accessibility via `AXIsProcessTrusted`).
On non-macOS hosts these checks return structured `not_applicable` for CI.

```sh
python agents/macos/agent_main.py doctor
python agents/macos/agent_main.py status   # includes TCC summary
```

Grant permissions in **System Settings → Privacy & Security** when the doctor
reports `denied`. `tccutil` is reported when present (reset-only utility).

## Status

Interactive Terminal Studio and launchd helpers are portable Python; full
launchd load/unload and live TCC grants require a real Mac login session.