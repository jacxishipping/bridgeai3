#include "local_security.hpp"

#include <wincrypt.h>
#include <sddl.h>

#include <chrono>
#include <cstring>
#include <mutex>
#include <sstream>
#include <iomanip>
#include <unordered_map>

#pragma comment(lib, "crypt32.lib")
#pragma comment(lib, "advapi32.lib")

namespace aibridge {
namespace security {

namespace {

// F08-03: replay window — a message whose timestamp is further than
// this from the receiver's clock is rejected outright.
constexpr int64_t kReplayWindowSeconds = 120;

// F08-03: nonces accepted within the window, with the Unix second
// they were first seen. Guarded by g_nonce_mutex; entries older than
// the window are pruned on every verification.
std::mutex g_nonce_mutex;
std::unordered_map<std::string, int64_t> g_seen_nonces;

int64_t CurrentUnixSeconds() {
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

bool GenerateNonce(std::string& out_nonce) {
    // 16 bytes from the OS CSPRNG, hex-encoded (32 chars).
    HCRYPTPROV hProv = 0;
    if (!CryptAcquireContextW(&hProv, nullptr, nullptr, PROV_RSA_FULL, CRYPT_VERIFYCONTEXT)) {
        return false;
    }
    uint8_t raw[16] = {};
    BOOL ok = CryptGenRandom(hProv, sizeof(raw), raw);
    CryptReleaseContext(hProv, 0);
    if (!ok) return false;
    static constexpr char kHex[] = "0123456789abcdef";
    out_nonce.clear();
    out_nonce.reserve(32);
    for (uint8_t b : raw) {
        out_nonce.push_back(kHex[(b >> 4) & 0xF]);
        out_nonce.push_back(kHex[b & 0xF]);
    }
    return true;
}

bool IsHex32(const std::string& s) {
    if (s.size() != 32) return false;
    for (char c : s) {
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

bool ConstantTimeEqual(const std::string& a, const std::string& b) {
    if (a.size() != b.size() || a.empty()) return false;
    unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned char>(a[i] ^ b[i]);
    }
    return diff == 0;
}

// Strict base64 decoder (RFC 4648, no whitespace tolerated): returns false
// on any character outside the alphabet, misplaced padding, or a length
// that is not a multiple of 4. Needed to decode the "aib-dpapi-v1:<base64>"
// sealed-secret wire format; there was previously no decoder in the tree.
int Base64Value(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

bool Base64Decode(const std::string& in, std::vector<uint8_t>& out) {
    out.clear();
    if (in.empty() || in.size() % 4 != 0) return false;
    size_t padding = 0;
    if (in.back() == '=') ++padding;
    if (in.size() >= 2 && in[in.size() - 2] == '=') ++padding;
    for (size_t i = 0; i < in.size(); ++i) {
        if (in[i] == '=') {
            if (i < in.size() - padding) return false;
        } else if (Base64Value(in[i]) < 0) {
            return false;
        }
    }
    out.reserve(in.size() / 4 * 3);
    for (size_t i = 0; i < in.size(); i += 4) {
        int v0 = Base64Value(in[i]);
        int v1 = Base64Value(in[i + 1]);
        int v2 = in[i + 2] == '=' ? 0 : Base64Value(in[i + 2]);
        int v3 = in[i + 3] == '=' ? 0 : Base64Value(in[i + 3]);
        uint32_t n = (static_cast<uint32_t>(v0) << 18) | (static_cast<uint32_t>(v1) << 12)
                   | (static_cast<uint32_t>(v2) << 6) | static_cast<uint32_t>(v3);
        out.push_back(static_cast<uint8_t>((n >> 16) & 0xFF));
        if (in[i + 2] != '=') out.push_back(static_cast<uint8_t>((n >> 8) & 0xFF));
        if (in[i + 3] != '=') out.push_back(static_cast<uint8_t>(n & 0xFF));
    }
    return true;
}

} // namespace

bool DpapiSecretStore::IsDpapiSealed(const std::string& value) {
    return value.rfind(kSealedPrefix, 0) == 0;
}

bool DpapiSecretStore::UnsealIfSealed(const std::string& value, std::string& out_plaintext) {
    out_plaintext.clear();
    if (!IsDpapiSealed(value)) {
        out_plaintext = value;
        return true;
    }
    std::vector<uint8_t> encrypted;
    if (!Base64Decode(value.substr(std::strlen(kSealedPrefix)), encrypted)) return false;
    return UnprotectSecret(encrypted, out_plaintext);
}

namespace {

int HexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool DecodeJsonStringContent(const std::string& json, size_t& pos, std::string& out) {
    // pos is at the first character inside the opening quote.
    while (pos < json.size()) {
        char c = json[pos++];
        if (c == '"') return true;
        if (c != '\\' || pos >= json.size()) {
            out.push_back(c);
            continue;
        }
        char e = json[pos++];
        switch (e) {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            case 'u': {
                if (pos + 4 > json.size()) return false;
                int cp = 0;
                for (int i = 0; i < 4; ++i) {
                    int n = HexNibble(json[pos++]);
                    if (n < 0) return false;
                    cp = (cp << 4) | n;
                }
                // BMP only (enough for \\u002B '+' inside DPAPI base64).
                if (cp <= 0x7F) {
                    out.push_back(static_cast<char>(cp));
                } else if (cp <= 0x7FF) {
                    out.push_back(static_cast<char>(0xC0 | ((cp >> 6) & 0x1F)));
                    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                } else {
                    out.push_back(static_cast<char>(0xE0 | ((cp >> 12) & 0x0F)));
                    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
                    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                }
                break;
            }
            default:
                out.push_back(e);
                break;
        }
    }
    return false;
}

} // namespace

std::string ExtractJsonStringField(const std::string& json, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return {};
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    pos = json.find('"', pos + 1);
    if (pos == std::string::npos) return {};
    ++pos;
    std::string out;
    if (!DecodeJsonStringContent(json, pos, out)) return {};
    return out;
}

bool DpapiSecretStore::ProtectSecret(const std::string& plaintext, std::vector<uint8_t>& out_encrypted, bool machine_scope) {
    DATA_BLOB in_blob{};
    in_blob.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(plaintext.data()));
    in_blob.cbData = static_cast<DWORD>(plaintext.size());

    DATA_BLOB out_blob{};
    DWORD flags = CRYPTPROTECT_UI_FORBIDDEN | (machine_scope ? CRYPTPROTECT_LOCAL_MACHINE : 0);

    if (!CryptProtectData(&in_blob, L"AIBridgeDeviceSecret", nullptr, nullptr, nullptr, flags, &out_blob)) {
        return false;
    }

    out_encrypted.assign(out_blob.pbData, out_blob.pbData + out_blob.cbData);
    LocalFree(out_blob.pbData);
    return true;
}

bool DpapiSecretStore::UnprotectSecret(const std::vector<uint8_t>& encrypted, std::string& out_plaintext) {
    if (encrypted.empty()) return false;
    DATA_BLOB in_blob{};
    in_blob.pbData = const_cast<BYTE*>(encrypted.data());
    in_blob.cbData = static_cast<DWORD>(encrypted.size());

    DATA_BLOB out_blob{};
    if (!CryptUnprotectData(&in_blob, nullptr, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out_blob)) {
        return false;
    }

    out_plaintext.assign(reinterpret_cast<const char*>(out_blob.pbData), out_blob.cbData);
    SecureZeroMemory(out_blob.pbData, out_blob.cbData);
    LocalFree(out_blob.pbData);
    return true;
}

std::string SecureNamedPipe::ComputeTag(const std::string& msg_type, const std::string& nonce, int64_t timestamp_seconds, const std::string& payload, const std::string& secret) {
    // RFC 2104 HMAC-SHA256 using Windows CryptoAPI SHA-256 primitive.
    // F08-03: the tag covers the nonce and timestamp as well as the
    // type and payload, so a captured message cannot be replayed with
    // a fresh timestamp or transplanted onto another message.
    auto sha256 = [](const uint8_t* data, size_t len, std::vector<uint8_t>& digest) -> bool {
        HCRYPTPROV hProv = 0;
        HCRYPTHASH hHash = 0;
        bool ok = false;
        if (CryptAcquireContextW(&hProv, nullptr, nullptr, PROV_RSA_AES, CRYPT_VERIFYCONTEXT)) {
            if (CryptCreateHash(hProv, CALG_SHA_256, 0, 0, &hHash)) {
                if (len == 0 || CryptHashData(hHash, data, static_cast<DWORD>(len), 0)) {
                    digest.resize(32);
                    DWORD hash_len = 32;
                    if (CryptGetHashParam(hHash, HP_HASHVAL, digest.data(), &hash_len, 0) && hash_len == 32) {
                        ok = true;
                    }
                }
                CryptDestroyHash(hHash);
            }
            CryptReleaseContext(hProv, 0);
        }
        return ok;
    };

    constexpr size_t kBlockSize = 64;
    std::vector<uint8_t> key(kBlockSize, 0);
    if (secret.size() > kBlockSize) {
        std::vector<uint8_t> hashed_key;
        if (!sha256(reinterpret_cast<const uint8_t*>(secret.data()), secret.size(), hashed_key)) {
            return {};
        }
        std::copy(hashed_key.begin(), hashed_key.end(), key.begin());
    } else {
        std::memcpy(key.data(), secret.data(), secret.size());
    }

    std::vector<uint8_t> ipad(kBlockSize);
    std::vector<uint8_t> opad(kBlockSize);
    for (size_t i = 0; i < kBlockSize; ++i) {
        ipad[i] = static_cast<uint8_t>(key[i] ^ 0x36);
        opad[i] = static_cast<uint8_t>(key[i] ^ 0x5C);
    }

    std::string message = msg_type + "|" + nonce + "|" + std::to_string(timestamp_seconds) + "|" + payload;
    std::vector<uint8_t> inner_in;
    inner_in.reserve(kBlockSize + message.size());
    inner_in.insert(inner_in.end(), ipad.begin(), ipad.end());
    inner_in.insert(inner_in.end(), message.begin(), message.end());

    std::vector<uint8_t> inner_hash;
    if (!sha256(inner_in.data(), inner_in.size(), inner_hash)) {
        return {};
    }

    std::vector<uint8_t> outer_in;
    outer_in.reserve(kBlockSize + inner_hash.size());
    outer_in.insert(outer_in.end(), opad.begin(), opad.end());
    outer_in.insert(outer_in.end(), inner_hash.begin(), inner_hash.end());

    std::vector<uint8_t> mac;
    if (!sha256(outer_in.data(), outer_in.size(), mac)) {
        return {};
    }

    std::ostringstream oss;
    for (uint8_t b : mac) {
        oss << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(b);
    }
    return oss.str();
}

HANDLE SecureNamedPipe::CreateRestrictedServerPipe(const wchar_t* pipe_name) {
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(SECURITY_ATTRIBUTES);
    sa.bInheritHandle = FALSE;

    // F08-03 (Wave 5A): SDDL tightened from AU (any authenticated
    // user, Generic Read/Write) to IU (interactive logon users only).
    // The desktop agent runs in the interactive session, so IU covers
    // the only legitimate client; a service or network-logon process
    // can no longer open the pipe at all. The pipe secret + HMAC
    // (with nonce/timestamp replay protection) remain the second
    // layer — the ACL is not the sole control.
    PSECURITY_DESCRIPTOR pSD = nullptr;
    const wchar_t* sddl = L"D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)";
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, &pSD, nullptr)) {
        sa.lpSecurityDescriptor = pSD;
    }

