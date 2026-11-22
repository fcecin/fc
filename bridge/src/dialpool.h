#pragma once

// The program windows' dials: one RUDP stream per window, bound to
// /ces/lua/1 and attached with the person's key as `cesh dial` does. The
// streams share two threads and a pool of UDP sockets, each carrying at most
// `perSocket` streams to one server, and the RUDP tick runs only while a
// stream is open.

#include <ces/keys.h>

#include <minx/types.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace fc {

class DialPool {
public:
  explicit DialPool(size_t perSocket = 4);
  ~DialPool();
  DialPool(const DialPool&) = delete;
  DialPool& operator=(const DialPool&) = delete;

  // Dials the program `pid` at the rpc port `rpc` of the server with the key
  // `serverKey`, signed by `signer`, and blocks for the handshake. Returns one
  // end of a socket pair, which gives the program's bytes (its greeting first)
  // and takes the bytes for it, and closing it ends the dial. On a failure,
  // returns -1 with the reason in `err`.
  int dial(const ces::KeyPair& signer, const minx::SockAddr& rpc,
           const minx::Hash& serverKey, uint64_t pid, std::string& err);

  // The open streams and UDP sockets now.
  size_t streams() const;
  size_t sockets() const;

  struct Impl;

private:
  std::unique_ptr<Impl> impl_;
};

} // namespace fc
