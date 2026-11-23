#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <cstdint>
#include <string>
#include <vector>

namespace aibridge {
namespace security {

inline constexpr const wchar_t* kDefaultNamedPipePath = L"\\\\.\\pipe\\AIBridgeDesktopPipe";

// Encrypts and decrypts local device enrollment tokens/secrets via Windows DPAPI (CryptProtectData)
class DpapiSecretStore {
public:
    static bool ProtectSecret(const std::string& plaintext, std::vector<uint8_t>& out_encrypted, bool machine_scope = false);
    static bool UnprotectSecret(const std::vector<uint8_t>& encrypted, std::string& out_plaintext);

    // Wire format written by the Windows installer
    // (windows-agent/installer/install-service.ps1 Protect-SecretDPAPI) and
    // the Python agent (agents/windows/app_runtime.py seal_token()):
    //   "aib-dpapi-v1:<base64 of the DPAPI-protected blob>" (LocalMachine scope)
    static constexpr const char* kSealedPrefix = "aib-dpapi-v1:";
    static bool IsDpapiSealed(const std::string& value);
    // Unseals a sealed value (strip prefix, base64-decode, DPAPI-unprotect)
    // and passes a non-sealed value through unchanged. Returns false — with
    // out_plaintext left empty — if a sealed value cannot be decoded or
    // unprotected. Callers MUST treat false as fatal and NEVER fall back to
    // using the sealed string as the secret (F08-08 follow-up).
    static bool UnsealIfSealed(const std::string& value, std::string& out_plaintext);
};

// Hand-rolled JSON string-field extractors used by the service/desktop
// agents (no JSON library is vendored). Must decode JSON escapes including
// \\uXXXX — System.Text.Json defaults escape '+' as \\u002B inside
// aib-dpapi-v1 base64 payloads, and a naive "\\ + next char" reader
// corrupts the sealed blob so CryptUnprotectData fails.
std::string ExtractJsonStringField(const std::string& json, const std::string& key);

// Creates and validates a Named Pipe with restricted ACLs (LocalSystem, Administrators, Authenticated Interactive User)
class SecureNamedPipe {
public:
    static HANDLE CreateRestrictedServerPipe(const wchar_t* pipe_name = kDefaultNamedPipePath);
    static HANDLE ConnectClientPipe(const wchar_t* pipe_name = kDefaultNamedPipePath, DWORD timeout_ms = 2000);

    static bool SendAuthenticatedMessage(HANDLE pipe, const std::string& msg_type, const std::string& json_payload, const std::string& shared_secret);
    static bool ReadAuthenticatedMessage(HANDLE pipe, std::string& out_msg_type, std::string& out_json_payload, const std::string& shared_secret);

    // F08-03 (Wave 5A): replay protection. The wire format is
    //   msg_type "\n" nonce "\n" timestamp "\n" tag "\n" payload
    // where the tag covers msg_type, nonce, timestamp, and payload.
    // BuildWireMessage / ParseAndVerifyWire are pure functions (no
    // I/O) so the replay rules are unit-testable without a pipe:
    // ParseAndVerifyWire rejects stale timestamps (outside ±120 s),
    // malformed nonces, bad tags, and any nonce seen before within
    // the window. Send/ReadAuthenticatedMessage are thin wrappers
    // around them.
    static std::string BuildWireMessage(const std::string& msg_type, const std::string& json_payload, const std::string& shared_secret);
    static bool ParseAndVerifyWire(const std::string& wire, const std::string& shared_secret, std::string& out_msg_type, std::string& out_json_payload);

    static std::string ComputeTag(const std::string& msg_type, const std::string& nonce, int64_t timestamp_seconds, const std::string& payload, const std::string& secret);
};

} // namespace security
} // namespace aibridge
