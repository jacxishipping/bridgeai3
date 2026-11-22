#include "http_signaling.hpp"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>

#include <sstream>

#pragma comment(lib, "winhttp.lib")

namespace aibridge {
namespace webrtc {
namespace {

std::wstring Widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring out(n > 0 ? n - 1 : 0, L'\0');
    if (n > 1) MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, out.data(), n);
    return out;
}

std::string Narrow(const std::wstring& s) {
    if (s.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, s.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(n > 0 ? n - 1 : 0, '\0');
    if (n > 1) WideCharToMultiByte(CP_UTF8, 0, s.c_str(), -1, out.data(), n, nullptr, nullptr);
    return out;
}

bool ParseUrl(const std::string& url, bool* https, std::wstring* host, INTERNET_PORT* port, std::wstring* path) {
    URL_COMPONENTS uc{};
    uc.dwStructSize = sizeof(uc);
    wchar_t host_buf[256]{};
    wchar_t path_buf[1024]{};
    uc.lpszHostName = host_buf;
    uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path_buf;
    uc.dwUrlPathLength = 1024;
    std::wstring wurl = Widen(url);
    if (!WinHttpCrackUrl(wurl.c_str(), 0, 0, &uc)) return false;
    *https = uc.nScheme == INTERNET_SCHEME_HTTPS;
    *host = host_buf;
    *port = uc.nPort;
    *path = path_buf;
    return true;
}

std::string JsonGetString(const std::string& json, const std::string& key) {
    // Locate `"key": "..."` and unescape JSON string escapes so SDP newlines
    // (a=ice-ufrag / a=ice-pwd) survive into libdatachannel.
    const std::string needle = "\"" + key + "\"";
    size_t search = 0;
    while (search < json.size()) {
        auto pos = json.find(needle, search);
        if (pos == std::string::npos) return "";
        size_t after = pos + needle.size();
        while (after < json.size() && (json[after] == ' ' || json[after] == '\t')) ++after;
        if (after >= json.size() || json[after] != ':') {
            search = pos + 1;
            continue;
        }
        ++after;
        while (after < json.size() && (json[after] == ' ' || json[after] == '\t' || json[after] == '\n' || json[after] == '\r')) {
            ++after;
        }
        if (after >= json.size() || json[after] != '"') return "";
        ++after;
        std::string out;
        out.reserve(256);
        while (after < json.size()) {
            char c = json[after++];
            if (c == '"') return out;
            if (c == '\\' && after < json.size()) {
                char e = json[after++];
                switch (e) {
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'u': {
                        if (after + 4 > json.size()) return out;
                        int cp = 0;
                        bool ok = true;
                        for (int i = 0; i < 4; ++i) {
                            char h = json[after++];
                            int n = -1;
                            if (h >= '0' && h <= '9') n = h - '0';
                            else if (h >= 'a' && h <= 'f') n = h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') n = h - 'A' + 10;
                            if (n < 0) { ok = false; break; }
                            cp = (cp << 4) | n;
                        }
                        if (!ok) return out;
                        if (cp <= 0x7F) out.push_back(static_cast<char>(cp));
                        else if (cp <= 0x7FF) {
                            out.push_back(static_cast<char>(0xC0 | ((cp >> 6) & 0x1F)));
                            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                        } else {
                            out.push_back(static_cast<char>(0xE0 | ((cp >> 12) & 0x0F)));
                            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
                            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                        }
                        break;
                    }
                    default: out.push_back(e); break;
                }
            } else {
                out.push_back(c);
            }
        }
        return out;
    }
    return "";
}

int JsonGetInt(const std::string& json, const std::string& key, int fallback = 0) {
    const std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return fallback;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return fallback;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) ++pos;
    size_t start = pos;
    if (pos < json.size() && (json[pos] == '-' || json[pos] == '+')) ++pos;
    while (pos < json.size() && json[pos] >= '0' && json[pos] <= '9') ++pos;
    if (pos == start || (pos == start + 1 && (json[start] == '-' || json[start] == '+'))) return fallback;
    try {
        return std::stoi(json.substr(start, pos - start));
    } catch (...) {
        return fallback;
    }
}

// F13-05: parse the viewer's ICE candidates out of a session snapshot.
// The plane stores viewer candidates as a JSON array of flat objects:
//   "viewer_candidates":[{"candidate":"candidate:...","sdpMid":"0",
//   "sdpMLineIndex":0}, ...]
// Each entry may also be nested as {"candidate": {...}} (the shape the
// plane echoes back when the viewer posts {"candidate": {...}}); both
// shapes are handled. Entries with an empty candidate string are
// skipped. Malformed trailing entries stop the parse — a partial list
// is better than dropping the whole snapshot.
//
// WHY HAND-ROLLED JSON (F08-15): no JSON library is vendored anywhere
// in this tree, and adding a third-party dependency (e.g. nlohmann/json)
// in a wave that cannot compile on Windows would be unverifiable risk.
// These helpers are deliberately NOT a general JSON parser: they extract
// a fixed set of string/number fields from the plane's own well-formed
// responses, with escape-aware string handling (see JsonGetString).
// If a general parser is ever needed, vendor nlohmann/json as a single
// header and replace these helpers — do not grow them into a parser.
std::vector<IceCandidateJson> ParseViewerCandidates(const std::string& resp) {
    std::vector<IceCandidateJson> out;
    size_t key_pos = resp.find("\"viewer_candidates\"");
    if (key_pos == std::string::npos) return out;
    size_t arr = resp.find('[', key_pos);
    if (arr == std::string::npos) return out;
    size_t pos = arr + 1;
    while (pos < resp.size()) {
        size_t obj = resp.find('{', pos);
        size_t arr_end = resp.find(']', pos);
        if (obj == std::string::npos || (arr_end != std::string::npos && arr_end < obj)) break;
        // Find the matching close brace, honouring string escapes.
        int depth = 0;
        bool in_str = false;
        bool esc = false;
        size_t end = obj;
        for (; end < resp.size(); ++end) {
            char c = resp[end];
            if (in_str) {
                if (esc) esc = false;
                else if (c == '\\') esc = true;
                else if (c == '"') in_str = false;
            } else {
                if (c == '"') in_str = true;
                else if (c == '{') ++depth;
                else if (c == '}') {
                    --depth;
                    if (depth == 0) break;
                }
            }
        }
        if (end >= resp.size() || depth != 0) break;
        std::string entry = resp.substr(obj, end - obj + 1);
        IceCandidateJson cand;
        // Nested shape: {"candidate": {"candidate": "...", ...}}.
        size_t nested = entry.find("\"candidate\"");
        if (nested != std::string::npos) {
            size_t colon = entry.find(':', nested);
            size_t brace = colon == std::string::npos ? std::string::npos : entry.find('{', colon);
            size_t quote = colon == std::string::npos ? std::string::npos : entry.find('"', colon);
            if (brace != std::string::npos && (quote == std::string::npos || brace < quote)) {
                std::string inner = entry.substr(brace);
                cand.candidate = JsonGetString(inner, "candidate");
                cand.sdp_mid = JsonGetString(inner, "sdpMid");
                cand.sdp_mline_index = JsonGetInt(inner, "sdpMLineIndex", 0);
            }
        }
        if (cand.candidate.empty()) {
            // Flat shape: {"candidate": "candidate:...", "sdpMid": "0", ...}.
            cand.candidate = JsonGetString(entry, "candidate");
            cand.sdp_mid = JsonGetString(entry, "sdpMid");
            cand.sdp_mline_index = JsonGetInt(entry, "sdpMLineIndex", 0);
        }
        if (!cand.candidate.empty()) {
            out.push_back(cand);
        }
        pos = end + 1;
    }
    return out;
}

} // namespace

