/**
 * AI Bridge Windows Service (Process A - Session 0 Supervisor)
 *
 * Responsibilities (Section 9.A):
 * - Starts automatically with Windows (or runs in --console mode for verification).
 * - Registers the device and protects credentials with DPAPI.
 * - Maintains authenticated backend connectivity & heartbeats.
 * - Enforces device-level policy.
 * - Monitors the interactive desktop agent in the active console/user session.
 * - Communicates with the Interactive Desktop Agent over the restricted
 *   local Named Pipe (\\.\pipe\AIBridgeDesktopPipe).
 *
 * F08-02 (Wave 5A): the service is a supervisor now, not a 1 s poll loop
 * that recreated the pipe on every iteration with a 250 ms connect window
 * and one message per iteration. It now:
 *   - serves ONE persistent pipe: it waits for the desktop agent to
 *     connect and handles every message on that connection until the
 *     agent disconnects (register / heartbeat / status, with replies);
 *   - tracks desktop-agent liveness (last REGISTER/HEARTBEAT seen) and
 *     relaunches the desktop agent in the active console session when
 *     it has been silent for too long;
 *   - heartbeats the control plane (POST /api/agent/heartbeat) every
 *     30 s when control_url + device_token are configured.
 * Enrollment stays installer-driven (install-service.ps1 issues the
 * device token) and policy enforcement stays with the control plane +
 * agent policy merge; this binary supervises liveness and transport.
 */

#include "../monitoring/system_monitor.hpp"
#include "../security/local_security.hpp"
#include "../input/sendinput_controller.hpp"
#include "../mcp/mcp_executor.hpp"

#include <winhttp.h>
#include <wtsapi32.h>
#include <userenv.h>

#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <chrono>

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "wtsapi32.lib")
#pragma comment(lib, "userenv.lib")

namespace {
SERVICE_STATUS g_service_status{};
SERVICE_STATUS_HANDLE g_status_handle = nullptr;
HANDLE g_stop_event = INVALID_HANDLE_VALUE;
std::wstring g_service_name_w = L"AIBridgeAgent";
std::string g_pipe_secret;
std::wstring g_pipe_name_w = L"\\\\.\\pipe\\AIBridgeDesktopPipe";
// F08-02: supervisor state, from the service config file (DPAPI-unsealed
// where secret). Empty control_url/device_token means "no control-plane
// heartbeat" — the pipe supervision still runs.
std::string g_control_url;
std::string g_device_token;
std::string g_desktop_agent_path;
std::string g_config_path;
aibridge::input::WindowsInputController g_input_controller;

bool ExtractJsonBoolField(const std::string& json, const std::string& key, bool fallback = false) {
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return fallback;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return fallback;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) ++pos;
    if (json.compare(pos, 4, "true") == 0) return true;
    if (json.compare(pos, 5, "false") == 0) return false;
    return fallback;
}

// F08-02: liveness tracking. Initialised to process start so a freshly
// started service gives the desktop agent a full grace period before
// it is considered stale.
std::chrono::steady_clock::time_point g_last_desktop_contact = std::chrono::steady_clock::now();
std::chrono::steady_clock::time_point g_last_heartbeat = std::chrono::steady_clock::now();
std::chrono::steady_clock::time_point g_last_relaunch_attempt{};
std::chrono::steady_clock::time_point g_last_stale_warning{};
constexpr std::chrono::seconds kDesktopStaleAfter{120};
constexpr std::chrono::seconds kRelaunchCooldown{120};
constexpr std::chrono::seconds kHeartbeatInterval{30};
constexpr std::chrono::seconds kStaleWarningInterval{60};

std::string ExtractJsonStringField(const std::string& json, const std::string& key) {
    // F08-15: hand-rolled JSON extraction — see the full justification
    // in windows-agent/src/webrtc/http_signaling.cpp (no JSON library
    // is vendored in this tree; this extracts a fixed set of string
    // fields from the service's own config file and must not grow into
    // a general parser).
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return {};
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    pos = json.find('"', pos + 1);
    if (pos == std::string::npos) return {};
    ++pos;
    std::string out;
    while (pos < json.size()) {
        char c = json[pos++];
        if (c == '\\' && pos < json.size()) {
            char next = json[pos++];
            out.push_back(next);
            continue;
        }
        if (c == '"') break;
        out.push_back(c);
    }
    return out;
}

