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
  return rc == ces::CES_ERROR_TIMEOUT || rc == ces::CES_ERROR_INTERNAL;
}

std::string idOf(const FileServer& s) { return s.host + ":" + std::to_string(s.rpcPort); }

uint8_t connect(ces::CesComputeClient& c, const FileServer& s, const ces::KeyPair& kp) {
  if (s.serverKey)
    c.setServerPubkey(*s.serverKey);
  return c.connect(s.host, s.rpcPort, kp);
}

} // namespace

ComputeOps::ComputeOps(const ces::KeyPair& readerKey) : readerKey_(readerKey) {}

ComputeResult ComputeOps::launch(const FileServer& s, const ces::KeyPair& owner,
                                 const std::string& path) {
  ces::CesComputeClient c;
  uint8_t rc = connect(c, s, owner);
  if (rc != ces::CES_OK)
    return fail(rc);
  ComputeResult r;
  rc = c.launch(path, r.pid, r.startedUs);
  if (rc != ces::CES_OK)
    return fail(rc);
  return r;
}

ComputeResult ComputeOps::kill(const FileServer& s, const ces::KeyPair& owner, uint64_t pid) {
  ces::CesComputeClient c;
  uint8_t rc = connect(c, s, owner);
  if (rc != ces::CES_OK)
    return fail(rc);
  rc = c.kill(pid);
  if (rc != ces::CES_OK)
    return fail(rc);
  return {};
}

ComputeResult ComputeOps::list(const FileServer& s, const ces::KeyPair& owner) {
  ces::CesComputeClient c;
  uint8_t rc = connect(c, s, owner);
  if (rc != ces::CES_OK)
    return fail(rc);
  ComputeResult r;
  rc = c.list(r.instances);
  if (rc != ces::CES_OK)
    return fail(rc);
  return r;
}

// Runs f(client) on a new reader connection, then closes it, since an open
// CesPlex client holds a receive ring of about 32 MB. A transport failure
// gets one more try on a new connection.
template <class F>
ComputeResult ComputeOps::withReader(const FileServer& s, F&& f) {
  for (int attempt = 0; attempt < 2; ++attempt) {
    ces::CesComputeClient c;
    uint8_t rc = connect(c, s, readerKey_);
    if (rc != ces::CES_OK) {
      std::cerr << "compute reader connect " << idOf(s) << " failed: " << ces::errorString(rc) << "\n";
      return fail(rc);
    }
    ComputeResult r;
    rc = f(c, r);
    if (transportFailure(rc))
      continue;
    if (rc != ces::CES_OK)
      return fail(rc);
    return r;
  }
  return fail(ces::CES_ERROR_TIMEOUT);
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
  ces::CesComputeClient c;
  uint8_t rc = connect(c, s, *caller);
  if (rc != ces::CES_OK)
    return fail(rc);
  ComputeResult r;
  rc = c.call(pid, value, memo, r.reply);
  if (rc != ces::CES_OK)
    return fail(rc);
  return r;
}

} // namespace fc
