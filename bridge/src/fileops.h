#pragma once

// CES file store operations for the bridge (L2 builtin:file over CesPlex).
// Reads sign with the reader key, so visitors pay nothing to view content, and
// writes sign with the owner key, which pays. Every operation opens its own
// connection and closes it.

#include <ces/keys.h>
#include <ces/l2/file_client.h>
#include <ces/types.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <string>

namespace fc {

struct FileServer {
  std::string host;
  uint16_t rpcPort = 0;
  std::optional<minx::Hash> serverKey;  // pins response signatures
};

struct FileResult {
  uint8_t code = 0;      // CES error code; 0 = CES_OK
  std::string error;
  ces::Bytes content;
  uint64_t size = 0;
  uint64_t fileBalance = 0;
  bool found = false;
};

class FileOps {
public:
  explicit FileOps(const ces::KeyPair& readerKey);

  // Reads up to maxBytes from the start of the file, as the reader key.
  FileResult get(const FileServer& server, const std::string& path, uint32_t maxBytes);

  // Creates or replaces the whole file as the owner key, then adds `deposit`
  // raw credit to its rent balance, all paid by the owner.
  FileResult put(const FileServer& server, const ces::KeyPair& owner,
                 const std::string& path, const ces::Bytes& content, uint64_t deposit);

  // Adds `amount` raw credit to any file's rent balance, paid by the key that
  // signs.
  FileResult deposit(const FileServer& server, const ces::KeyPair& payer,
                     const std::string& path, uint64_t amount);

private:
  uint8_t connect(ces::CesFileClient& c, const FileServer& s, const ces::KeyPair& kp);

  ces::KeyPair readerKey_;
};

} // namespace fc
