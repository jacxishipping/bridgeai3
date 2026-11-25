#include "../src/capture/dxgi_capture.hpp"
#include "../src/encoder/mf_h264_encoder.hpp"
#include "../src/input/sendinput_controller.hpp"
#include "../src/monitoring/system_monitor.hpp"
#include "../src/security/local_security.hpp"
#include "../src/webrtc/webrtc_transport.hpp"
#include "../src/mcp/mcp_executor.hpp"

#include <cassert>
#include <iostream>
#include <mutex>
#include <thread>

// R-P0-09 / F08-14: assert() is stripped under NDEBUG, so checks use
// CHECK() instead — a passing Release build must mean the checks
// actually ran. CHECK() is thread-safe (the pipe test asserts from a
// server thread).
namespace {
int g_check_failures = 0;
std::mutex g_check_mutex;
void check_impl(bool cond, const char* expr, int line) {
    if (!cond) {
        std::lock_guard<std::mutex> lock(g_check_mutex);
        ++g_check_failures;
        std::cerr << "CHECK FAILED (line " << line << "): " << expr << std::endl;
    }
}
} // namespace
#define CHECK(cond) check_impl(static_cast<bool>(cond), #cond, __LINE__)

int main() {
    std::cout << "[1/7] Testing DPAPI Secret Protection..." << std::endl;
    {
        std::string secret = "device-enroll-token-987654321";
        std::vector<uint8_t> encrypted;
        bool enc_ok = aibridge::security::DpapiSecretStore::ProtectSecret(secret, encrypted, false);
        CHECK(enc_ok && !encrypted.empty());

        std::string decrypted;
        bool dec_ok = aibridge::security::DpapiSecretStore::UnprotectSecret(encrypted, decrypted);
        CHECK(dec_ok && decrypted == secret);
    }

    std::cout << "[2/7] Testing Authenticated Restricted Named Pipe IPC..." << std::endl;
    {
        const wchar_t* test_pipe = L"\\\\.\\pipe\\AIBridgeTestPipeV1";
        const std::string ipc_key = "test-shared-hmac-secret";
        std::string server_received_type;
        std::string server_received_payload;

        std::thread server_thread([&]() {
            HANDLE hServer = aibridge::security::SecureNamedPipe::CreateRestrictedServerPipe(test_pipe);
            CHECK(hServer != INVALID_HANDLE_VALUE);
            BOOL connected = ConnectNamedPipe(hServer, nullptr) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);
            if (connected) {
                aibridge::security::SecureNamedPipe::ReadAuthenticatedMessage(
                    hServer, server_received_type, server_received_payload, ipc_key
                );
                aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
                    hServer, "HEARTBEAT_PONG", "{\"status\":\"verified\"}", ipc_key
                );
                DisconnectNamedPipe(hServer);
            }
            CloseHandle(hServer);
        });

        std::this_thread::sleep_for(std::chrono::milliseconds(80));
        HANDLE hClient = aibridge::security::SecureNamedPipe::ConnectClientPipe(test_pipe, 2000);
        CHECK(hClient != INVALID_HANDLE_VALUE);
        bool sent = aibridge::security::SecureNamedPipe::SendAuthenticatedMessage(
            hClient, "REGISTER_DESKTOP_AGENT", "{\"session\":1}", ipc_key
        );
        CHECK(sent);

        // F08-04: the client reads the supervisor's reply (the desktop
        // agent used to send and close without reading it).
        std::string reply_type, reply_payload;
        bool read_ok = aibridge::security::SecureNamedPipe::ReadAuthenticatedMessage(
            hClient, reply_type, reply_payload, ipc_key
        );
        CHECK(read_ok && reply_type == "HEARTBEAT_PONG");
        CloseHandle(hClient);
        server_thread.join();
        CHECK(server_received_type == "REGISTER_DESKTOP_AGENT");
    }

    // F08-03 (Wave 5A): replay protection, exercised without a pipe via
    // the pure BuildWireMessage / ParseAndVerifyWire functions.
    std::cout << "[2b/7] Testing pipe replay protection (nonce + timestamp)..." << std::endl;
    {
        const std::string ipc_key = "test-shared-hmac-secret";
        std::string wire = aibridge::security::SecureNamedPipe::BuildWireMessage(
            "HEARTBEAT_PING", "{\"n\":1}", ipc_key);
        CHECK(!wire.empty());
        std::string out_type, out_payload;
        // First delivery verifies.
        CHECK(aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
            wire, ipc_key, out_type, out_payload));
        CHECK(out_type == "HEARTBEAT_PING" && out_payload == "{\"n\":1}");
        // The identical bytes a second time are a replay: rejected.
        CHECK(!aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
            wire, ipc_key, out_type, out_payload));
        // A fresh message (new nonce) verifies.
        std::string wire2 = aibridge::security::SecureNamedPipe::BuildWireMessage(
            "HEARTBEAT_PING", "{\"n\":1}", ipc_key);
        CHECK(wire2 != wire);
        CHECK(aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
            wire2, ipc_key, out_type, out_payload));
        // Tampered payload: the tag no longer matches (fresh wire, so
        // this isolates tampering from the replay check above).
        std::string wire3 = aibridge::security::SecureNamedPipe::BuildWireMessage(
            "HEARTBEAT_PING", "{\"n\":1}", ipc_key);
        std::string tampered = wire3;
        size_t last_nl = tampered.find_last_of('\n');
        if (last_nl != std::string::npos) tampered.replace(last_nl + 1, 1, "X");
        CHECK(!aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
            tampered, ipc_key, out_type, out_payload));
        // Wrong secret: rejected.
        CHECK(!aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
            aibridge::security::SecureNamedPipe::BuildWireMessage(
                "HEARTBEAT_PING", "{\"n\":2}", ipc_key),
            "different-secret", out_type, out_payload));
        // Stale timestamp (10 minutes old): rejected even with a valid tag.
        {
            const std::string nonce = "0123456789abcdef0123456789abcdef";
            int64_t stale = static_cast<int64_t>(
                std::chrono::duration_cast<std::chrono::seconds>(
                    std::chrono::system_clock::now().time_since_epoch()).count()) - 600;
            std::string tag = aibridge::security::SecureNamedPipe::ComputeTag(
                "HEARTBEAT_PING", nonce, stale, "{\"n\":3}", ipc_key);
            std::string stale_wire = "HEARTBEAT_PING\n" + nonce + "\n" +
                                     std::to_string(stale) + "\n" + tag + "\n{\"n\":3}";
            CHECK(!aibridge::security::SecureNamedPipe::ParseAndVerifyWire(
                stale_wire, ipc_key, out_type, out_payload));
        }
    }

    std::cout << "[3/7] Testing SendInput Coordinate Transformation & Disconnect Key Release Safety..." << std::endl;
    {
        auto pt_origin = aibridge::input::WindowsInputController::TransformCoordinates(0.0, 0.0, true, 0, 0, 1920, 1080);
        CHECK(pt_origin.pixel_x == 0 && pt_origin.pixel_y == 0);

        auto pt_center = aibridge::input::WindowsInputController::TransformCoordinates(0.5, 0.5, true, 0, 0, 1920, 1080);
        CHECK(pt_center.pixel_x >= 959 && pt_center.pixel_x <= 961);
        CHECK(pt_center.pixel_y >= 539 && pt_center.pixel_y <= 541);
        CHECK(pt_center.normalized_dx > 0 && pt_center.normalized_dy > 0);

        aibridge::input::WindowsInputController input_ctrl;
        // F08-07 (Wave 5A): a fresh controller denies input by
        // default — no session has enabled it yet. Loading the DLL
        // alone must not grant injection.
        {
            auto defaults = input_ctrl.GetPermissions();
            CHECK(!defaults.allow_mouse && !defaults.allow_keyboard &&
                  !defaults.allow_clipboard && !defaults.session_active);
            CHECK(!input_ctrl.MoveMouse(0.5, 0.5, true));
            CHECK(!input_ctrl.KeyEvent(VK_SHIFT, true, false));
            CHECK(input_ctrl.GetHeldKeyCount() == 0);
        }
        aibridge::input::SessionInputPermissions perms{true, true, false, true};
        input_ctrl.SetPermissions(perms);

        // Press Shift key down and verify held key tracking & release on disconnect
        input_ctrl.KeyEvent(VK_SHIFT, true, false);
        CHECK(input_ctrl.GetHeldKeyCount() == 1);
        int released = input_ctrl.ReleaseAllHeldKeys();
        CHECK(released >= 1 && input_ctrl.GetHeldKeyCount() == 0);

        // Verify permission enforcement blocks input when allow_mouse=false
        perms.allow_mouse = false;
        input_ctrl.SetPermissions(perms);
        CHECK(!input_ctrl.MoveMouse(0.5, 0.5, true));

        // F08-07: revoking all permissions (session end) blocks input
        // again — a stale allow must never survive a disconnect.
        aibridge::input::SessionInputPermissions denied{false, false, false, false};
        input_ctrl.SetPermissions(denied);
        CHECK(!input_ctrl.MoveMouse(0.5, 0.5, true));
        CHECK(!input_ctrl.KeyEvent(VK_SHIFT, true, false));

        // Clipboard sync requires allow_clipboard + session_active.
        std::wstring clip_out;
        CHECK(!input_ctrl.GetClipboardText(clip_out));
        CHECK(!input_ctrl.SetClipboardText(L"blocked"));
        aibridge::input::SessionInputPermissions clip_perms{false, false, true, true};
        input_ctrl.SetPermissions(clip_perms);
        CHECK(input_ctrl.SetClipboardText(L"ai-bridge-clip-test"));
        CHECK(input_ctrl.GetClipboardText(clip_out));
        CHECK(clip_out == L"ai-bridge-clip-test");
    }

    std::cout << "[4/7] Testing DXGI Desktop Duplication & Multi-Monitor Enumeration..." << std::endl;
    {
        aibridge::capture::DxgiDesktopCapturer capturer;
        auto monitors = capturer.EnumerateMonitors();
        CHECK(!monitors.empty());
        CHECK(monitors[0].width > 0 && monitors[0].height > 0);

        aibridge::capture::CapturedFrame frame;
        bool cap_ok = capturer.CaptureNextFrame(frame, 50, true);
        CHECK(cap_ok && frame.valid);
        CHECK(frame.width > 0 && frame.height > 0);
        CHECK(frame.bgra_pixels.size() == static_cast<size_t>(frame.width) * static_cast<size_t>(frame.height) * 4);
    }

    std::cout << "[5/7] Testing Media Foundation H.264 Encoding Pipeline (R-P0-09)..." << std::endl;
    {
        // The deleted fake-IDR fallback emitted exactly these 46 bytes and
        // reported valid=true. No access unit from a real or a failing
        // encoder may ever equal them.
        const std::vector<uint8_t> kDeletedFakeIdr = {
            0x00, 0x00, 0x00, 0x01, 0x67, 0x42, 0xC0, 0x28, 0xD9, 0x00, 0x78, 0x02, 0x27, 0xE5, 0x84,
            0x00, 0x00, 0x03, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0xF0, 0x3C, 0x60, 0xC9, 0x20,
            0x00, 0x00, 0x00, 0x01, 0x68, 0xCE, 0x3C, 0x80,
            0x00, 0x00, 0x00, 0x01, 0x65, 0x88, 0x84, 0x21, 0xA0
        };

        aibridge::encoder::MediaFoundationH264Encoder encoder;
        aibridge::encoder::EncoderConfig cfg;
        cfg.width = 64;
        cfg.height = 64;
        cfg.fps = 30;
        cfg.bitrate_bps = 1000000;
        cfg.prefer_hardware = false;
        bool init_ok = encoder.Initialize(cfg);

        // 64x64 mid-grey BGRA frame.
        std::vector<uint8_t> bgra(static_cast<size_t>(64) * 64 * 4, 0x80);
        aibridge::encoder::EncodedAccessUnit au;
        bool enc_ok = encoder.EncodeBgraFrame(bgra.data(), 64, 64, 0, true, au);
        if (!init_ok) {
            // No usable MF encoder on this host: the failure must be
            // honest — invalid, no bytes, a reason, counted as dropped.
            CHECK(!enc_ok);
            CHECK(!au.valid);
            CHECK(au.annexb_nal_bytes.empty());
            CHECK(au.annexb_nal_bytes != kDeletedFakeIdr);
            CHECK(!au.failure_reason.empty());
            CHECK(encoder.GetTelemetry().dropped_frames >= 1);
            std::cout << "  -> no MF encoder on this host; failure reported honestly: "
                      << au.failure_reason << std::endl;
        } else {
            // Healthy encoder: a real frame, never the fabricated bytes.
            CHECK(enc_ok && au.valid);
            CHECK(!au.annexb_nal_bytes.empty());
            CHECK(au.annexb_nal_bytes != kDeletedFakeIdr);
            CHECK(au.failure_reason.empty());
            CHECK(encoder.GetTelemetry().frames_encoded >= 1);
            std::cout << "  -> Encoded H.264 frame: " << au.annexb_nal_bytes.size()
                      << " bytes via [" << encoder.GetTelemetry().backend_name << "]"
                      << " (encode=" << au.encode_time_ms << "ms)" << std::endl;
        }

        // Invalid arguments are always a failure, on any encoder.
        aibridge::encoder::EncodedAccessUnit bad;
        CHECK(!encoder.EncodeBgraFrame(nullptr, 64, 64, 0, false, bad));
        CHECK(!bad.valid);
        CHECK(bad.annexb_nal_bytes.empty());
        CHECK(!bad.failure_reason.empty());
        encoder.Shutdown();
    }

    std::cout << "[6/7] Testing Windows System Monitor, Multi-Session & Window Enumeration..." << std::endl;
    {
        aibridge::monitoring::WindowsSystemMonitor mon;
        auto snap = mon.CollectSnapshot();
        CHECK(snap.memory_total_gb > 0.0);
        CHECK(snap.disk_total_gb > 0.0);
        CHECK(!snap.hostname.empty());
        CHECK(snap.interactive_session_count >= 0);

        auto wins = aibridge::mcp::DesktopMcpExecutor::GetVisibleWindows();
        if (!wins.empty()) {
            aibridge::capture::DxgiDesktopCapturer cap;
            aibridge::capture::CapturedFrame wframe;
            bool w_ok = cap.CaptureWindow(reinterpret_cast<HWND>(static_cast<uintptr_t>(wins[0].hwnd)), wframe);
            CHECK(w_ok && wframe.valid && wframe.width > 0 && wframe.height > 0);
        }
    }

    std::cout << "[7/7] Testing Wave 3C: monitor-geometry input (F08-10), mouse-button release (F13-08), StartSession honesty (F08-16), secure-desktop cache (F08-09)..." << std::endl;
    {
        // F08-10: monitor offsets shift the resolved pixel by exactly
        // the offset (relative assertion, origin coordinates — robust
        // to the host's virtual-desktop size, which clamps absolute
        // coordinates on narrow displays).
        auto pt_base = aibridge::input::WindowsInputController::TransformCoordinates(0.0, 0.0, true, 0, 0, 1920, 1080);
        auto pt_off = aibridge::input::WindowsInputController::TransformCoordinates(0.0, 0.0, true, 100, 50, 1920, 1080);
        CHECK(pt_base.pixel_x == 0 && pt_base.pixel_y == 0);
        CHECK(pt_off.pixel_x == pt_base.pixel_x + 100);
        CHECK(pt_off.pixel_y == pt_base.pixel_y + 50);

        // F13-08 (C++ side): a mouse-down is tracked as held and
        // ReleaseAllHeldKeys() releases it. The return value of
        // MouseButton is NOT asserted: SendInput can legitimately fail
        // in a non-interactive session (Session 0 / UIPI) while the
        // held-button mask is still set — the mask is what must be
        // released on disconnect.
        aibridge::input::WindowsInputController input_ctrl;
        aibridge::input::SessionInputPermissions perms{true, true, false, true};
        input_ctrl.SetPermissions(perms);
        input_ctrl.MouseButton("left", "down", 0.5, 0.5, true, 0, 0, 1920, 1080);
        CHECK(input_ctrl.GetHeldMouseButtonCount() == 1);
        CHECK(input_ctrl.GetHeldKeyCount() == 0);
        int released_btn = input_ctrl.ReleaseAllHeldKeys();
        CHECK(released_btn >= 1);
        CHECK(input_ctrl.GetHeldMouseButtonCount() == 0);

        // F08-09: the cached secure-desktop probe agrees with the
        // uncached helper, and repeated cached calls agree with each
        // other (2 s TTL). No assertion on the VALUE: it depends on
        // whether this host is locked / on the secure desktop.
        aibridge::capture::DxgiDesktopCapturer cap;
        bool secure_direct = cap.IsSecureDesktopActive();
        bool secure_cached_1 = cap.IsSecureDesktopActiveCached();
        bool secure_cached_2 = cap.IsSecureDesktopActiveCached();
        CHECK(secure_cached_1 == secure_direct);
        CHECK(secure_cached_2 == secure_cached_1);

        // F08-11: monitor enumeration is cached (5 s TTL) — two calls
        // in a row return the same list.
        auto mons_1 = cap.EnumerateMonitors();
        auto mons_2 = cap.EnumerateMonitors();
        CHECK(!mons_1.empty());
        CHECK(mons_1.size() == mons_2.size());

        // F08-16: StartSession is honest. Either the encoder
        // initialises and the session genuinely streams, or it fails
        // and StartSession returns false with status "error" and a
        // non-empty reason — never a fabricated "streaming". After
        // StopSession the status is "stopped" and no more frames pump.
        aibridge::webrtc::NativeWebRtcPipeline pipeline;
        bool started = pipeline.StartSession("wave3c_test_session", 0, 64, 64, 30, 1000);
        auto stats = pipeline.GetStats();
        if (started) {
            CHECK(stats.status == "streaming");
            CHECK(stats.error.empty());
            CHECK(stats.monitor_index == 0);
            CHECK(stats.adapted_bitrate_kbps == 1000);
            // Live monitor switch + dynamic ABR bitrate update + keyframe force
            CHECK(pipeline.SwitchMonitor(0));
            CHECK(pipeline.UpdateBitrateKbps(1500));
            pipeline.ForceKeyframe();
            auto updated = pipeline.GetStats();
            CHECK(updated.monitor_switches >= 1);
            CHECK(updated.bitrate_adaptations >= 1);
            CHECK(updated.adapted_bitrate_kbps == 1500);
            // Reject out-of-range monitor/bitrate gracefully
            CHECK(!pipeline.SwitchMonitor(-1));
            CHECK(!pipeline.UpdateBitrateKbps(50));
        } else {
            CHECK(stats.status == "error");
            CHECK(!stats.error.empty());
            std::cout << "  -> StartSession honestly refused: " << stats.error << std::endl;
        }
        pipeline.StopSession();
        CHECK(pipeline.GetStats().status == "stopped");
        aibridge::capture::CapturedFrame raw;
        aibridge::encoder::EncodedAccessUnit au;
        CHECK(!pipeline.StepCaptureAndEncode(raw, au));
    }

    // CHECK() failures (unlike assert()) survive NDEBUG Release builds.
    if (g_check_failures != 0) {
        std::cerr << "NATIVE WINDOWS AGENT C++ TESTS FAILED: " << g_check_failures
                  << " CHECK(S) FAILED." << std::endl;
        return 1;
    }
    std::cout << "ALL NATIVE WINDOWS AGENT C++ TESTS PASSED!" << std::endl;
    return 0;
}