std::wstring ToWide(const std::string& s) {
    if (s.empty()) return {};
    int needed = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (needed <= 0) return {};
    std::wstring w(needed, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), needed);
    return w;
}

std::string JsonEscape(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == '"' || c == '\\') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

std::string DefaultDesktopAgentPath() {
    // The desktop agent ships next to the service executable.
    wchar_t module_path[MAX_PATH] = {0};
    if (GetModuleFileNameW(nullptr, module_path, MAX_PATH) == 0) return {};
    std::wstring dir(module_path);
    size_t slash = dir.find_last_of(L"\\/");
    if (slash != std::wstring::npos) dir = dir.substr(0, slash + 1);
    else dir.clear();
    dir += L"ai_bridge_desktop.exe";
    char narrow[MAX_PATH] = {0};
    if (WideCharToMultiByte(CP_UTF8, 0, dir.c_str(), -1, narrow, MAX_PATH, nullptr, nullptr) == 0) return {};
    return std::string(narrow);
}

// F08-08 follow-up: the installer writes DPAPI-sealed secrets
// ("aib-dpapi-v1:<base64>") into the config and into the pipe-secret file.
// Every source is unsealed here via DpapiSecretStore::UnsealIfSealed.
// Returns false if a sealed secret was present but could not be unsealed;
// callers must fail loudly on false and NEVER use the sealed value as the
// secret itself.
bool LoadServiceConfig(const std::string& config_path) {
    auto unseal_or_fail = [](const std::string& raw, const char* what, std::string& out) -> bool {
        if (!aibridge::security::DpapiSecretStore::UnsealIfSealed(raw, out)) {
            std::cerr << "[AI Bridge] FATAL: could not DPAPI-unseal " << what
                      << "; refusing to start with a sealed value as the secret.\n";
            return false;
        }
        return true;
    };
    if (!config_path.empty()) {
        g_config_path = config_path;
        std::ifstream ifs(config_path);
        if (ifs) {
            std::ostringstream ss;
            ss << ifs.rdbuf();
            std::string body = ss.str();
            std::string secret = ExtractJsonStringField(body, "pipe_secret");
            if (!secret.empty() && !unseal_or_fail(secret, "pipe_secret from the config file", g_pipe_secret)) return false;
            std::string pname = ExtractJsonStringField(body, "pipe_name");
            if (!pname.empty()) g_pipe_name_w = ToWide(pname);
            // F08-02: supervisor settings (control-plane heartbeat +
            // desktop-agent relaunch). All optional; absent means the
            // corresponding supervision stays off.
            std::string curl = ExtractJsonStringField(body, "control_url");
            if (!curl.empty()) g_control_url = curl;
            std::string dtok = ExtractJsonStringField(body, "device_token");
            if (!dtok.empty() && !unseal_or_fail(dtok, "device_token from the config file", g_device_token)) return false;
            std::string dapath = ExtractJsonStringField(body, "desktop_agent_path");
            if (!dapath.empty()) g_desktop_agent_path = dapath;
        }
    }
    if (g_pipe_secret.empty()) {
        const char* env_sec = std::getenv("AI_BRIDGE_PIPE_SECRET");
        if (env_sec && *env_sec && !unseal_or_fail(env_sec, "pipe_secret from AI_BRIDGE_PIPE_SECRET", g_pipe_secret)) return false;
    }
    if (g_pipe_secret.empty()) {
        char prog_data[MAX_PATH] = {0};
        if (GetEnvironmentVariableA("ProgramData", prog_data, MAX_PATH) > 0) {
            std::ifstream sf(std::string(prog_data) + "\\ai-bridge\\native-pipe.secret");
            std::string file_secret;
            if (sf) std::getline(sf, file_secret);
            if (!file_secret.empty() && !unseal_or_fail(file_secret, "pipe_secret from the native-pipe.secret file", g_pipe_secret)) return false;
        }
    }
    if (g_control_url.empty()) {
        const char* env_url = std::getenv("AI_BRIDGE_CONTROL_URL");
        if (env_url && *env_url) g_control_url = env_url;
    }
    if (g_device_token.empty()) {
        const char* env_tok = std::getenv("AI_BRIDGE_DEVICE_TOKEN");
        if (env_tok && *env_tok && !unseal_or_fail(env_tok, "device_token from AI_BRIDGE_DEVICE_TOKEN", g_device_token)) return false;
    }
    if (g_desktop_agent_path.empty()) {
        g_desktop_agent_path = DefaultDesktopAgentPath();
    }
    return true;
}

