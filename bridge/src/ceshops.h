#pragma once

// The bridge's cesh socket, where the hub runs cesh as a person through ceshd
// (ces's ceshd/1 wire, src/ceshd/ceshd.h). A client (SOCK_SEQPACKET) sends
//   {"sealed": K, "server": "host:port", "args": ["query", ...]}
// with the command's stdin, stdout and stderr (SCM_RIGHTS). The reply is "ok"
// with the ceshd connection (CESH_WALLET is the key, CESH_SERVER the server),
// which brings the result and stops the command when closed, or "error TEXT".
//   {"sealed": K, "server": "host:port", "dial": PID}
// dials a program as the person (dialpool.h), and the "ok" reply carries one
// end of a socket pair for the program's bytes, its greeting first.

#include "dialpool.h"

#include <minx/types.h>

#include <array>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>

namespace fc {

class CeshServer {
public:
  using Unseal = std::function<std::optional<std::array<uint8_t, 32>>(const std::string&)>;
  // The rpc address and key of the server at "host:port", or nothing and the
  // reason.
  struct Target {
    minx::SockAddr rpc;
    minx::Hash key{};
  };
  using Resolve = std::function<std::optional<Target>(const std::string&, std::string&)>;

  CeshServer(std::string socketPath, std::string ceshdPath, Unseal unseal, Resolve resolve,
             DialPool& dials);

  // Makes the socket (mode 0660) and serves it on its own thread. Throws when
  // it can't make the socket.
  void start();

private:
  void serve(int conn) const;

  void dial(int conn, const std::string& sealed, const std::string& server, uint64_t pid,
            const std::string& expectedKey) const;

  std::string socketPath_;
  std::string ceshdPath_;
  Unseal unseal_;
  Resolve resolve_;
  DialPool& dials_;
};

} // namespace fc
