#pragma once

// Sealed private keys: AES-256-GCM under the bridge master key.
// Sealed form = hex(nonce[12] || ciphertext[32] || tag[16]).
// The web app stores only sealed keys, which only the bridge can open.

#include <array>
#include <cstdint>
#include <optional>
#include <string>

namespace fc {

using MasterKey = std::array<uint8_t, 32>;

// Reads a 64-hex-char master key from a file. Throws on a bad file.
MasterKey loadMasterKey(const std::string& path);

std::string seal(const MasterKey& mk, const std::array<uint8_t, 32>& secret);

// Returns nothing when the sealed text is malformed or fails authentication.
std::optional<std::array<uint8_t, 32>> unseal(const MasterKey& mk,
                                              const std::string& sealedHex);

} // namespace fc