HttpSignalingClient::HttpSignalingClient(std::string control_url, std::string device_token, std::string session_id)
    : control_url_(std::move(control_url)),
      device_token_(std::move(device_token)),
      session_id_(std::move(session_id)) {
    while (!control_url_.empty() && (control_url_.back() == '/' || control_url_.back() == '\\')) {
        control_url_.pop_back();
    }
}

HttpSignalingClient::~HttpSignalingClient() {
    Close();
}

void HttpSignalingClient::Close() {
    DropConnection();
}

void HttpSignalingClient::DropConnection() {
    if (connect_handle_) {
        WinHttpCloseHandle(connect_handle_);
        connect_handle_ = nullptr;
    }
    if (session_handle_) {
        WinHttpCloseHandle(session_handle_);
        session_handle_ = nullptr;
    }
}

// F13-05: create the persistent WinHTTP session + connection once and
// reuse them. Parses control_url_ a single time and caches host, port,
// scheme and path prefix. A failed setup leaves both handles null so
// the next request retries; a failed request drops the connection so
// the next request re-establishes it (server restart / network change).
bool HttpSignalingClient::EnsureConnected(std::string* error_out) {
    if (session_handle_ && connect_handle_) return true;
    DropConnection();
    bool https = false;
    std::wstring host, url_path;
    INTERNET_PORT port = 0;
    if (!ParseUrl(control_url_, &https, &host, &port, &url_path)) {
        if (error_out) *error_out = "invalid control_url";
        return false;
    }
    HINTERNET session = WinHttpOpen(L"AIBridgeNativeAgent/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) {
        if (error_out) *error_out = "WinHttpOpen failed";
        return false;
    }
    HINTERNET connect = WinHttpConnect(session, host.c_str(), port, 0);
    if (!connect) {
        WinHttpCloseHandle(session);
        if (error_out) *error_out = "WinHttpConnect failed";
        return false;
    }
    session_handle_ = session;
    connect_handle_ = connect;
    connect_https_ = https;
    connect_host_ = host;
    connect_port_ = port;
    connect_path_prefix_ = url_path;
    return true;
}

