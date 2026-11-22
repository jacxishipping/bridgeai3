#pragma once

#include <optional>
#include <string>
#include <vector>

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>

namespace aibridge {
namespace webrtc {

struct IceCandidateJson {
    std::string candidate;
    std::string sdp_mid;
    int sdp_mline_index = 0;
};

struct SessionSnapshot {
    std::string status;
    std::string error;
    std::string answer_sdp;
    std::string answer_type;
    std::vector<IceCandidateJson> viewer_candidates;
    std::string mode;
    bool human_takeover_active = false;
    bool control_mouse = false;
    bool control_keyboard = false;
    bool permissions_present = false;
};

// WinHTTP client for /api/agent/webrtc/* (same contract as Python HttpSignaling).
//
// F13-05: ONE persistent WinHTTP session + connection, reused for every
// request (created lazily by EnsureConnected, closed by Close() / the
// destructor). The old code opened a fresh WinHTTP session, connection
// (and, for HTTPS, a fresh TLS handshake) for EVERY request — and the
// peer loop polled once per video frame, so signalling cost a TLS
// handshake per frame. Requests still open/close one WinHttp request
// handle each; the expensive session/connect setup is amortised.
class HttpSignalingClient {
public:
    HttpSignalingClient(std::string control_url, std::string device_token, std::string session_id);
    ~HttpSignalingClient();
    HttpSignalingClient(const HttpSignalingClient&) = delete;
    HttpSignalingClient& operator=(const HttpSignalingClient&) = delete;

    bool PostOffer(const std::string& sdp, const std::string& type, std::string* error_out = nullptr);
    bool PostCandidate(const IceCandidateJson& candidate, std::string* error_out = nullptr);
    bool PostError(const std::string& message);
    std::optional<SessionSnapshot> PollSession(std::string* error_out = nullptr);
    void Close();

private:
    std::string control_url_;
    std::string device_token_;
    std::string session_id_;

    // Persistent WinHTTP handles (F13-05). Null until EnsureConnected().
    HINTERNET session_handle_ = nullptr;
    HINTERNET connect_handle_ = nullptr;
    bool connect_https_ = false;
    std::wstring connect_host_;
    INTERNET_PORT connect_port_ = 0;
    std::wstring connect_path_prefix_;

    bool EnsureConnected(std::string* error_out);
    void DropConnection();

    bool RequestJson(
        const std::string& method,
        const std::string& path,
        const std::string& body_json,
        std::string* response_body,
        std::string* error_out
    );
};

} // namespace webrtc
} // namespace aibridge
