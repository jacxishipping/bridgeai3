#pragma once

#include "../capture/dxgi_capture.hpp"
#include "../encoder/mf_h264_encoder.hpp"
#include "../input/sendinput_controller.hpp"

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>

namespace aibridge {
namespace webrtc {

struct StreamSessionStats {
    std::string session_id;
    std::string status = "idle";
    // F08-16: machine-readable failure when StartSession() returns
    // false (e.g. "encoder_init_failed"). Empty on a healthy session.
    std::string error;
    // F08-16: whether DXGI duplication initialised. False does NOT by
    // itself fail the session — CaptureNextFrame falls back to GDI —
    // but callers can see capture is degraded.
    bool capture_init_ok = false;
    std::string codec = "H264";
    std::string encoder_backend = "Media Foundation H.264";
    bool hardware_encoding = false;
    int width = 1920;
    int height = 1080;
    int monitor_index = 0;
    int target_fps = 30;
    int target_bitrate_kbps = 4500;
    int adapted_bitrate_kbps = 4500;
    uint64_t monitor_switches = 0;
    uint64_t bitrate_adaptations = 0;
    double capture_time_ms = 0.0;
    double encoder_queue_time_ms = 0.0;
    double encode_time_ms = 0.0;
    size_t encoded_frame_bytes = 0;
    uint64_t frames_captured = 0;
    uint64_t frames_encoded = 0;
    uint64_t dropped_frames = 0;
};

// Honesty: this is a capture+encode pipeline (DXGI + Media Foundation H.264).
// It is NOT a libwebrtc PeerConnection — no SDP/ICE/DTLS/RTP stack is linked here.
// Dashboard Live Desktop E2E today uses the Python aiortc path; see
// scripts/libwebrtc_e2e_checklist.md for the residual PeerConnection work.
class NativeWebRtcPipeline {
public:
    NativeWebRtcPipeline();
    ~NativeWebRtcPipeline();

    bool StartSession(const std::string& session_id, int monitor_index, int width, int height, int fps, int bitrate_kbps);
    // F08-16: StartSession() returns false when the session cannot
    // stream — i.e. when the ENCODER fails to initialise. A failed
    // capture initialisation alone does NOT fail the session (the
    // per-frame GDI fallback may still work) but is recorded in
    // stats.capture_init_ok. Callers MUST check the return value:
    // the old code always returned true, so a dead session reported
    // "streaming". On false, stats.status is "error" and stats.error
    // names the failure; active_ stays false.
    void StopSession();

    // Captures one frame via DXGI and encodes it via Media Foundation H.264, updating stage-by-stage latency stats
    bool StepCaptureAndEncode(capture::CapturedFrame& out_raw, encoder::EncodedAccessUnit& out_encoded);

    // Hot-switch DXGI capture monitor without tearing down the WebRTC PeerConnection.
    bool SwitchMonitor(int monitor_index);

    // Dynamically adjust Media Foundation H.264 target bitrate under packet loss / congestion.
    bool UpdateBitrateKbps(int bitrate_kbps);

    // Request an immediate IDR keyframe on the next encoded frame.
    void ForceKeyframe();

    StreamSessionStats GetStats() const;
    input::WindowsInputController& InputController() { return input_controller_; }

private:
    mutable std::mutex mutex_;
    std::atomic<bool> active_{false};
    std::atomic<bool> force_next_idr_{false};
    StreamSessionStats stats_{};

    capture::DxgiDesktopCapturer capturer_;
    encoder::MediaFoundationH264Encoder encoder_;
    input::WindowsInputController input_controller_;
};

} // namespace webrtc
} // namespace aibridge