bool HttpSignalingClient::RequestJson(
    const std::string& method,
    const std::string& path,
    const std::string& body_json,
    std::string* response_body,
    std::string* error_out
) {
    if (!EnsureConnected(error_out)) return false;
    std::wstring full_path = connect_path_prefix_;
    if (!full_path.empty() && full_path.back() == L'/') full_path.pop_back();
    full_path += Widen(path);

    DWORD flags = connect_https_ ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET request = WinHttpOpenRequest(connect_handle_, Widen(method).c_str(), full_path.c_str(),
                                           nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags);
    if (!request) {
        DropConnection();
        if (error_out) *error_out = "WinHttpOpenRequest failed";
        return false;
    }

    std::wstring headers = L"Content-Type: application/json\r\nX-Device-Token: " + Widen(device_token_);
    BOOL ok = FALSE;
    if (body_json.empty()) {
        ok = WinHttpSendRequest(request, headers.c_str(), static_cast<DWORD>(-1),
                                WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
    } else {
        ok = WinHttpSendRequest(
            request,
            headers.c_str(),
            static_cast<DWORD>(-1),
            (LPVOID)body_json.data(),
            static_cast<DWORD>(body_json.size()),
            static_cast<DWORD>(body_json.size()),
            0
        );
    }
    if (!ok || !WinHttpReceiveResponse(request, nullptr)) {
        WinHttpCloseHandle(request);
        // The persistent connection is suspect (server gone, TLS
        // failure); drop it so the next request re-establishes it.
        DropConnection();
        if (error_out) *error_out = "WinHttp send/receive failed";
        return false;
    }

    DWORD status = 0;
    DWORD status_size = sizeof(status);
    WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size, WINHTTP_NO_HEADER_INDEX);

    std::string body;
    for (;;) {
        DWORD avail = 0;
        if (!WinHttpQueryDataAvailable(request, &avail) || avail == 0) break;
        std::string chunk(avail, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(request, chunk.data(), avail, &read)) break;
        chunk.resize(read);
        body.append(chunk);
    }

    WinHttpCloseHandle(request);

    if (response_body) *response_body = body;
    if (status < 200 || status >= 300) {
        if (error_out) *error_out = "HTTP " + std::to_string(status) + ": " + body.substr(0, 200);
        return false;
    }
    return true;
}

