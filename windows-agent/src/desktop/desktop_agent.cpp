/**
 * AI Bridge Interactive Desktop Agent (Process B - User Session)
 *
 * Modes:
 *  1) Probe (default): one DXGI+MF capture/encode sample → JSON stdout.
 *  2) Session: --control-url --device-token --session-id → C++ PeerConnection
 *     signaling against /api/agent/webrtc/* (requires AIBRIDGE_WITH_LIBDATACHANNEL
 *     or AIBRIDGE_WITH_LIBWEBRTC at build time).
 */

#include "../webrtc/webrtc_transport.hpp"
#include "../webrtc/peer_connection.hpp"
#include "../mcp/mcp_executor.hpp"
#include "../security/local_security.hpp"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <atomic>
#include <cstdlib>
#include <csignal>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace {
std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop.store(true); }

std::string ExtractJsonField(const std::string& json, const std::string& key) {
    // F08-15: hand-rolled JSON extraction — see the full justification
    // in windows-agent/src/webrtc/http_signaling.cpp (no JSON library
    // is vendored in this tree; this extracts a fixed set of string
    // fields from the agent's own config file and must not grow into
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
            out.push_back(json[pos++]);
            continue;
        }
        if (c == '"') break;
        out.push_back(c);
    }
    return out;
}

struct SupervisorRegistration {
    bool confirmed = false;
    std::string status;
    uint32_t active_session_id = 0;
    std::string hostname;
};

std::string ExtractJsonUintField(const std::string& json, const std::string& key) {
    // Numeric (unquoted) JSON field extraction for the supervisor reply
    // (active_session_id); string fields use ExtractJsonField above.
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return {};
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) ++pos;
    size_t start = pos;
    while (pos < json.size() && json[pos] >= '0' && json[pos] <= '9') ++pos;
    return json.substr(start, pos - start);
}

// F08-04 (Wave 5A): the old code sent REGISTER_DESKTOP_AGENT and
// closed the pipe without reading the reply, discarding the
// supervisor's verdict, active_session_id, and hostname. It now reads
// the authenticated reply, verifies status == "registered", and
// returns the values to main(), which reports them in its JSON output.
// A missing or rejected reply is NOT fatal (the agent can still run
// a WebRTC session), but it is logged loudly so a supervisor that
// never confirms registration is visible instead of silent.
SupervisorRegistration RegisterWithSupervisorPipe(const std::string& pipe_name_utf8, const std::string& pipe_secret) {
    SupervisorRegistration result;
    if (pipe_secret.empty()) return result;
    std::wstring pipe_w = L"\\\\.\\pipe\\AIBridgeDesktopPipe";
    if (!pipe_name_utf8.empty()) {
        int needed = MultiByteToWideChar(CP_UTF8, 0, pipe_name_utf8.data(), static_cast<int>(pipe_name_utf8.size()), nullptr, 0);
        if (needed > 0) {
            pipe_w.assign(needed, L'\0');
            MultiByteToWideChar(CP_UTF8, 0, pipe_name_utf8.data(), static_cast<int>(pipe_name_utf8.size()), pipe_w.data(), needed);
        }
    }
    HANDLE hPipe = aibridge::security::SecureNamedPipe::ConnectClientPipe(pipe_w.c_str(), 1000);
    if (hPipe != INVALID_HANDLE_VALUE) {
        DWORD sess_id = 0;
        ProcessIdToSessionId(GetCurrentProcessId(), &sess_id);
        std::string payload = "{\"session\":" + std::to_string(sess_id) + "}";
        if (aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                hPipe, "REGISTER_DESKTOP_AGENT", payload, pipe_secret)) {
            std::string reply_type, reply_payload;
            if (aibridge::security::SecureNamedPipe::ReadAuthenticatedMessage(
                    hPipe, reply_type, reply_payload, pipe_secret)) {
                result.status = ExtractJsonField(reply_payload, "status");
                std::string sess_text = ExtractJsonUintField(reply_payload, "active_session_id");
                if (!sess_text.empty()) {
                    result.active_session_id = static_cast<uint32_t>(std::stoul(sess_text));
                }
                result.hostname = ExtractJsonField(reply_payload, "hostname");
                result.confirmed = (reply_type == "REGISTERED" && result.status == "registered");
            }
        }
        CloseHandle(hPipe);
    }
    return result;
}
}

