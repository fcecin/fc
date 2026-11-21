#include "computeops.h"

#include <iostream>

namespace fc {

namespace {

ComputeResult fail(uint8_t rc) {
  ComputeResult r;
  r.code = rc;
  r.error = ces::errorString(rc);
  return r;
}

bool transportFailure(uint8_t rc) {
  return rc == ces::CES_ERROR_TIMEOUT || rc == ces::CES_ERROR_INTERNAL ||
         rc == ces::CES_ERROR_UNREACHABLE || rc == ces::CES_ERROR_CONNECTION_LOST;
}

std::string idOf(const FileServer& s) { return s.host + ":" + std::to_string(s.rpcPort); }

uint8_t connect(ces::CesComputeClient& c, const FileServer& s, const ces::KeyPair& kp) {
  if (s.serverKey)
    c.setServerPubkey(*s.serverKey);
  return c.connect(s.host, s.rpcPort, kp);
}

// Connect as `kp`, run f(client, result), then end the client's channel with
// the server.
template <class F>
ComputeResult session(const FileServer& s, const ces::KeyPair& kp, F&& f) {
  ces::CesComputeClient c;
  uint8_t rc = connect(c, s, kp);
  ComputeResult r;
  if (rc == ces::CES_OK)
    rc = f(c, r);
  c.disconnect();
  return rc == ces::CES_OK ? r : fail(rc);
}

} // namespace

ComputeOps::ComputeOps(const ces::KeyPair& readerKey) : readerKey_(readerKey) {}

ComputeResult ComputeOps::launch(const FileServer& s, const ces::KeyPair& owner,
                                 const std::string& path) {
  return session(s, owner, [&](ces::CesComputeClient& c, ComputeResult& r) {
    return c.launch(path, r.pid, r.startedUs);
  });
}

ComputeResult ComputeOps::kill(const FileServer& s, const ces::KeyPair& owner, uint64_t pid) {
  return session(s, owner, [&](ces::CesComputeClient& c, ComputeResult&) { return c.kill(pid); });
}

ComputeResult ComputeOps::list(const FileServer& s, const ces::KeyPair& owner) {
  return session(s, owner, [&](ces::CesComputeClient& c, ComputeResult& r) {
    return c.list(r.instances);
  });
}

// Runs f(client) on a new reader connection, then closes it, since an open
// CesPlex client holds a receive ring of about 32 MB. A transport failure
// gets one more try on a new connection.
template <class F>
ComputeResult ComputeOps::withReader(const FileServer& s, F&& f) {
  ComputeResult last;
  for (int attempt = 0; attempt < 2; ++attempt) {
    last = session(s, readerKey_, f);
    if (!transportFailure(last.code))
      break;
  }
  if (transportFailure(last.code))
    std::cerr << "compute reader " << idOf(s) << " failed: " << last.error << "\n";
  return last;
}

ComputeResult ComputeOps::instances(const FileServer& s, const std::string& path) {
  return withReader(s, [&](ces::CesComputeClient& c, ComputeResult& r) {
    return c.instances(path, r.instances);
  });
}

ComputeResult ComputeOps::call(const FileServer& s, const ces::KeyPair* caller, uint64_t pid,
                               uint64_t value, const ces::Bytes& memo) {
  if (!caller) {
    if (value != 0)
      return fail(ces::CES_ERROR_BAD_INPUT);
    return withReader(s, [&](ces::CesComputeClient& c, ComputeResult& r) {
      return c.call(pid, 0, memo, r.reply);
    });
  }
  return session(s, *caller, [&](ces::CesComputeClient& c, ComputeResult& r) {
    return c.call(pid, value, memo, r.reply);
  });
}

} // namespace fc
