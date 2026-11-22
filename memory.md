# AI Bridge (`bridgeai3`) — Project Memory

This document captures the architecture, build/test workflows, local runtime paths, design decisions, and solved pitfalls for the **AI Bridge** repository so future sessions and contributors can ramp up immediately.

---

## 1. Project Overview & Architecture

AI Bridge is a multi-platform MCP server, FastAPI control plane, web management dashboard, and native desktop agent suite that enables AI models to safely inspect and operate remote or local machines.

| Layer | Location | Stack | Purpose |
| :--- | :--- | :--- | :--- |
| **Core Package & Control Plane** | `src/ai_bridge/` | Python 3.11+, FastAPI, MCP | MCP server (`server.py`), policy engine (`policy.py`), tool registry, WebRTC streaming, and FastAPI control plane (`control/`). |
| **Management Dashboard** | `apps/dashboard/` | React, TypeScript, Vite | Web UI for fleet management, live screen viewing, device policy sync, and audit logs. |
| **Windows Client App** | `apps/windows-client/` | .NET 8 WPF (C#) | Desktop client with embedded autonomous AI Agent, Screen Takeover HUD, Vision-in-the-Loop, offline OCR, Voice input, Playbooks, Memory, and Action Timeline/Undo. |
| **Windows Client Tests** | `apps/windows-client.Tests/` | .NET 8 C# | Unit test runner covering agent runtime, takeover lock, native pipe framing, and tool execution (`35/35` passing). |
| **Native Windows Agent** | `windows-agent/` | C++20, CMake, DXGI, Media Foundation | Native DLL (`ai_bridge_native.dll`), background service (`ai_bridge_service.exe`), desktop helper (`ai_bridge_desktop.exe`), and C++ unit tests (`ai_bridge_tests.exe`) for hardware H.264 capture and named-pipe IPC. |
| **Platform Polling Agents** | `agents/{linux,macos,windows}/` | Python / Platform scripts | Host-specific polling agents isolated by OS. |
| **Tests & Migrations** | `tests/`, `migrations/` | pytest, SQL | Unit, integration, security, and E2E suites plus DB migrations. |

---

## 2. Windows Client (`apps/windows-client/`) Key Modules

All core services live in `apps/windows-client/Services/`:

- **Agent Runtime & Tools:**
  - `DeviceAgentRuntimeService.cs` — Autonomous multi-step AI agent loop, provider/model routing, Vision-in-the-Loop JPEG screenshot feedback, Auto-Verify post-action loop, text `<tool_call>` fallback parser, and effective system prompt builder.
  - `NativeToolExecutor.cs` — Executes local desktop tools (`mouse_click`, `mouse_draw_path`, `type_text`, `press_hotkey`, `ocr_screen`, `click_text_ocr`, `find_ui_elements`, `capture_desktop_screenshot`, `remember_fact`/`forget_fact`/`list_memory`, WebRTC H.264 stream controls, etc.).
  - `DesktopInputService.cs` — Human-like cubic Bezier mouse gliding, Catmull-Rom curve smoothing, speed profiles (`Fast` / `Natural` / `Careful`), natural keyboard typing cadence, and geometric drawing primitives.
  - `OcrService.cs` — Offline `Windows.Media.Ocr` engine indexing full lines, 2-word and 3-word phrase spans (with exact union bounding boxes), and single words.
  - `UiAutomationService.cs` — Windows UI Automation tree traversal (default `maxDepth: 10`, max `16` to reach deep Chrome/Electron DOM elements).
  - `ScreenCaptureService.cs` & `WindowManagementService.cs` — Screen capture, window enumeration, and Window/Monitor Pinning (Scoped Focus Mode).
- **Takeover & Native C++20 Bridge:**
  - `AgentTakeoverService.cs` — Screen Takeover state machine with `WH_MOUSE_LL` + `WH_KEYBOARD_LL` input lock, Pause/Resume, animated neon border glow, floating top HUD pill, click ripples, step counter, and 60Hz cursor halo.
  - `CppNativeBridge.cs` & `NamedPipeBridge.cs` — P/Invoke bridge to `ai_bridge_native.dll` and HMAC-signed wire protocol (`SignEnvelope` / `BuildCppWireMessage`) to `ai_bridge_service.exe`.
- **Persistence, UX & Control Plane:**
  - `ConfigService.cs` & `DpapiTokenStore.cs` — Local config (`~/.ai-bridge/agent_gui.json`) and Windows DPAPI-protected credential storage.
  - `ControlPlaneClient.cs` — Heartbeat, two-way policy sync, and command polling with the control plane.
  - `ChatHistoryService.cs` — Persistent chat sessions (`~/.ai-bridge/agent_chats.json`), search, delete, and Markdown export.
  - `AgentMemoryService.cs` — Persistent agent memory store and Memory drawer integration.
  - `AgentPlaybookService.cs` — Reusable 1-click Playbooks and Command Palette launcher.
  - `AgentActionTimelineService.cs` — Action timeline and one-click Undo for file edits and UI actions.
  - `VoiceInputService.cs` — Push-to-Talk Windows Speech voice input.

### Notable UI Features (`MainWindow.xaml` / `Views/`)
- Windows 11 DWM Mica backdrop, smooth tab transitions, keyboard shortcuts (`Ctrl+1`–`7`, `Ctrl+N`), and Global Quick-Command Overlay (`Ctrl+Shift+Space`).
- Collapsible tool step cards, message Copy button, screenshot lightbox, drag-and-drop file attachments, and `Ctrl+V` screenshot paste.

---

## 3. Build, Test & Run Commands

### Python Control Plane & Tests
```bash
pip install -e ".[dev]"
ruff check .
pytest -v
python -m ai_bridge.control
```

### Dashboard (`apps/dashboard/`)
```bash
npm --prefix apps/dashboard ci
npm --prefix apps/dashboard run dev
npm --prefix apps/dashboard run typecheck
npm --prefix apps/dashboard run build
```

### Windows Client (`apps/windows-client/`)
```powershell
# Build Release
dotnet build apps/windows-client/AIBridgeClient.csproj -c Release

# Run C# Unit Tests (35 tests)
dotnet run --project apps/windows-client.Tests/AIBridgeClient.Tests.csproj -c Release

# Publish to local install directory
dotnet publish apps/windows-client/AIBridgeClient.csproj -c Release -o "$env:LOCALAPPDATA\Programs\AIBridgeClient"
```

### Native C++20 Windows Agent (`windows-agent/`)
Built via CMake (`windows-agent/CMakeLists.txt`). Do **not** commit `windows-agent/build/`, `bin/`, or `obj/`.

---

## 4. Local Runtime Paths (Windows)

- **Installed Binaries:** `%LOCALAPPDATA%\Programs\AIBridgeClient\` (`AIBridgeClient.exe`, `ai_bridge_service.exe`, `ai_bridge_desktop.exe`, `ai_bridge_native.dll`)
- **Agent Configuration:** `%USERPROFILE%\.ai-bridge\agent_gui.json` (ensure `max_tool_steps` is ≥ `20` for multi-step browser/desktop workflows)
- **Chat History:** `%USERPROFILE%\.ai-bridge\agent_chats.json`
- **Client Logs:** `%USERPROFILE%\.ai-bridge\logs\ai-bridge-client.log`

---

## 5. Key Decisions & Solved Pitfalls

Keep these lessons in mind to avoid regressions:

1. **Multi-word OCR Matching (`OcrService.cs` & `NativeToolExecutor.cs`):**
   - `Windows.Media.Ocr` returns individual words (`"Sign"`, `"In"`). Always index full lines and 2–3 word phrase spans with union bounding boxes so `click_text_ocr(text="Sign In")` resolves exact center coordinates, with automatic fallback to deep UI Automation (`maxDepth: 12`).
2. **Chrome / Electron UIA Depth (`UiAutomationService.cs`):**
   - Browser DOM controls sit 8–14 levels deep in the UIA tree. Keep default `maxDepth` at `10` (max `16`); lower depths (e.g., `6`) miss web page buttons.
3. **No Per-Frame Approval Popups During Screen Sharing (`ControlPlaneClient.cs`):**
   - Live screen capture / vision polling tools must not be classified as sensitive tools requiring per-call local approval popups (`IsSensitiveTool`).
4. **WPF / System Namespace Ambiguities:**
   - Because the WPF project references both WPF and WinForms/System drawing APIs, explicitly qualify `System.Windows.Media.Effects` and `System.Windows.Controls.Orientation` in overlay windows.
5. **`.NET` API Compatibility:**
   - Avoid `Math.Hypot` where unavailable; use `Math.Sqrt(dx * dx + dy * dy)`.
   - Keep `NamedPipeBridge.SignEnvelope` preserved as a wrapper over `BuildCppWireMessage` for unit test compatibility.
6. **Security & Hygiene (`AGENTS.md`):**
   - Never commit tokens, credentials, `.pem` keys, or `.env` files.
   - Always add policy tests when changing a tool, capability tier, or dispatch path.
   - Ruff line limit is `200`; Python uses 4-space indentation, `snake_case`, and type hints.

---

## 6. Recent Major Milestones

- `d08de7c` / `632df56` / `c9eab41` — Full C++20 `windows-agent` DLL, named-pipe server, DXGI/MF H.264 engine, settings UI, and CI integration.
- `7445f94` — Eliminated per-frame local approval popups during live screen sharing.
- `d195028` / `9daf859` — Agent Screen Takeover input lock, animated neon border glow, and HUD pill.
- `793a2c6` / `dbaa647` — Autonomous desktop operator mode, `mouse_draw_path`, text `<tool_call>` fallback, and human-like Bezier mouse/keyboard physics.
- `3468941` → `b6b4e76` — 7 core agent enhancements: Vision-in-the-Loop, live action streaming + Stop button, offline Windows OCR, `Ctrl+Shift+Space` Quick-Command Overlay, WebRTC H.264 stream toggle, speed profiles + Catmull-Rom smoothing + 60Hz cursor halo, and Action Timeline + Undo.
- `1ceabc7` → `235c2ee` — 10 UI/UX & feature enhancements: collapsible tool cards + lightbox, persistent chat history + Markdown export, drag-and-drop + `Ctrl+V` paste, HUD click ripples + Pause/Resume, Win11 Mica + shortcuts, Push-to-Talk voice, 1-click Playbooks, persistent Agent Memory, Window/Monitor Pinning, and Auto-Verify visual loop.
- `05e0126` — Fixed multi-word `click_text_ocr` matching, deep Chrome UIA clicking, and `max_tool_steps` for browser automation.