// F08-02: relaunch the desktop agent in the active console session.
// Session 0 cannot CreateProcess into the interactive session
// directly; WTSQueryUserToken yields the logged-on user's token and
// CreateProcessAsUserW starts the agent there. Best-effort: a failure
// is logged and retried after the cooldown, never fatal.
bool LaunchDesktopAgentInActiveSession() {
    if (g_desktop_agent_path.empty()) return false;
    {
        std::ifstream probe(g_desktop_agent_path);
        if (!probe) {
            std::cerr << "[AI Bridge] supervisor: desktop agent binary not found at "
                      << g_desktop_agent_path << "; cannot relaunch.\n";
            return false;
        }
    }
    DWORD session_id = WTSGetActiveConsoleSessionId();
    if (session_id == 0xFFFFFFFF) {
        std::cerr << "[AI Bridge] supervisor: no active console session; cannot relaunch desktop agent.\n";
        return false;
    }
    HANDLE hToken = nullptr;
    if (!WTSQueryUserToken(session_id, &hToken)) {
        std::cerr << "[AI Bridge] supervisor: WTSQueryUserToken failed (" << GetLastError() << ").\n";
        return false;
    }
    LPVOID env_block = nullptr;
    BOOL env_ok = CreateEnvironmentBlock(&env_block, hToken, FALSE);
    std::string cmdline = "\"" + g_desktop_agent_path + "\"";
    if (!g_config_path.empty()) {
        cmdline += " --config \"" + g_config_path + "\"";
    }
    std::wstring cmdline_w = ToWide(cmdline);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.lpDesktop = const_cast<LPWSTR>(L"winsta0\\default");
    PROCESS_INFORMATION pi{};
    BOOL started = CreateProcessAsUserW(
        hToken, nullptr, cmdline_w.data(),
        nullptr, nullptr, FALSE,
        CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_CONSOLE,
        env_ok ? env_block : nullptr, nullptr, &si, &pi);
    DWORD err = GetLastError();
    if (env_block) DestroyEnvironmentBlock(env_block);
    CloseHandle(hToken);
    if (!started) {
        std::cerr << "[AI Bridge] supervisor: CreateProcessAsUserW failed (" << err << ").\n";
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    std::cerr << "[AI Bridge] supervisor: relaunched desktop agent in session "
              << session_id << ".\n";
    return true;
}

// F08-02: control-plane heartbeat (POST /api/agent/heartbeat, the
// same endpoint the Python agent polls). Best-effort: any failure is
// logged and the next heartbeat is still attempted on schedule.
bool PostHeartbeat(const aibridge::monitoring::SystemHealthSnapshot& snap) {
    if (g_control_url.empty() || g_device_token.empty()) return false;
    auto now = std::chrono::steady_clock::now();
    double seen_ago = std::chrono::duration<double>(now - g_last_desktop_contact).count();

    std::ostringstream body;
    body << "{\"hostname\":\"" << JsonEscape(snap.hostname) << "\","
         << "\"os\":\"Windows\","
         << "\"metrics\":{\"cpu_percent\":" << snap.cpu_percent
         << ",\"memory_percent\":" << snap.memory_percent
         << ",\"active_session_id\":" << snap.active_session_id
         << ",\"desktop_agent_seen_seconds_ago\":" << static_cast<long long>(seen_ago)
         << ",\"supervisor\":\"ai_bridge_service\"}}";

    std::string url = g_control_url;
    while (!url.empty() && url.back() == '/') url.pop_back();
    url += "/api/agent/heartbeat";
    std::wstring wurl = ToWide(url);

    URL_COMPONENTSW uc{};
    uc.dwStructSize = sizeof(uc);
    wchar_t host[256] = {0};
    wchar_t path[1024] = {0};
    uc.lpszHostName = host;
    uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path;
    uc.dwUrlPathLength = 1024;
    if (!WinHttpCrackUrl(wurl.c_str(), 0, 0, &uc)) {
        std::cerr << "[AI Bridge] supervisor: heartbeat URL parse failed.\n";
        return false;
    }
    HINTERNET session = WinHttpOpen(L"AIBridgeService/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) return false;
    HINTERNET connect = WinHttpConnect(session, host, uc.nPort, 0);
    if (!connect) {
        WinHttpCloseHandle(session);
        return false;
    }
    DWORD flags = (uc.nScheme == INTERNET_SCHEME_HTTPS) ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET request = WinHttpOpenRequest(connect, L"POST", path, nullptr,
                                           WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags);
    if (!request) {
        WinHttpCloseHandle(connect);
        WinHttpCloseHandle(session);
        return false;
    }
    std::wstring headers = L"Content-Type: application/json\r\nX-Device-Token: " + ToWide(g_device_token);
    std::string body_str = body.str();
    BOOL ok = WinHttpSendRequest(request, headers.c_str(), static_cast<DWORD>(-1),
                                 (LPVOID)body_str.data(), static_cast<DWORD>(body_str.size()),
                                 static_cast<DWORD>(body_str.size()), 0);
    if (ok) ok = WinHttpReceiveResponse(request, nullptr);
    DWORD status = 0;
    if (ok) {
        DWORD len = sizeof(status);
        ok = WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                 WINHTTP_HEADER_NAME_BY_INDEX, &status, &len, WINHTTP_NO_HEADER_INDEX);
    }
    WinHttpCloseHandle(request);
    WinHttpCloseHandle(connect);
    WinHttpCloseHandle(session);
    if (!ok || status != 200) {
        std::cerr << "[AI Bridge] supervisor: heartbeat failed (HTTP "
                  << status << ").\n";
        return false;
    }
    return true;
}

void DoPeriodicWork(aibridge::monitoring::WindowsSystemMonitor& monitor) {
    auto now = std::chrono::steady_clock::now();
    // Control-plane heartbeat every 30 s (only when configured).
    if (!g_control_url.empty() && !g_device_token.empty()
        && now - g_last_heartbeat >= kHeartbeatInterval) {
        auto snap = monitor.CollectSnapshot();
        // Attempted either way: do not hammer a down plane — the next
        // attempt is a full interval away whether this one worked or not.
        PostHeartbeat(snap);
        g_last_heartbeat = now;
    }
    // Desktop-agent liveness: warn, then relaunch, on cooldowns.
    if (now - g_last_desktop_contact > kDesktopStaleAfter) {
        if (now - g_last_stale_warning >= kStaleWarningInterval) {
            g_last_stale_warning = now;
            std::cerr << "[AI Bridge] supervisor: desktop agent silent for "
                      << std::chrono::duration_cast<std::chrono::seconds>(
                             now - g_last_desktop_contact).count()
                      << " s; will relaunch when possible.\n";
        }
        if (now - g_last_relaunch_attempt >= kRelaunchCooldown) {
            g_last_relaunch_attempt = now;
            if (LaunchDesktopAgentInActiveSession()) {
                // Give the fresh agent a full grace period before the
                // next staleness verdict.
                g_last_desktop_contact = std::chrono::steady_clock::now();
            }
        }
    }
}

std::string SupervisorStatusJson(aibridge::monitoring::WindowsSystemMonitor& monitor) {
    auto snap = monitor.CollectSnapshot();
    auto perms = g_input_controller.GetPermissions();
    auto wins = aibridge::mcp::DesktopMcpExecutor::GetVisibleWindows();
    std::ostringstream ss;
    ss << "{\"status\":\"ok\",\"engine\":\"cpp_native_v1\",\"hostname\":\"" << JsonEscape(snap.hostname)
       << "\",\"active_user\":\"" << JsonEscape(snap.active_user)
       << "\",\"active_session_id\":" << snap.active_session_id
       << ",\"cpu_percent\":" << snap.cpu_percent
       << ",\"memory_percent\":" << snap.memory_percent
       << ",\"disk_percent\":" << snap.disk_percent
       << ",\"visible_windows\":" << wins.size()
       << ",\"held_keys\":" << g_input_controller.GetHeldKeyCount()
       << ",\"held_mouse_buttons\":" << g_input_controller.GetHeldMouseButtonCount()
       << ",\"permissions\":{\"allow_mouse\":" << (perms.allow_mouse ? "true" : "false")
       << ",\"allow_keyboard\":" << (perms.allow_keyboard ? "true" : "false")
       << ",\"allow_clipboard\":" << (perms.allow_clipboard ? "true" : "false")
       << ",\"session_active\":" << (perms.session_active ? "true" : "false") << "}}";
    return ss.str();
}

// Handle every message on one connected desktop-agent pipe until the
// client disconnects (F08-02: the old code handled one message per
// 1 s iteration). Returns when the client goes away or errors.
void HandleDesktopClient(HANDLE hPipe, aibridge::monitoring::WindowsSystemMonitor& monitor, const std::string& pipe_secret) {
    for (;;) {
        std::string msg_type, payload;
        if (!aibridge::security::SecureNamedPipe::ReadAuthenticatedMessage(hPipe, msg_type, payload, pipe_secret)) {
            return;  // client disconnected or sent a bad message
        }
        g_last_desktop_contact = std::chrono::steady_clock::now();
        if (msg_type == "REGISTER_DESKTOP_AGENT") {
            auto snap = monitor.CollectSnapshot();
            std::string reply = "{\"status\":\"registered\",\"active_session_id\":" +
                                std::to_string(snap.active_session_id) + ",\"hostname\":\"" + JsonEscape(snap.hostname) + "\"}";
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(hPipe, "REGISTERED", reply, pipe_secret);
        } else if (msg_type == "HEARTBEAT_PING") {
            auto snap = monitor.CollectSnapshot();
            std::string reply = "{\"status\":\"ok\",\"engine\":\"cpp_native_v1\",\"active_session_id\":" +
                                std::to_string(snap.active_session_id) + ",\"hostname\":\"" + JsonEscape(snap.hostname) +
                                "\",\"cpu_percent\":" + std::to_string(snap.cpu_percent) +
                                ",\"memory_percent\":" + std::to_string(snap.memory_percent) + "}";
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(hPipe, "HEARTBEAT_PONG", reply, pipe_secret);
        } else if (msg_type == "STATUS_QUERY" || msg_type == "DESKTOP_STATUS_REPORT") {
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                hPipe, "STATUS_RESPONSE", SupervisorStatusJson(monitor), pipe_secret);
        } else if (msg_type == "UPDATE_SESSION_PERMISSIONS") {
            bool allow_desktop = ExtractJsonBoolField(payload, "allow_desktop_control", false);
            bool allow_clip = ExtractJsonBoolField(payload, "allow_clipboard", false);
            aibridge::input::SessionInputPermissions perms{};
            perms.allow_mouse = allow_desktop;
            perms.allow_keyboard = allow_desktop;
            perms.allow_clipboard = allow_clip;
            perms.session_active = allow_desktop || allow_clip;
            g_input_controller.SetPermissions(perms);
            std::ostringstream reply;
            reply << "{\"status\":\"permissions_updated\",\"allow_mouse\":" << (perms.allow_mouse ? "true" : "false")
                  << ",\"allow_keyboard\":" << (perms.allow_keyboard ? "true" : "false")
                  << ",\"allow_clipboard\":" << (perms.allow_clipboard ? "true" : "false")
                  << ",\"session_active\":" << (perms.session_active ? "true" : "false") << "}";
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                hPipe, "PERMISSIONS_UPDATED", reply.str(), pipe_secret);
        } else if (msg_type == "RELEASE_ALL_INPUT") {
            int released = g_input_controller.ReleaseAllHeldKeys();
            std::string reply = "{\"status\":\"released\",\"released_count\":" + std::to_string(released) + "}";
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                hPipe, "INPUT_RELEASED", reply, pipe_secret);
        } else {
            aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                hPipe, "ERROR", "{\"error\":\"unknown_msg_type\",\"type\":\"" + JsonEscape(msg_type) + "\"}", pipe_secret
            );
        }
    }
}