int main(int argc, char* argv[]) {
    int monitor_index = 0;
    std::string control_url;
    std::string device_token;
    std::string session_id;
    std::string config_path;
    std::string pipe_name;
    std::string pipe_secret;
    // F13-05: TURN, config-driven (see PeerConnectionConfig). Empty
    // means STUN-only (old behaviour).
    std::string turn_url;
    std::string turn_username;
    std::string turn_credential;
    int fps = 30;
    int bitrate_kbps = 4500;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--monitor" && i + 1 < argc) {
            monitor_index = std::atoi(argv[++i]);
        } else if (arg == "--control-url" && i + 1 < argc) {
            control_url = argv[++i];
        } else if (arg == "--device-token" && i + 1 < argc) {
            device_token = argv[++i];
        } else if (arg == "--session-id" && i + 1 < argc) {
            session_id = argv[++i];
        } else if (arg == "--config" && i + 1 < argc) {
            config_path = argv[++i];
        } else if (arg == "--fps" && i + 1 < argc) {
            fps = std::atoi(argv[++i]);
        } else if (arg == "--bitrate-kbps" && i + 1 < argc) {
            bitrate_kbps = std::atoi(argv[++i]);
        } else if (arg == "--help") {
            std::cout << "Usage:\n  ai_bridge_desktop [--monitor N] [--config PATH]\n"
                      << "  ai_bridge_desktop --control-url URL --device-token TOK --session-id ID\n";
            return 0;
        }
    }

    bool allow_mouse = false;
    bool allow_keyboard = false;
    bool allow_clipboard = false;
    if (!config_path.empty()) {
        std::ifstream ifs(config_path);
        if (ifs) {
            std::ostringstream ss;
            ss << ifs.rdbuf();
            std::string body = ss.str();
            if (control_url.empty()) control_url = ExtractJsonField(body, "control_url");
            if (device_token.empty()) device_token = ExtractJsonField(body, "device_token");
            pipe_name = ExtractJsonField(body, "pipe_name");
            pipe_secret = ExtractJsonField(body, "pipe_secret");
            if (turn_url.empty()) turn_url = ExtractJsonField(body, "turn_url");
            if (turn_username.empty()) turn_username = ExtractJsonField(body, "turn_username");
            if (turn_credential.empty()) turn_credential = ExtractJsonField(body, "turn_credential");
            allow_mouse = body.find("\"allow_desktop_control\":true") != std::string::npos ||
                          body.find("\"allow_desktop_control\": true") != std::string::npos;
            allow_keyboard = allow_mouse;
            allow_clipboard = body.find("\"allow_clipboard\":true") != std::string::npos ||
                              body.find("\"allow_clipboard\": true") != std::string::npos;
        }
    }
    // F13-05: environment fallback for TURN (same names as the Python
    // path: AI_BRIDGE_TURN_URL / AI_BRIDGE_TURN_USERNAME /
    // AI_BRIDGE_TURN_CREDENTIAL). Config file wins over environment.
    if (turn_url.empty()) {
        const char* env_turn = std::getenv("AI_BRIDGE_TURN_URL");
        if (env_turn && *env_turn) turn_url = env_turn;
    }
    if (turn_username.empty()) {
        const char* env_user = std::getenv("AI_BRIDGE_TURN_USERNAME");
        if (env_user && *env_user) turn_username = env_user;
    }
    if (turn_credential.empty()) {
        const char* env_cred = std::getenv("AI_BRIDGE_TURN_CREDENTIAL");
        if (env_cred && *env_cred) turn_credential = env_cred;
    }
    // F08-08 follow-up: the installer writes DPAPI-sealed secrets
    // ("aib-dpapi-v1:<base64>") into the config. Unseal before use; fail
    // loudly if unsealing fails — never treat the sealed value as the
    // secret. (A --device-token passed on the CLI is unsealed too if it
    // carries the prefix; a plaintext token passes through unchanged.)
    {
        std::string unsealed;
        if (!aibridge::security::DpapiSecretStore::UnsealIfSealed(device_token, unsealed)) {
            std::cerr << "[AI Bridge] FATAL: could not DPAPI-unseal device_token; refusing to run.\n";
            return 1;
        }
        device_token = unsealed;
        if (!aibridge::security::DpapiSecretStore::UnsealIfSealed(pipe_secret, unsealed)) {
            std::cerr << "[AI Bridge] FATAL: could not DPAPI-unseal pipe_secret; refusing to run.\n";
            return 1;
        }
        pipe_secret = unsealed;
    }
    SupervisorRegistration supervisor_reg = RegisterWithSupervisorPipe(pipe_name, pipe_secret);
    // F08-04: the supervisor's verdict is reported, not discarded.
    // Registration is not fatal when unconfirmed (no supervisor
    // installed, or the pipe was unreachable), but it is loud.
    if (!pipe_secret.empty() && !supervisor_reg.confirmed) {
        std::cerr << "[AI Bridge] WARNING: supervisor registration not confirmed"
                  << " (status='" << supervisor_reg.status << "'); continuing unsupervised.\n";
    }

    // F08-12: single-instance guard. The installer used to launch this
    // agent twice (HKLM Run + scheduled task) with no mutex or lock
    // file, so two agents could capture, encode and inject input in
    // the same session. One named mutex per interactive session: the
    // second launch in the same session exits (code 3) instead of
    // duplicating the agent. The mutex is held for the life of the
    // process (g_instance_mutex is never closed before exit).
    {
        DWORD sess_id = 0;
        ProcessIdToSessionId(GetCurrentProcessId(), &sess_id);
        std::wstring mutex_name = L"Global\\AIBridgeDesktopAgent-Session-" + std::to_wstring(sess_id);
        HANDLE mutex_handle = CreateMutexW(nullptr, FALSE, mutex_name.c_str());
        if (mutex_handle && GetLastError() == ERROR_ALREADY_EXISTS) {
            std::cout << "{\"ok\":false,\"error\":\"already_running\","
                      << "\"session_id\":" << sess_id << "}\n";
            CloseHandle(mutex_handle);
            return 3;
        }
        if (mutex_handle) {
            static HANDLE g_instance_mutex = mutex_handle;
            (void)g_instance_mutex;
        }
    }

    if (!control_url.empty() && !device_token.empty() && !session_id.empty()) {
        std::signal(SIGINT, on_signal);
        std::signal(SIGTERM, on_signal);
        aibridge::webrtc::PeerConnectionConfig cfg;
        cfg.control_url = control_url;
        cfg.device_token = device_token;
        cfg.session_id = session_id;
        cfg.monitor_index = monitor_index;
        cfg.fps = fps;
        cfg.bitrate_kbps = bitrate_kbps;
        cfg.turn_url = turn_url;
        cfg.turn_username = turn_username;
        cfg.turn_credential = turn_credential;
        cfg.allow_mouse = allow_mouse;
        cfg.allow_keyboard = allow_keyboard;
        cfg.allow_clipboard = allow_clipboard;

        auto backend = aibridge::webrtc::DescribeAvailablePeerBackend();
        auto session = aibridge::webrtc::CreatePeerConnectionSession(cfg);
        if (!session) {
            std::cout << "{\"ok\":false,\"error\":\"no_cpp_webrtc_backend\","
                      << "\"backend\":\"" << aibridge::webrtc::PeerBackendName(backend) << "\","
                      << "\"hint\":\"Build with -DAIBRIDGE_WITH_LIBDATACHANNEL=ON (or libwebrtc root)\"}\n";
            return 2;
        }
        std::string err;
        // Auto-stop after a short E2E window unless interrupted — CI harness sets stop via timeout kill.
        bool ok = session->RunUntilStopped(&g_stop, &err);
        std::cout << "{\"ok\":" << (ok ? "true" : "false")
                  << ",\"supervisor_registered\":" << (supervisor_reg.confirmed ? "true" : "false")
                  << ",\"supervisor_session_id\":" << supervisor_reg.active_session_id
                  << ",\"backend\":\"" << aibridge::webrtc::PeerBackendName(session->Backend()) << "\""
                  << ",\"ice_state\":\"" << session->IceState() << "\""
                  << ",\"frames_sent\":" << session->FramesSent()
                  << ",\"error\":\"" << err << "\"}\n";
        return ok ? 0 : 1;
    }

    aibridge::webrtc::NativeWebRtcPipeline pipeline;
    // F08-16: StartSession() is honest now — false when the encoder
    // fails to initialise. The probe reports session_started and its
    // error instead of claiming a streaming session that cannot encode.
    bool session_started = pipeline.StartSession("desktop_probe_session", monitor_index, 1920, 1080, 30, 4500);
    auto probe_stats = pipeline.GetStats();

    aibridge::capture::CapturedFrame raw_frame;
    aibridge::encoder::EncodedAccessUnit encoded_au;
    bool ok = pipeline.StepCaptureAndEncode(raw_frame, encoded_au);
    auto stats = pipeline.GetStats();
    auto windows = aibridge::mcp::DesktopMcpExecutor::GetVisibleWindows();
    auto backend = aibridge::webrtc::DescribeAvailablePeerBackend();

    std::cout << "{\"process\":\"ai_bridge_desktop\",\"role\":\"interactive_desktop_agent\","
              << "\"supervisor_registered\":" << (supervisor_reg.confirmed ? "true" : "false") << ","
              << "\"supervisor_session_id\":" << supervisor_reg.active_session_id << ","
              << "\"session_started\":" << (session_started ? "true" : "false") << ","
              << "\"session_error\":\"" << probe_stats.error << "\","
              << "\"capture_ok\":" << (ok ? "true" : "false") << ","
              << "\"from_dxgi\":" << (raw_frame.from_dxgi ? "true" : "false") << ","
              << "\"width\":" << stats.width << ","
              << "\"height\":" << stats.height << ","
              << "\"codec\":\"" << stats.codec << "\","
              << "\"encoder_backend\":\"" << stats.encoder_backend << "\","
              << "\"hardware_encoding\":" << (stats.hardware_encoding ? "true" : "false") << ","
              << "\"peer_backend\":\"" << aibridge::webrtc::PeerBackendName(backend) << "\","
              << "\"capture_time_ms\":" << stats.capture_time_ms << ","
              << "\"encode_time_ms\":" << stats.encode_time_ms << ","
              << "\"encoded_frame_bytes\":" << stats.encoded_frame_bytes << ","
              << "\"visible_windows\":" << windows.size() << "}\n";

    pipeline.StopSession();
    return ok ? 0 : 1;
}