    HANDLE hPipe = CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        PIPE_UNLIMITED_INSTANCES,
        65536,
        65536,
        1000,
        pSD ? &sa : nullptr
    );

    if (pSD) {
        LocalFree(pSD);
    }
    return hPipe;
}

HANDLE SecureNamedPipe::ConnectClientPipe(const wchar_t* pipe_name, DWORD timeout_ms) {
    if (!WaitNamedPipeW(pipe_name, timeout_ms)) {
        return INVALID_HANDLE_VALUE;
    }
    HANDLE hPipe = CreateFileW(
        pipe_name,
        GENERIC_READ | GENERIC_WRITE,
        0,
        nullptr,
        OPEN_EXISTING,
        0,
        nullptr
    );
    if (hPipe != INVALID_HANDLE_VALUE) {
        DWORD mode = PIPE_READMODE_MESSAGE;
        SetNamedPipeHandleState(hPipe, &mode, nullptr, nullptr);
    }
    return hPipe;
}

std::string SecureNamedPipe::BuildWireMessage(
    const std::string& msg_type,
    const std::string& json_payload,
    const std::string& shared_secret
) {
    if (msg_type.empty() || shared_secret.empty()) return {};
    std::string nonce;
    if (!GenerateNonce(nonce)) return {};
    int64_t now = CurrentUnixSeconds();
    std::string tag = ComputeTag(msg_type, nonce, now, json_payload, shared_secret);
    if (tag.empty()) return {};
    return msg_type + "\n" + nonce + "\n" + std::to_string(now) + "\n" + tag + "\n" + json_payload;
}

bool SecureNamedPipe::ParseAndVerifyWire(
    const std::string& wire,
    const std::string& shared_secret,
    std::string& out_msg_type,
    std::string& out_json_payload
) {
    if (wire.empty() || shared_secret.empty()) return false;
    size_t nl1 = wire.find('\n');
    if (nl1 == std::string::npos) return false;
    size_t nl2 = wire.find('\n', nl1 + 1);
    if (nl2 == std::string::npos) return false;
    size_t nl3 = wire.find('\n', nl2 + 1);
    if (nl3 == std::string::npos) return false;
    size_t nl4 = wire.find('\n', nl3 + 1);
    if (nl4 == std::string::npos) return false;

    std::string msg_type = wire.substr(0, nl1);
    std::string nonce = wire.substr(nl1 + 1, nl2 - (nl1 + 1));
    std::string ts_text = wire.substr(nl2 + 1, nl3 - (nl2 + 1));
    std::string recv_tag = wire.substr(nl3 + 1, nl4 - (nl3 + 1));
    std::string payload = wire.substr(nl4 + 1);

    if (msg_type.empty() || !IsHex32(nonce)) return false;
    int64_t timestamp = 0;
    try {
        size_t used = 0;
        timestamp = std::stoll(ts_text, &used);
        if (used != ts_text.size()) return false;
    } catch (...) {
        return false;
    }
    int64_t now = CurrentUnixSeconds();
    int64_t skew = now - timestamp;
    if (skew < 0) skew = -skew;
    if (skew > kReplayWindowSeconds) return false;

    std::string expected_tag = ComputeTag(msg_type, nonce, timestamp, payload, shared_secret);
    if (!ConstantTimeEqual(recv_tag, expected_tag)) return false;

    // Replay check: reject any nonce already seen within the window,
    // and prune entries older than the window so the set stays small.
    {
        std::lock_guard<std::mutex> lock(g_nonce_mutex);
        for (auto it = g_seen_nonces.begin(); it != g_seen_nonces.end();) {
            if (now - it->second > kReplayWindowSeconds) it = g_seen_nonces.erase(it);
            else ++it;
        }
        if (g_seen_nonces.find(nonce) != g_seen_nonces.end()) return false;
        g_seen_nonces.emplace(nonce, now);
    }

    out_msg_type = msg_type;
    out_json_payload = payload;
    return true;
}

bool SecureNamedPipe::SendAuthenticatedMessage(
    HANDLE pipe,
    const std::string& msg_type,
    const std::string& json_payload,
    const std::string& shared_secret
) {
    if (pipe == INVALID_HANDLE_VALUE || msg_type.empty()) return false;
    std::string wire = BuildWireMessage(msg_type, json_payload, shared_secret);
    if (wire.empty() || wire.size() > 65536) return false;

    DWORD written = 0;
    BOOL ok = WriteFile(pipe, wire.data(), static_cast<DWORD>(wire.size()), &written, nullptr);
    return ok && (written == wire.size());
}

bool SecureNamedPipe::ReadAuthenticatedMessage(
    HANDLE pipe,
    std::string& out_msg_type,
    std::string& out_json_payload,
    const std::string& shared_secret
) {
    if (pipe == INVALID_HANDLE_VALUE) return false;
    std::vector<char> buffer(65536);
    DWORD bytes_read = 0;
    if (!ReadFile(pipe, buffer.data(), static_cast<DWORD>(buffer.size()), &bytes_read, nullptr) || bytes_read == 0) {
        return false;
    }

    std::string wire(buffer.data(), bytes_read);
    return ParseAndVerifyWire(wire, shared_secret, out_msg_type, out_json_payload);
}

} // namespace security
} // namespace aibridge
