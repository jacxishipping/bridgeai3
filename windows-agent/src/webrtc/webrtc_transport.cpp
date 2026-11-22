#include "webrtc_transport.hpp"

namespace aibridge {
namespace webrtc {

NativeWebRtcPipeline::NativeWebRtcPipeline() = default;

NativeWebRtcPipeline::~NativeWebRtcPipeline() {
    StopSession();
}

bool NativeWebRtcPipeline::StartSession(
    const std::string& session_id,
    int monitor_index,
    int width,
    int height,
    int fps,
    int bitrate_kbps
) {
    std::lock_guard<std::mutex> lock(mutex_);
    // F08-16: honest return. Capture init may fail while the session is
    // still usable (GDI fallback per frame); encoder init failure is
    // fatal — without an encoder no frame can ever be produced, so the
    // session must report "error", not "streaming".
    bool cap_ok = capturer_.Initialize(monitor_index);

    encoder::EncoderConfig cfg{};
    cfg.width = width > 0 ? width : 1920;
    cfg.height = height > 0 ? height : 1080;
    cfg.fps = fps > 0 ? fps : 30;
    cfg.bitrate_bps = (bitrate_kbps > 0 ? bitrate_kbps : 4500) * 1000;
    cfg.keyframe_interval_sec = 2;
    cfg.prefer_hardware = true;
    cfg.adaptive_bitrate = true;
    bool enc_ok = encoder_.Initialize(cfg);

    auto enc_tel = encoder_.GetTelemetry();
    stats_ = StreamSessionStats{};
    stats_.session_id = session_id;
    stats_.capture_init_ok = cap_ok;
    if (!enc_ok) {
        stats_.status = "error";
        stats_.error = "encoder_init_failed";
        stats_.codec = "H264";
        stats_.encoder_backend = enc_tel.backend_name;
        stats_.hardware_encoding = enc_tel.is_hardware;
        stats_.width = cfg.width;
        stats_.height = cfg.height;
        stats_.target_fps = cfg.fps;
        stats_.target_bitrate_kbps = bitrate_kbps > 0 ? bitrate_kbps : 4500;
        active_.store(false);
        return false;
    }
    stats_.status = "streaming";
    stats_.codec = "H264";
    stats_.encoder_backend = enc_tel.backend_name;
    stats_.hardware_encoding = enc_tel.is_hardware;
    stats_.width = cfg.width;
    stats_.height = cfg.height;
    stats_.target_fps = cfg.fps;
    stats_.target_bitrate_kbps = bitrate_kbps > 0 ? bitrate_kbps : 4500;

    // Deny-by-default: capture/encode pipeline must NOT silently enable
    // SendInput. PeerConnection / WPF / pipe UPDATE_SESSION_PERMISSIONS
    // explicitly grant input when policy allows.
    input::SessionInputPermissions perms{};
    perms.session_active = false;
    perms.allow_mouse = false;
    perms.allow_keyboard = false;
    perms.allow_clipboard = false;
    input_controller_.SetPermissions(perms);

    active_.store(true);
    return true;
}

void NativeWebRtcPipeline::StopSession() {
    active_.store(false);
    input_controller_.ReleaseAllHeldKeys();
    std::lock_guard<std::mutex> lock(mutex_);
    encoder_.Shutdown();
    capturer_.Shutdown();
    stats_.status = "stopped";
}

bool NativeWebRtcPipeline::StepCaptureAndEncode(
    capture::CapturedFrame& out_raw,
    encoder::EncodedAccessUnit& out_encoded
) {
    if (!active_.load()) return false;

    if (!capturer_.CaptureNextFrame(out_raw, 35, true) || !out_raw.valid) {
        std::lock_guard<std::mutex> lock(mutex_);
        ++stats_.dropped_frames;
        return false;
    }

    bool force_idr = (stats_.frames_encoded == 0);
    bool enc_ok = encoder_.EncodeBgraFrame(
        out_raw.bgra_pixels.data(),
        out_raw.width,
        out_raw.height,
        out_raw.timestamp_us,
        force_idr,
        out_encoded
    );

    std::lock_guard<std::mutex> lock(mutex_);
    ++stats_.frames_captured;
    stats_.width = out_raw.width;
    stats_.height = out_raw.height;
    stats_.capture_time_ms = out_raw.capture_time_ms;
    if (enc_ok) {
        auto enc_tel = encoder_.GetTelemetry();
        ++stats_.frames_encoded;
        stats_.encoder_backend = enc_tel.backend_name;
        stats_.hardware_encoding = enc_tel.is_hardware;
        stats_.encoder_queue_time_ms = out_encoded.queue_time_ms;
        stats_.encode_time_ms = out_encoded.encode_time_ms;
        stats_.encoded_frame_bytes = out_encoded.annexb_nal_bytes.size();
    } else {
        ++stats_.dropped_frames;
    }
    return enc_ok;
}

StreamSessionStats NativeWebRtcPipeline::GetStats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

} // namespace webrtc
} // namespace aibridge
