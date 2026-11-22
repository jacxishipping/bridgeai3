#include "peer_connection.hpp"
#include "../capture/dxgi_capture.hpp"
#include "../input/sendinput_controller.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <random>
#include <string>
#include <thread>

#if defined(AIBRIDGE_WITH_LIBDATACHANNEL)
#include <rtc/rtc.hpp>
#endif

namespace aibridge {
namespace webrtc {

std::string PeerBackendName(PeerBackend b) {
    switch (b) {
        case PeerBackend::LibDataChannel: return "libdatachannel";
        case PeerBackend::LibWebRTC: return "libwebrtc";
        default: return "none";
    }
}

PeerBackend DescribeAvailablePeerBackend() {
#if defined(AIBRIDGE_WITH_LIBWEBRTC)
    return PeerBackend::LibWebRTC;
#elif defined(AIBRIDGE_WITH_LIBDATACHANNEL)
    return PeerBackend::LibDataChannel;
#else
    return PeerBackend::None;
#endif
}

#if defined(AIBRIDGE_WITH_LIBDATACHANNEL)
namespace {

// F08-15: hand-rolled JSON extractors — see the full justification in
// http_signaling.cpp (no JSON library is vendored in this tree; these
// extract a fixed set of fields from the plane's own well-formed
// messages and must not grow into a general parser).
std::string ExtractJsonStr(const std::string& json, const std::string& key) {
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

double ExtractJsonNum(const std::string& json, const std::string& key, double fallback = 0.0) {
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return fallback;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return fallback;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) ++pos;
    try {
        return std::stod(json.substr(pos));
    } catch (...) {
        return fallback;
    }
}

class LibDataChannelSession : public PeerConnectionSession {
public:
    explicit LibDataChannelSession(PeerConnectionConfig cfg) : cfg_(std::move(cfg)) {}

    PeerBackend Backend() const override { return PeerBackend::LibDataChannel; }
    std::string IceState() const override {
        std::lock_guard<std::mutex> lock(state_mutex_);
        return ice_state_;
    }
    uint64_t FramesSent() const override { return frames_sent_.load(); }

    bool RunUntilStopped(std::atomic<bool>* stop_flag, std::string* error_out) override {
        HttpSignalingClient signaling(cfg_.control_url, cfg_.device_token, cfg_.session_id);
        rtc::Configuration rtc_cfg;
        rtc_cfg.iceServers.emplace_back("stun:stun.l.google.com:19302");
        // F13-05: TURN when configured (PeerConnectionConfig.turn_*,
        // from the desktop agent's config file or AI_BRIDGE_TURN_*
        // env). Without TURN the peer cannot connect behind symmetric
        // NAT; with it, libdatachannel relays through the TURN server
        // using these credentials.
        if (!cfg_.turn_url.empty()) {
            rtc_cfg.iceServers.emplace_back(cfg_.turn_url);
            rtc::IceServer& turn = rtc_cfg.iceServers.back();
            turn.username = cfg_.turn_username;
            turn.password = cfg_.turn_credential;
        }

        // F08-10: monitor geometry for input, enumerated ONCE per
        // session (not per input event). The Python path passes explicit
        // monitor offsets; the old C++ path passed none, so
        // normalized coordinates were resolved against the virtual
        // desktop origin and multi-monitor clicks landed on the wrong
        // monitor. Geometry comes from the session's monitor index.
        int mon_left = 0, mon_top = 0, mon_width = 1920, mon_height = 1080;
        {
            aibridge::capture::DxgiDesktopCapturer mon_probe;
            auto monitors = mon_probe.EnumerateMonitors();
            const aibridge::capture::MonitorDescriptor* mon = nullptr;
            for (const auto& m : monitors) {
                if (m.index == cfg_.monitor_index) { mon = &m; break; }
            }
            if (!mon && !monitors.empty()) mon = &monitors[0];
            if (mon) {
                mon_left = mon->left;
                mon_top = mon->top;
                mon_width = mon->width;
                mon_height = mon->height;
            }
        }

        aibridge::input::WindowsInputController input_ctrl;
        // Deny-by-default; grant from PeerConnectionConfig + plane poll + session_permissions.json.
        auto apply_input_permissions = [&](bool mouse, bool keyboard, bool clipboard) {
            aibridge::input::SessionInputPermissions perms{};
            perms.allow_mouse = mouse;
            perms.allow_keyboard = keyboard;
            perms.allow_clipboard = clipboard;
            perms.session_active = mouse || keyboard || clipboard;
            input_ctrl.SetPermissions(perms);
        };
        apply_input_permissions(cfg_.allow_mouse, cfg_.allow_keyboard, cfg_.allow_clipboard);

        auto pc = std::make_shared<rtc::PeerConnection>(rtc_cfg);
        pc->onStateChange([this, &input_ctrl](rtc::PeerConnection::State state) {
            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                ice_state_ = std::to_string(static_cast<int>(state));
            }
            if (state == rtc::PeerConnection::State::Disconnected ||
                state == rtc::PeerConnection::State::Failed ||
                state == rtc::PeerConnection::State::Closed) {
                input_ctrl.ReleaseAllHeldKeys();
            }
        });
        pc->onLocalCandidate([&signaling](rtc::Candidate cand) {
            IceCandidateJson j;
            j.candidate = std::string(cand);
            j.sdp_mid = cand.mid();
            // sdp_mline_index 0 is correct here, not a placeholder:
            // the offer carries a single video m-line (index 0); the
            // "input" data channel is SCTP and has no m-line.
            j.sdp_mline_index = 0;
            signaling.PostCandidate(j, nullptr);
        });

