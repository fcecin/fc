#include "fileops.h"

#include <iostream>

namespace fc {

namespace {

FileResult fail(uint8_t rc) {
  FileResult r;
  r.code = rc;
  r.error = ces::errorString(rc);
  return r;
}

bool transportFailure(uint8_t rc) {
  return rc == ces::CES_ERROR_TIMEOUT || rc == ces::CES_ERROR_INTERNAL ||
         rc == ces::CES_ERROR_UNREACHABLE || rc == ces::CES_ERROR_CONNECTION_LOST;
}

} // namespace

FileOps::FileOps(const ces::KeyPair& readerKey) : readerKey_(readerKey) {}

namespace {
std::string idOf(const FileServer& s) { return s.host + ":" + std::to_string(s.rpcPort); }

// Run f(), then end the client's channel with the server.
template <class F>
FileResult ended(ces::CesFileClient& c, F&& f) {
  FileResult r = f();
  c.disconnect();
  return r;
}
} // namespace

uint8_t FileOps::connect(ces::CesFileClient& c, const FileServer& s, const ces::KeyPair& kp) {
  if (s.serverKey)
    c.setServerPubkey(*s.serverKey);
  return c.connect(s.host, s.rpcPort, kp);
}

// Reads on a new reader connection, then closes it, since an open CesPlex
// client holds a receive ring of about 32 MB. A transport failure gets one
// more try on a new connection.
FileResult FileOps::get(const FileServer& fs, const std::string& path, uint32_t maxBytes) {
  FileResult last;
  for (int attempt = 0; attempt < 2; ++attempt) {
    bool again = false;
    ces::CesFileClient c;
    last = ended(c, [&]() -> FileResult {
      uint8_t rc = connect(c, fs, readerKey_);
      if (rc != ces::CES_OK) {
        std::cerr << "file reader connect " << idOf(fs) << " failed: " << ces::errorString(rc) << "\n";
        return fail(rc);
      }
      ces::CesFileClient::StatInfo st;
      rc = c.stat(path, st);
      if (rc == ces::CES_ERROR_FILE_NOT_FOUND) {
        FileResult r;
        r.found = false;
        return r;
      }
      again = transportFailure(rc);
      if (rc != ces::CES_OK)
        return fail(rc);
      FileResult r;
      r.found = true;
      r.size = st.size;
      r.fileBalance = st.fileBalance;
      uint32_t len = static_cast<uint32_t>(std::min<uint64_t>(st.size, maxBytes));
      if (len > 0) {
        minx::Hash rangeHash;
        rc = c.read(path, 0, len, r.content, rangeHash);
        again = transportFailure(rc);
        if (rc != ces::CES_OK)
          return fail(rc);
      }
      return r;
    });
    if (!again)
      break;
  }
  return last;
}

FileResult FileOps::put(const FileServer& fs, const ces::KeyPair& owner,
                        const std::string& path, const ces::Bytes& content, uint64_t deposit) {
  ces::CesFileClient c;
  return ended(c, [&]() -> FileResult {
    uint8_t rc = connect(c, fs, owner);
    if (rc != ces::CES_OK)
      return fail(rc);

    FileResult r;
    uint64_t bal = 0;
    ces::CesFileClient::StatInfo st;
    rc = c.stat(path, st);
    if (rc == ces::CES_ERROR_FILE_NOT_FOUND) {
      uint64_t cost = 0;
      rc = c.create(path, content.size(), 0, deposit, bal, cost);
      if (rc != ces::CES_OK)
        return fail(rc);
      deposit = 0;  // the create already paid it in
    } else if (rc != ces::CES_OK) {
      return fail(rc);
    } else {
      // Deposit before a resize, since growth needs 15 minutes of rent up front.
      if (deposit > 0) {
        rc = c.deposit(path, deposit, bal);
        if (rc != ces::CES_OK)
          return fail(rc);
        deposit = 0;
      }
      if (st.size != content.size()) {
        uint64_t newSize = 0;
        rc = c.resize(path, content.size(), newSize);
        if (rc != ces::CES_OK)
          return fail(rc);
      }
    }
    if (!content.empty()) {
      rc = c.write(path, 0, content, bal);
      if (rc != ces::CES_OK)
        return fail(rc);
    }
    if (deposit > 0) {
      rc = c.deposit(path, deposit, bal);
      if (rc != ces::CES_OK)
        return fail(rc);
    }
    r.found = true;
    r.size = content.size();
    r.fileBalance = bal;
    return r;
  });
}

FileResult FileOps::deposit(const FileServer& fs, const ces::KeyPair& payer,
                            const std::string& path, uint64_t amount) {
  ces::CesFileClient c;
  return ended(c, [&]() -> FileResult {
    uint8_t rc = connect(c, fs, payer);
    if (rc != ces::CES_OK)
      return fail(rc);
    FileResult r;
    rc = c.deposit(path, amount, r.fileBalance);
    if (rc != ces::CES_OK)
      return fail(rc);
    r.found = true;
    return r;
  });
}

} // namespace fc