bool HttpSignalingClient::PostOffer(const std::string& sdp, const std::string& type, std::string* error_out) {
    // Escape minimal JSON (SDP should not contain raw quotes often; still escape).
    auto esc = [](const std::string& in) {
        std::string out;
        out.reserve(in.size() + 8);
        for (char c : in) {
            if (c == '\\' || c == '"') out.push_back('\\');
            if (c == '\n') { out += "\\n"; continue; }
            if (c == '\r') { out += "\\r"; continue; }
            out.push_back(c);
        }
        return out;
    };
    std::ostringstream oss;
    oss << "{\"sdp\":\"" << esc(sdp) << "\",\"type\":\"" << esc(type) << "\"}";
    std::string resp;
    return RequestJson("POST", "/api/agent/webrtc/" + session_id_ + "/offer", oss.str(), &resp, error_out);
}

bool HttpSignalingClient::PostCandidate(const IceCandidateJson& candidate, std::string* error_out) {
    auto esc = [](const std::string& in) {
        std::string out;
        for (char c : in) {
            if (c == '\\' || c == '"') out.push_back('\\');
            out.push_back(c);
        }
        return out;
    };
    std::ostringstream oss;
    oss << "{\"from\":\"agent\",\"candidate\":{\"candidate\":\"" << esc(candidate.candidate)
        << "\",\"sdpMid\":\"" << esc(candidate.sdp_mid)
        << "\",\"sdpMLineIndex\":" << candidate.sdp_mline_index << "}}";
    std::string resp;
    return RequestJson("POST", "/api/agent/webrtc/" + session_id_ + "/candidates", oss.str(), &resp, error_out);
}

bool HttpSignalingClient::PostError(const std::string& message) {
    std::ostringstream oss;
    oss << "{\"error\":\"";
    for (char c : message) {
        if (c == '\\' || c == '"') oss << '\\';
        oss << c;
    }
    oss << "\"}";
    std::string resp, err;
    return RequestJson("POST", "/api/agent/webrtc/" + session_id_ + "/error", oss.str(), &resp, &err);
}

std::optional<SessionSnapshot> HttpSignalingClient::PollSession(std::string* error_out) {
    std::string resp;
    if (!RequestJson("GET", "/api/agent/webrtc/" + session_id_, "", &resp, error_out)) {
        return std::nullopt;
    }
    SessionSnapshot snap;
    snap.status = JsonGetString(resp, "status");
    snap.error = JsonGetString(resp, "error");
    snap.mode = JsonGetString(resp, "mode");
    // Nested answer.sdp — best-effort parse
    auto answer_pos = resp.find("\"answer\"");
    if (answer_pos != std::string::npos) {
        auto sub = resp.substr(answer_pos);
        snap.answer_sdp = JsonGetString(sub, "sdp");
        snap.answer_type = JsonGetString(sub, "type");
        if (snap.answer_type.empty()) snap.answer_type = "answer";
    }
    // F13-05: viewer ICE candidates were previously dropped entirely —
    // PollSession read the answer SDP only. Parse them so the peer
    // connection can apply them (see peer_connection.cpp).
    snap.viewer_candidates = ParseViewerCandidates(resp);

    auto takeover_pos = resp.find("\"human_takeover_active\"");
    if (takeover_pos != std::string::npos) {
        auto sub = resp.substr(takeover_pos);
        snap.human_takeover_active = sub.find("true") != std::string::npos &&
            (sub.find("true") < sub.find("false") || sub.find("false") == std::string::npos);
    }
    auto perms_pos = resp.find("\"permissions\"");
    if (perms_pos != std::string::npos) {
        auto sub = resp.substr(perms_pos, 400);
        snap.permissions_present = true;
        // Accept both v1 keys (control_mouse) and legacy (control).
        snap.control_mouse =
            sub.find("\"control_mouse\":true") != std::string::npos ||
            sub.find("\"control_mouse\": true") != std::string::npos ||
            (sub.find("\"control\":true") != std::string::npos && sub.find("control_mouse") == std::string::npos);
        snap.control_keyboard =
            sub.find("\"control_keyboard\":true") != std::string::npos ||
            sub.find("\"control_keyboard\": true") != std::string::npos ||
            snap.control_mouse;
        if (snap.mode == "view_only") {
            snap.control_mouse = false;
            snap.control_keyboard = false;
        }
    }
    return snap;
}

} // namespace webrtc
} // namespace aibridge