// Wait for a desktop agent to connect, honouring the stop event and
// running periodic work (heartbeat / liveness) while waiting. The
// pipe is created by the caller and left connected on success.
bool WaitForDesktopClient(HANDLE hPipe, HANDLE stop_event) {
    OVERLAPPED ov{};
    ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!ov.hEvent) return false;
    BOOL connected = ConnectNamedPipe(hPipe, &ov);
    bool result = false;
    if (connected || GetLastError() == ERROR_PIPE_CONNECTED) {
        result = true;
    } else if (GetLastError() == ERROR_IO_PENDING) {
        HANDLE waits[2] = {ov.hEvent, stop_event};
        DWORD w = WaitForMultipleObjects(2, waits, FALSE, 500);
        if (w == WAIT_OBJECT_0) {
            DWORD transferred = 0;
            result = GetOverlappedResult(hPipe, &ov, &transferred, FALSE) != FALSE;
        } else if (w == WAIT_OBJECT_0 + 1) {
            CancelIo(hPipe);
            result = false;
        } else {
            // Timeout slice: the caller re-waits (and runs periodic
            // work between slices). Cancel the pending connect and
            // wait for it to finish before the pipe is reused — a
            // late completion would otherwise race the next connect.
            // (A client that connected in the meantime still reports
            // success from GetOverlappedResult and is served.)
            CancelIo(hPipe);
            WaitForSingleObject(ov.hEvent, INFINITE);
            DWORD transferred = 0;
            result = GetOverlappedResult(hPipe, &ov, &transferred, FALSE) != FALSE;
        }
    } else {
        // ConnectNamedPipe failed immediately (not pending, not already
        // connected): do not hot-spin the outer loop on a broken pipe.
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    CloseHandle(ov.hEvent);
    return result;
}

