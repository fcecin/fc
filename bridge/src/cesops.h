#pragma once

// CES operations for the bridge. A server is the "host:port" of its main UDP
// port. The bridge keeps one client session per address, up to a cap, each
// with its own mutex, so a slow or dead server blocks only its own requests.
// Requests name the ledger by its public address, and the bridge reaches it
// at its local address.

#include <ces/keys.h>
#include <ces/util/wallet.h>

#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace fc {

struct OpResult {
  uint8_t code = 0;      // CES error code; 0 = CES_OK
  std::string error;     // errorString(code), or a bridge error
  int64_t balance = 0;   // the new origin balance, or the queried one
  bool exists = false;   // for balance queries
};

struct ServerInfo {
  std::string pubkey;    // 64 hex
  uint16_t rpcPort = 0;  // 0 for a server with no L2 file store
  int minDifficulty = 0;
  int powEngine = 0;     // 1 = the server mines and verifies tickets
  int tps = 0;
};

struct Peer {
  std::string pubkey;
  std::string address;
};

class CesOps {
public:
  // `ledger` is the ledger's public address, and `ledgerLocal` the address that
  // the bridge reaches it at, and its server key signs grants. The bridge talks
  // to the ledger and to any public address, plus private and loopback ones
  // with `allowPrivate` (development).
  CesOps(std::string ledger, std::string ledgerLocal, const ces::KeyPair& ledgerKey,
         bool allowPrivate);

  const std::string& ledger() const { return ledger_; }

  // Checks an address from a request, and returns the address to dial, or an
  // empty string with an error code in `why`.
  std::string target(const std::string& address, std::string& why) const;

  void warm();

  // Free reads.
  std::optional<ServerInfo> info(const std::string& address);
  OpResult peers(const std::string& address, uint16_t from, uint16_t max,
                 uint16_t& count, std::vector<Peer>& out);
  OpResult balance(const std::string& address, const minx::Hash& pubkey);

  // Signed operations.
  OpResult grant(const minx::Hash& pubkey, uint64_t amount);
  OpResult transfer(const ces::KeyPair& kp, const std::string& address,
                    const minx::Hash& dest, uint64_t amount);
  OpResult cross(const ces::KeyPair& kp, const std::string& address,
                 const minx::Hash& dest, uint64_t amount, const std::string& destServer);

private:
  struct Slot {
    std::mutex mu;
    std::unique_ptr<ces::ClientSession> s;
    std::optional<ServerInfo> info;
  };

  // Gets an address's slot. The caller locks slot->mu, then calls client() to
  // connect if needed.
  std::shared_ptr<Slot> slot(const std::string& dial);
  ces::CesClient* client(Slot& sl, const std::string& dial);
  void dropLocked(Slot& sl);

  std::string ledger_, ledgerLocal_;
  ces::KeyPair ledgerKey_;
  bool allowPrivate_ = false;
  std::map<std::string, std::shared_ptr<Slot>> slots_;
  std::mutex mapMu_;
};

} // namespace fc