        // Create the local offer, then post it to the plane (same contract as Python HttpSignaling).
        std::string err;
        std::string local_sdp;
        std::promise<void> local_ready;
        auto local_future = local_ready.get_future();
        pc->onLocalDescription([&](rtc::Description desc) {
            local_sdp = std::string(desc);
            try {
                local_ready.set_value();
            } catch (...) {
            }
        });

        auto video = rtc::Description::Video("video", rtc::Description::Direction::SendOnly);
        video.addH264Codec(96);
        auto track = pc->addTrack(std::move(video));

        // Annex-B NALs from Media Foundation must be RTP-packetized for browsers.
        // Raw track->send(annexb) is not interoperable with Chromium/aiortc.
        std::mt19937 rng{std::random_device{}()};
        uint32_t ssrc = 1 + (rng() % 0x7ffffffe);
        auto rtpConfig = std::make_shared<rtc::RtpPacketizationConfig>(
            ssrc, "video", /*payloadType=*/96, rtc::H264RtpPacketizer::defaultClockRate);
        auto packetizer = std::make_shared<rtc::H264RtpPacketizer>(
            rtc::H264RtpPacketizer::Separator::LongStartSequence, rtpConfig);
        track->setMediaHandler(packetizer);

        // Same contract as Python webrtc_stream: agent-offerred "input" datachannel.
        auto input_dc = pc->createDataChannel("input");
        input_dc->onClosed([&input_ctrl]() {
            input_ctrl.ReleaseAllHeldKeys();
        });
        input_dc->onMessage([input_dc, &input_ctrl, mon_left, mon_top, mon_width, mon_height](rtc::message_variant msg) {
            std::string text;
            if (std::holds_alternative<std::string>(msg)) {
                text = std::get<std::string>(msg);
            } else if (std::holds_alternative<rtc::binary>(msg)) {
                const auto& bin = std::get<rtc::binary>(msg);
                text.assign(reinterpret_cast<const char*>(bin.data()), bin.size());
            }
            std::string mtype = ExtractJsonStr(text, "type");
            if (mtype.empty()) mtype = ExtractJsonStr(text, "action");
            if (mtype == "ping" || (mtype.empty() && text.find("ping") != std::string::npos)) {
                try {
                    input_dc->send(std::string("{\"type\":\"pong\",\"action\":\"pong\"}"));
                } catch (...) {
                }
                return;
            }
            if (mtype == "mouse_move") {
                double x = ExtractJsonNum(text, "x", 0.5);
                double y = ExtractJsonNum(text, "y", 0.5);
                input_ctrl.MoveMouse(x, y, true, mon_left, mon_top, mon_width, mon_height);
            } else if (mtype == "mouse_down" || mtype == "mouse_up" || mtype == "click") {
                double x = ExtractJsonNum(text, "x", 0.5);
                double y = ExtractJsonNum(text, "y", 0.5);
                std::string btn = ExtractJsonStr(text, "button");
                if (btn.empty()) btn = "left";
                if (mtype == "click") {
                    input_ctrl.MouseButton(btn, "down", x, y, true, mon_left, mon_top, mon_width, mon_height);
                    input_ctrl.MouseButton(btn, "up", x, y, true, mon_left, mon_top, mon_width, mon_height);
                } else {
                    input_ctrl.MouseButton(btn, mtype == "mouse_down" ? "down" : "up", x, y, true, mon_left, mon_top, mon_width, mon_height);
                }
            } else if (mtype == "scroll") {
                int dy = static_cast<int>(ExtractJsonNum(text, "delta_y", 0.0));
                int dx = static_cast<int>(ExtractJsonNum(text, "delta_x", 0.0));
                input_ctrl.MouseScroll(dy, dx);
            } else if (mtype == "key_down" || mtype == "key_up") {
                std::string key = ExtractJsonStr(text, "key");
                if (!key.empty()) {
                    input_ctrl.KeyTokenEvent(key, mtype == "key_down" ? "down" : "up");
                }
            } else if (mtype == "type") {
                std::string chars = ExtractJsonStr(text, "text");
                if (!chars.empty()) {
                    int wlen = MultiByteToWideChar(CP_UTF8, 0, chars.data(), static_cast<int>(chars.size()), nullptr, 0);
                    if (wlen > 0) {
                        std::wstring wtext(wlen, L'\0');
                        MultiByteToWideChar(CP_UTF8, 0, chars.data(), static_cast<int>(chars.size()), wtext.data(), wlen);
                        input_ctrl.TypeUnicodeText(wtext);
                    }
                }
            } else if (mtype == "clipboard_sync") {
                // shared/protocols/input_protocol.json: direction to_agent | from_agent
                // Also accept Python webrtc_stream aliases get/set.
                std::string direction = ExtractJsonStr(text, "direction");
                if (direction.empty()) direction = "to_agent";
                auto utf8_from_wide = [](const std::wstring& w) -> std::string {
                    if (w.empty()) return {};
                    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
                    if (n <= 0) return {};
                    std::string out(n, '\0');
                    WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), out.data(), n, nullptr, nullptr);
                    return out;
                };
                auto wide_from_utf8 = [](const std::string& s) -> std::wstring {
                    if (s.empty()) return {};
                    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
                    if (n <= 0) return {};
                    std::wstring out(n, L'\0');
                    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), n);
                    return out;
                };
                auto json_escape = [](const std::string& s) -> std::string {
                    std::string out;
                    out.reserve(s.size() + 8);
                    for (unsigned char c : s) {
                        if (c == '\\' || c == '"') {
                            out.push_back('\\');
                            out.push_back(static_cast<char>(c));
                        } else if (c < 0x20) {
                            char buf[8];
                            std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                            out += buf;
                        } else {
                            out.push_back(static_cast<char>(c));
                        }
                    }
                    return out;
                };
                if (direction == "from_agent" || direction == "get") {
                    std::wstring clip;
                    bool ok = input_ctrl.GetClipboardText(clip);
                    std::string payload = std::string("{\"type\":\"clipboard_data\",\"ok\":") +
                        (ok ? "true" : "false") + ",\"text\":\"" + json_escape(utf8_from_wide(clip)) + "\"}";
                    try {
                        input_dc->send(payload);
                    } catch (...) {
                    }
                } else if (direction == "to_agent" || direction == "set") {
                    std::string clip_text = ExtractJsonStr(text, "text");
                    bool ok = input_ctrl.SetClipboardText(wide_from_utf8(clip_text));
                    try {
                        input_dc->send(std::string("{\"type\":\"clipboard_data\",\"ok\":") +
                            (ok ? "true" : "false") + ",\"action\":\"set\"}");
                    } catch (...) {
                    }
                }
            }
        });

        // F13-10: register the ICE-gathering callback BEFORE
        // setLocalDescription() so a completion that fires early is not
        // missed. See the gathering wait after the local description
        // is ready, below. The promise is heap-held (shared_ptr) on
        // purpose: the callback may outlive this scope if the peer
        // connection fires during teardown, and a by-reference capture
        // would dangle (this scope's locals are destroyed before pc).
        auto gathering_ready = std::make_shared<std::promise<void>>();
        auto gathering_future = gathering_ready->get_future();
        pc->onGatheringStateChange([gathering_ready](rtc::PeerConnection::GatheringState state) {
            if (state == rtc::PeerConnection::GatheringState::Complete) {
                try {
                    gathering_ready->set_value();
                } catch (...) {
                }
            }
        });

        pc->setLocalDescription();

        if (local_future.wait_for(std::chrono::seconds(5)) != std::future_status::ready || local_sdp.empty()) {
            if (error_out) *error_out = "local_description_timeout";
            return false;
        }
        // F13-10: ICE-complete gate — same rule as the Python path.
        // Do not post the offer until ICE gathering is complete;
        // a partial-candidate offer fails opaquely on restrictive
        // networks. The gathering-state callback was registered before
        // setLocalDescription, so a completion that already fired has
        // already fulfilled the promise.
        // NOTE (needs a Windows build to verify): this uses
        // libdatachannel's onGatheringStateChange / gatheringState /
        // GatheringState::Complete API. If the linked libdatachannel
        // version names these differently, the build will fail here —
        // do not silently drop the gate to make it compile.
        if (pc->gatheringState() != rtc::PeerConnection::GatheringState::Complete) {
            if (gathering_future.wait_for(std::chrono::seconds(5)) != std::future_status::ready) {
                if (error_out) *error_out = "ice_gathering_incomplete";
                signaling.PostError("ice_gathering_incomplete");
                return false;
            }
        }
        // F08-16: do not offer a session the pipeline cannot serve.
        // StartSession now returns false when the encoder fails to
        // initialise; check it BEFORE posting the offer.
        NativeWebRtcPipeline pipeline;
        if (!pipeline.StartSession(cfg_.session_id, cfg_.monitor_index, cfg_.width, cfg_.height, cfg_.fps, cfg_.bitrate_kbps)) {
            auto stats = pipeline.GetStats();
            std::string err_msg = stats.error.empty() ? "pipeline_start_failed" : stats.error;
            if (error_out) *error_out = err_msg;
            signaling.PostError(err_msg);
            return false;
        }
        if (!signaling.PostOffer(local_sdp, "offer", &err)) {
            if (error_out) *error_out = err;
            pipeline.StopSession();
            return false;
        }
        // Visible progress for harnesses that scrape stdout before ICE completes.
        std::cout << "{\"event\":\"offer_posted\",\"backend\":\"libdatachannel\",\"sdp_bytes\":"
                  << local_sdp.size() << "}" << std::endl;

        bool answer_applied = false;
        size_t seen_viewer_candidates = 0;
        // F13-05: NO per-frame HTTPS polling. The old loop polled the
        // plane once per video frame (each poll a fresh TLS handshake).
        // Signalling is now polled at most every 500 ms while waiting
        // for the answer, every 2 s once connected; the frame pump
        // below still runs at cfg_.fps in between. The signalling
        // client also reuses one persistent WinHTTP connection.
        auto last_poll = std::chrono::steady_clock::now() - std::chrono::seconds(1);
        while (stop_flag == nullptr || !stop_flag->load()) {
            auto now = std::chrono::steady_clock::now();
            auto poll_interval = answer_applied ? std::chrono::seconds(2)
                                                 : std::chrono::milliseconds(500);
            if (now - last_poll >= poll_interval) {
                last_poll = now;
                auto snap = signaling.PollSession(nullptr);
                if (snap && (snap->status == "ended" || snap->status == "error")) {
                    break;
                }
                // Keep input permissions aligned with plane session mode + local sidecar.
                bool mouse = cfg_.allow_mouse;
                bool keyboard = cfg_.allow_keyboard;
                bool clipboard = cfg_.allow_clipboard;
                if (snap && snap->permissions_present) {
                    mouse = snap->control_mouse && !snap->human_takeover_active && snap->mode != "view_only";
                    keyboard = snap->control_keyboard && !snap->human_takeover_active && snap->mode != "view_only";
                }
                // Sidecar written by WPF NamedPipeBridge.UpdateSessionPermissionsAsync
                try {
                    std::ifstream ifs;
                    // Prefer beside config via USERPROFILE\.ai-bridge\session_permissions.json
                    const char* home = std::getenv("USERPROFILE");
                    if (home && *home) {
                        std::string path = std::string(home) + "\\.ai-bridge\\session_permissions.json";
                        ifs.open(path);
                    }
                    if (ifs) {
                        std::ostringstream ss;
                        ss << ifs.rdbuf();
                        std::string body = ss.str();
                        if (body.find("\"allow_desktop_control\":false") != std::string::npos ||
                            body.find("\"allow_desktop_control\": false") != std::string::npos) {
                            mouse = false;
                            keyboard = false;
                        } else if (body.find("\"allow_desktop_control\":true") != std::string::npos ||
                                   body.find("\"allow_desktop_control\": true") != std::string::npos) {
                            // Only raise if plane has not forced view_only / takeover
                            if (!(snap && (snap->mode == "view_only" || snap->human_takeover_active))) {
                                mouse = true;
                                keyboard = true;
                            }
                        }
                        clipboard = body.find("\"allow_clipboard\":true") != std::string::npos ||
                                    body.find("\"allow_clipboard\": true") != std::string::npos;
                    }
                } catch (...) {
                }
                apply_input_permissions(mouse, keyboard, clipboard);

                if (snap && !answer_applied && !snap->answer_sdp.empty()) {
                    try {
                        // Reject truncated/empty-looking answers before libdatachannel throws.
                        if (snap->answer_sdp.find("ice-ufrag") == std::string::npos) {
                            if (error_out) *error_out = "answer_missing_ice_ufrag";
                            pipeline.StopSession();
                            return false;
                        }
                        pc->setRemoteDescription(
                            rtc::Description(snap->answer_sdp, snap->answer_type.empty() ? "answer" : snap->answer_type)
                        );
                        answer_applied = true;
                        std::cout << "{\"event\":\"answer_applied\",\"sdp_bytes\":" << snap->answer_sdp.size()
                                  << "}" << std::endl;
                    } catch (const std::exception& ex) {
                        if (error_out) *error_out = ex.what();
                        pipeline.StopSession();
                        return false;
                    }
                }
                // F13-05: apply viewer ICE candidates (previously
                // dropped — PollSession never even parsed them). Only
                // after the remote description is set; each candidate
                // is applied once, in order.
                if (snap && answer_applied) {
                    for (size_t i = seen_viewer_candidates; i < snap->viewer_candidates.size(); ++i) {
                        const auto& cand = snap->viewer_candidates[i];
                        try {
                            pc->addRemoteCandidate(rtc::Candidate(cand.candidate, cand.sdp_mid));
                        } catch (...) {
                            // One bad candidate must not kill the session.
                        }
                    }
                    seen_viewer_candidates = snap->viewer_candidates.size();
                }
            }

            capture::CapturedFrame raw;
            encoder::EncodedAccessUnit au;
            if (pipeline.StepCaptureAndEncode(raw, au) && au.valid && track) {
                try {
                    // Count encoded frames even before ICE opens the track; send when open.
                    frames_sent_.fetch_add(1);
                    if (track->isOpen() && !au.annexb_nal_bytes.empty()) {
                        // H264RtpPacketizer (setMediaHandler above) converts Annex-B → RTP.
                        rtc::binary payload(au.annexb_nal_bytes.size());
                        std::memcpy(
                            payload.data(),
                            au.annexb_nal_bytes.data(),
                            au.annexb_nal_bytes.size()
                        );
                        track->send(std::move(payload));
                    }
                } catch (...) {
                }
            }
            int sleep_ms = 1000 / (cfg_.fps > 0 ? cfg_.fps : 30);
            std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms > 0 ? sleep_ms : 1));
        }
        input_ctrl.ReleaseAllHeldKeys();
        pipeline.StopSession();
        return true;
    }

private:
    PeerConnectionConfig cfg_;
    mutable std::mutex state_mutex_;
    std::string ice_state_ = "new";
    std::atomic<uint64_t> frames_sent_{0};
};

} // namespace
#endif

std::unique_ptr<PeerConnectionSession> CreatePeerConnectionSession(const PeerConnectionConfig& cfg) {
#if defined(AIBRIDGE_WITH_LIBDATACHANNEL)
    return std::make_unique<LibDataChannelSession>(cfg);
#else
    (void)cfg;
    return nullptr;
#endif
}

} // namespace webrtc
} // namespace aibridge