// F08-02: the supervisor main loop. One persistent pipe: wait for the
// desktop agent, serve the whole connection, then wait for the next.
// Between connect attempts the loop runs periodic work (control-plane
// heartbeat, desktop-agent liveness + relaunch) instead of recreating
// the pipe every second.
void RunSupervisorLoop(aibridge::monitoring::WindowsSystemMonitor& monitor, const std::string& pipe_secret) {
    if (pipe_secret.empty()) {
        // No pipe secret: nothing to supervise over the pipe, but the
        // control-plane heartbeat still runs.
        while (WaitForSingleObject(g_stop_event, 500) == WAIT_TIMEOUT) {
            DoPeriodicWork(monitor);
        }
        return;
    }
    for (;;) {
        HANDLE hPipe = aibridge::security::SecureNamedPipe::CreateRestrictedServerPipe(g_pipe_name_w.c_str());
        if (hPipe == INVALID_HANDLE_VALUE) {
            if (WaitForSingleObject(g_stop_event, 1000) != WAIT_TIMEOUT) return;
            continue;
        }
        bool stop = false;
        while (!stop) {
            if (WaitForSingleObject(g_stop_event, 0) == WAIT_OBJECT_0) {
                stop = true;
                break;
            }
            if (WaitForDesktopClient(hPipe, g_stop_event)) {
                HandleDesktopClient(hPipe, monitor, pipe_secret);
                DisconnectNamedPipe(hPipe);
            }
            DoPeriodicWork(monitor);
            if (WaitForSingleObject(g_stop_event, 0) == WAIT_OBJECT_0) stop = true;
        }
        CloseHandle(hPipe);
        if (stop) return;
    }
}

