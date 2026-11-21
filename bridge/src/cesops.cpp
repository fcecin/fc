#include "cesops.h"

#include <ces/account.h>
#include <ces/types.h>
#include <ces/util/resolver.h>

#include <boost/asio/ip/address.hpp>

#include <iostream>

namespace fc {

namespace {

constexpr size_t MAX_SESSIONS = 8;

OpResult fromCode(uint8_t rc) {
  OpResult r;
  r.code = rc;
  if (rc != ces::CES_OK)
    r.error = ces::errorString(rc);
  return r;
}

OpResult bridgeError(const std::string& msg) {
  OpResult r;
  r.code = 0xFF;
  r.error = msg;
  return r;
}

// A timeout can mean a dead session, so a failed call drops it and the next
// request reconnects.
bool isTransportFailure(uint8_t rc) {
  return rc == ces::CES_ERROR_TIMEOUT || rc == ces::CES_ERROR_INTERNAL ||
         rc == ces::CES_ERROR_UNREACHABLE || rc == ces::CES_ERROR_CONNECTION_LOST;
}

bool publicAddress(const boost::asio::ip::address& a) {
  if (a.is_unspecified() || a.is_loopback() || a.is_multicast())
    return false;
  if (a.is_v4()) {
    auto b = a.to_v4().to_bytes();
    if (b[0] == 10 || b[0] == 0 || b[0] >= 224) return false;
    if (b[0] == 172 && (b[1] & 0xF0) == 16) return false;
    if (b[0] == 192 && b[1] == 168) return false;
    if (b[0] == 169 && b[1] == 254) return false;
    if (b[0] == 100 && (b[1] & 0xC0) == 64) return false;  // CGNAT
    return true;
  }
  auto v6 = a.to_v6();
  if (v6.is_link_local() || v6.is_site_local() || v6.is_v4_mapped())
    return false;
  auto b = v6.to_bytes();
  return (b[0] & 0xFE) != 0xFC;  // unique local
}

bool goodHostPort(const std::string& s) {
  auto c = s.rfind(':');
  if (c == std::string::npos || c == 0 || c > 253 || s.size() - c > 6 || s.size() - c < 2)
    return false;
  for (size_t i = 0; i < c; ++i) {
    char ch = s[i];
    if (!(std::isalnum(static_cast<unsigned char>(ch)) || ch == '.' || ch == '-'))
      return false;
  }
  for (size_t i = c + 1; i < s.size(); ++i)
    if (!std::isdigit(static_cast<unsigned char>(s[i])))
      return false;
  int port = std::stoi(s.substr(c + 1));
  return port > 0 && port < 65536;
}

} // namespace

CesOps::CesOps(std::string ledger, std::string ledgerLocal, const ces::KeyPair& ledgerKey,
               bool allowPrivate)
    : ledger_(std::move(ledger)), ledgerLocal_(std::move(ledgerLocal)), ledgerKey_(ledgerKey),
      allowPrivate_(allowPrivate) {}

std::string CesOps::target(const std::string& address, std::string& why) const {
  if (address == ledger_)
    return ledgerLocal_;
  if (!goodHostPort(address)) {
    why = "BAD_ADDRESS";
    return {};
  }
  try {
    auto ep = ces::Resolver::resolveUdp(address);
    if (!allowPrivate_ && !publicAddress(ep.address())) {
      why = "ADDRESS_NOT_PUBLIC";
      return {};
    }
  } catch (const std::exception&) {
    why = "CANNOT_RESOLVE";
    return {};
  }
  return address;
}

std::shared_ptr<CesOps::Slot> CesOps::slot(const std::string& dial) {
  std::lock_guard lk(mapMu_);
  auto it = slots_.find(dial);
  if (it != slots_.end())
    return it->second;
  if (slots_.size() >= MAX_SESSIONS) {
    // Close one idle session that is not the ledger.
    for (auto i = slots_.begin(); i != slots_.end(); ++i) {
      if (i->first == ledgerLocal_)
        continue;
      std::unique_lock tl(i->second->mu, std::try_to_lock);
      if (!tl.owns_lock())
        continue;
      tl.unlock();
      slots_.erase(i);
      break;
    }
  }
  auto s = std::make_shared<Slot>();
  slots_[dial] = s;
  return s;
}

ces::CesClient* CesOps::client(Slot& sl, const std::string& dial) {
  if (sl.s)
    return &sl.s->client();
  try {
    auto ep = ces::Resolver::resolveUdp(dial);
    // cacheOnly = true is RandomX light mode, since tickets are cheap and the
    // full dataset costs ~2 GB of RAM per session. An unknown server gets one
    // try, so a dead address fails fast.
    int tries = dial == ledgerLocal_ ? 3 : 2;
    sl.s = std::make_unique<ces::ClientSession>(true, 0, ep, nullptr, tries);
    auto& c = sl.s->client();
    ServerInfo in;
    in.pubkey = minx::hashToString(c.getServerKey());
    in.rpcPort = c.getServerRpcPort();
    in.minDifficulty = c.getMinDifficulty();
    in.powEngine = c.getServerPowEngine();
    in.tps = c.getTps();
    sl.info = in;
    c.setTries(3);
    return &c;
  } catch (const std::exception& e) {
    std::cerr << "connect " << dial << " failed: " << e.what() << "\n";
    return nullptr;
  }
}

void CesOps::dropLocked(Slot& sl) {
  sl.s.reset();
  sl.info.reset();
}

void CesOps::warm() {
  auto sl = slot(ledgerLocal_);
  std::lock_guard lk(sl->mu);
  client(*sl, ledgerLocal_);
}

std::optional<ServerInfo> CesOps::info(const std::string& dial) {
  auto sl = slot(dial);
  std::lock_guard lk(sl->mu);
  if (sl->s) {
    // Refresh with a free GetInfo. A dead session reconnects once.
    auto& c = sl->s->client();
    c.setTries(1);
    bool ok = c.getInfo();
    c.setTries(3);
    if (!ok)
      dropLocked(*sl);
    else if (sl->info) {
      sl->info->rpcPort = c.getServerRpcPort();
      sl->info->tps = c.getTps();
      return sl->info;
    }
  }
  if (!client(*sl, dial))
    return std::nullopt;
  return sl->info;
}

OpResult CesOps::peers(const std::string& dial, uint16_t from, uint16_t max,
                       uint16_t& count, std::vector<Peer>& out) {
  auto sl = slot(dial);
  std::lock_guard lk(sl->mu);
  auto* c = client(*sl, dial);
  if (!c)
    return bridgeError("CANNOT_CONNECT");
  count = 0;
  for (uint16_t i = from; i < from + max; ++i) {
    uint16_t n = 0;
    bool found = false;
    minx::Hash pk;
    std::string addr;
    uint8_t rc = c->queryPeerInfo(i, n, found, pk, addr);
    if (rc != ces::CES_OK) {
      if (isTransportFailure(rc))
        dropLocked(*sl);
      return fromCode(rc);
    }
    count = n;
    if (found)
      out.push_back({minx::hashToString(pk), addr});
    if (i + 1 >= n)
      break;
  }
  return fromCode(ces::CES_OK);
}

OpResult CesOps::balance(const std::string& dial, const minx::Hash& pubkey) {
  auto sl = slot(dial);
  std::lock_guard lk(sl->mu);
  auto* c = client(*sl, dial);
  if (!c)
    return bridgeError("CANNOT_CONNECT");
  int64_t bal = 0;
  uint32_t nonce = 0;
  // One short try, since an absent account answers with silence, and a page
  // shouldn't wait 3 x 3 s for that.
  c->setTries(1);
  c->setRetryIntervalMs(1500);
  uint8_t rc = c->queryAccount(ces::Account::getMapKey(pubkey), bal, nonce);
  c->setTries(3);
  c->setRetryIntervalMs(3000);
  if (rc == ces::CES_ERROR_TIMEOUT) {
    // ces answers nothing for an absent account, so a free GetInfo tells a
    // missing account from a dead link.
    if (c->getInfo()) {
      OpResult r;
      r.exists = false;
      return r;
    }
    dropLocked(*sl);
    return fromCode(rc);
  }
  if (isTransportFailure(rc))
    dropLocked(*sl);
  OpResult r = fromCode(rc);
  r.balance = bal;
  // Some servers answer an absent account with a zero balance and nonce, and
  // daily maintenance deletes accounts at zero, so that counts as absent.
  r.exists = rc == ces::CES_OK && (bal != 0 || nonce != 0);
  return r;
}









OpResult CesOps::grant(const minx::Hash& pubkey, uint64_t amount) {
  auto sl = slot(ledgerLocal_);
  std::lock_guard lk(sl->mu);
  auto* c = client(*sl, ledgerLocal_);
  if (!c)
    return bridgeError("CANNOT_CONNECT");
  // The server's own account is bottomless, so this transfer is the grant.
  c->setKey(ledgerKey_);
  int64_t nb = 0;
  uint8_t rc = c->openTransfer(pubkey, amount, nb);
  if (isTransportFailure(rc))
    dropLocked(*sl);
  return fromCode(rc);
}

OpResult CesOps::transfer(const ces::KeyPair& kp, const std::string& dial,
                          const minx::Hash& dest, uint64_t amount) {
  auto sl = slot(dial);
  std::lock_guard lk(sl->mu);
  auto* c = client(*sl, dial);
  if (!c)
    return bridgeError("CANNOT_CONNECT");
  c->setKey(kp);
  int64_t nb = 0;
  uint8_t rc = c->openTransfer(dest, amount, nb);
  if (isTransportFailure(rc))
    dropLocked(*sl);
  OpResult r = fromCode(rc);
  r.balance = nb;
  return r;
}

OpResult CesOps::cross(const ces::KeyPair& kp, const std::string& dial,
                       const minx::Hash& dest, uint64_t amount, const std::string& destServer) {
  auto sl = slot(dial);
  std::lock_guard lk(sl->mu);
  auto* c = client(*sl, dial);
  if (!c)
    return bridgeError("CANNOT_CONNECT");
  c->setKey(kp);
  int64_t nb = 0;
  uint8_t rc = c->crossTransfer(dest, amount, destServer, nb);
  if (isTransportFailure(rc))
    dropLocked(*sl);
  OpResult r = fromCode(rc);
  r.balance = nb;
  return r;
}

} // namespace fc