// Single accept cycle for the interactive (non-service) fallback in
// main(): wait up to ~2 s for a desktop agent, serve it, return.
void RunSupervisorOnce(aibridge::monitoring::WindowsSystemMonitor& monitor, const std::string& pipe_secret) {
    if (pipe_secret.empty()) return;
    HANDLE hPipe = aibridge::security::SecureNamedPipe::CreateRestrictedServerPipe(g_pipe_name_w.c_str());
    if (hPipe == INVALID_HANDLE_VALUE) return;
    if (WaitForDesktopClient(hPipe, g_stop_event)) {
        HandleDesktopClient(hPipe, monitor, pipe_secret);
        DisconnectNamedPipe(hPipe);
    }
    CloseHandle(hPipe);
}

void WINAPI ServiceCtrlHandler(DWORD ctrl_code) {
    if (ctrl_code == SERVICE_CONTROL_STOP && g_status_handle) {
        g_service_status.dwCurrentState = SERVICE_STOP_PENDING;
        SetServiceStatus(g_status_handle, &g_service_status);
        if (g_stop_event != INVALID_HANDLE_VALUE) {
            SetEvent(g_stop_event);
        }
    }
}

void WINAPI ServiceMain(DWORD /*argc*/, LPWSTR* /*argv*/) {
    g_status_handle = RegisterServiceCtrlHandlerW(g_service_name_w.c_str(), ServiceCtrlHandler);
    if (!g_status_handle) return;

    g_service_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    g_service_status.dwControlsAccepted = SERVICE_ACCEPT_STOP;
    g_service_status.dwCurrentState = SERVICE_RUNNING;
    SetServiceStatus(g_status_handle, &g_service_status);

    g_stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);

    // F08-11: one monitor for the life of the service. Constructing
    // it here baselines the CPU tick counts once; every later
    // CollectSnapshot() is a real delta.
    aibridge::monitoring::WindowsSystemMonitor monitor;
    RunSupervisorLoop(monitor, g_pipe_secret);

    CloseHandle(g_stop_event);
    g_service_status.dwCurrentState = SERVICE_STOPPED;
    SetServiceStatus(g_status_handle, &g_service_status);
}
} // namespace

int main(int argc, char* argv[]) {
    bool console_once = false;
    bool pipe_server_mode = false;
    std::string config_path;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--once" || arg == "--console" || arg == "--console-once" || arg == "--status") {
            console_once = true;
        } else if (arg == "--pipe-server" || arg == "--daemon") {
            pipe_server_mode = true;
        } else if (arg == "--config" && i + 1 < argc) {
            config_path = argv[++i];
        } else if (arg == "--pipe-name" && i + 1 < argc) {
            std::string pn = argv[++i];
            if (pn.rfind("\\\\.\\pipe\\", 0) != 0) pn = "\\\\.\\pipe\\" + pn;
            g_pipe_name_w = ToWide(pn);
        } else if (arg == "--service-name" && i + 1 < argc) {
            g_service_name_w = ToWide(argv[++i]);
        }
    }
    if (!LoadServiceConfig(config_path)) {
        std::cerr << "[AI Bridge] FATAL: service configuration could not be loaded; exiting.\n";
        return 1;
    }

    if (console_once) {
        aibridge::monitoring::WindowsSystemMonitor mon;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        auto snap = mon.CollectSnapshot();
        std::cout << "{\"process\":\"ai_bridge_service\",\"role\":\"session0_supervisor\","
                  << "\"hostname\":\"" << snap.hostname << "\","
                  << "\"active_user\":\"" << snap.active_user << "\","
                  << "\"active_session_id\":" << snap.active_session_id << ","
                  << "\"cpu_percent\":" << snap.cpu_percent << ","
                  << "\"memory_percent\":" << snap.memory_percent << ","
                  << "\"disk_percent\":" << snap.disk_percent << "}\n";
        return 0;
    }

    // F08-12: single-instance guard for the service process.
    {
        HANDLE mutex_handle = CreateMutexW(nullptr, FALSE, L"Global\\AIBridgeAgentService");
        if (mutex_handle && GetLastError() == ERROR_ALREADY_EXISTS) {
            std::cerr << "[AI Bridge] FATAL: another ai_bridge_service instance is already running; exiting.\n";
            CloseHandle(mutex_handle);
            return 1;
        }
        if (mutex_handle) {
            static HANDLE g_service_mutex = mutex_handle;
            (void)g_service_mutex;
        }
    }

    if (pipe_server_mode) {
        if (g_pipe_secret.empty()) {
            g_pipe_secret = "aibridge-local-pipe-hmac-v1";
        }
        if (g_stop_event == INVALID_HANDLE_VALUE) {
            g_stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        }
        aibridge::monitoring::WindowsSystemMonitor mon;
        RunSupervisorLoop(mon, g_pipe_secret);
        return 0;
    }

    std::wstring svc_buf = g_service_name_w;
    SERVICE_TABLE_ENTRYW service_table[] = {
        {svc_buf.data(), ServiceMain},
        {nullptr, nullptr}
    };
    if (!StartServiceCtrlDispatcherW(service_table)) {
        // Fallback when launched interactively from terminal without --once.
        // The stop event must exist: WaitForDesktopClient waits on it.
        if (g_stop_event == INVALID_HANDLE_VALUE) {
            g_stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        }
        aibridge::monitoring::WindowsSystemMonitor mon;
        RunSupervisorOnce(mon, g_pipe_secret);
    }
    return 0;
}
