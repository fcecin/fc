#include "ceshops.h"

#include "ceshd.h"

#include <ces/keys.h>

#include <boost/json.hpp>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>

namespace fc {

namespace json = boost::json;

namespace {

constexpr size_t kMaxMessage = 64 * 1024;
constexpr size_t kMaxArgs = 256;
constexpr size_t kMaxServer = 260;

// One reply message, with `fd` (SCM_RIGHTS) when it is not -1.
void answer(int conn, const std::string& text, int fd = -1) {
  iovec iov{const_cast<char*>(text.data()), text.size()};
  msghdr msg{};
  msg.msg_iov = &iov;
  msg.msg_iovlen = 1;
  alignas(cmsghdr) char ctl[CMSG_SPACE(sizeof(int))];
  if (fd >= 0) {
    std::memset(ctl, 0, sizeof(ctl));
    msg.msg_control = ctl;
    msg.msg_controllen = sizeof(ctl);
    cmsghdr* c = CMSG_FIRSTHDR(&msg);
    c->cmsg_level = SOL_SOCKET;
    c->cmsg_type = SCM_RIGHTS;
    c->cmsg_len = CMSG_LEN(sizeof(int));
    std::memcpy(CMSG_DATA(c), &fd, sizeof(int));
  }
  ::sendmsg(conn, &msg, MSG_NOSIGNAL);
}

// Text for ceshd's wire, where a NUL ends a field.
bool wireText(const std::string& s) {
  return s.find('\0') == std::string::npos;
}

std::string hex(const std::array<uint8_t, 32>& b) {
  static const char* digits = "0123456789abcdef";
  std::string out;
  for (uint8_t x : b) {
    out += digits[x >> 4];
    out += digits[x & 15];
  }
  return out;
}

} // namespace

CeshServer::CeshServer(std::string socketPath, std::string ceshdPath, Unseal unseal,
                       Resolve resolve, DialPool& dials)
    : socketPath_(std::move(socketPath)), ceshdPath_(std::move(ceshdPath)),
      unseal_(std::move(unseal)), resolve_(std::move(resolve)), dials_(dials) {}

void CeshServer::start() {
  int lsn = ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  if (lsn < 0 || socketPath_.size() >= sizeof(addr.sun_path))
    throw std::runtime_error("cannot make the cesh socket " + socketPath_);
  std::memcpy(addr.sun_path, socketPath_.c_str(), socketPath_.size() + 1);
  ::unlink(socketPath_.c_str());
  if (::bind(lsn, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
      ::chmod(socketPath_.c_str(), 0660) != 0 || ::listen(lsn, 64) != 0)
    throw std::runtime_error("cesh socket " + socketPath_ + ": " + std::strerror(errno));
  std::cerr << "fc-bridge: cesh on " << socketPath_ << ", ceshd at " << ceshdPath_ << "\n";
  std::thread([this, lsn] {
    for (;;) {
      int conn = ::accept4(lsn, nullptr, nullptr, SOCK_CLOEXEC);
      if (conn < 0) {
        if (errno == EINTR || errno == ECONNABORTED)
          continue;
        std::cerr << "fc-bridge: cesh accept: " << std::strerror(errno) << "\n";
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        continue;
      }
      std::thread([this, conn] { serve(conn); }).detach();
    }
  }).detach();
}

void CeshServer::serve(int conn) const {
  timeval tv{2, 0};
  ::setsockopt(conn, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
  std::string data(kMaxMessage + 1, '\0');
  alignas(cmsghdr) char ctl[CMSG_SPACE(sizeof(int) * 3)];
  iovec iov{data.data(), data.size()};
  msghdr msg{};
  msg.msg_iov = &iov;
  msg.msg_iovlen = 1;
  msg.msg_control = ctl;
  msg.msg_controllen = sizeof(ctl);
  ssize_t n;
  do {
    n = ::recvmsg(conn, &msg, MSG_CMSG_CLOEXEC);
  } while (n < 0 && errno == EINTR);
  std::vector<int> fds;
  if (n > 0) {
    for (cmsghdr* c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
      if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
        continue;
      size_t count = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
      for (size_t i = 0; i < count; ++i) {
        int fd;
        std::memcpy(&fd, CMSG_DATA(c) + i * sizeof(int), sizeof(int));
        fds.push_back(fd);
      }
    }
  }
  // Send the reply, then close the request's descriptors, since ceshd has
  // its own copies.
  auto done = [&](const std::string& text, int fd = -1) {
    answer(conn, text, fd);
    for (int f : fds)
      ::close(f);
    ::close(conn);
  };
  if (n <= 0)
    return done("error no request");
  if ((msg.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) != 0)
    return done("error the request is too large");
  data.resize(static_cast<size_t>(n));

  ceshd::Request req;
  std::string sealed;
  try {
    json::value v = json::parse(data);
    if (!v.is_object())
      return done("error the request is not a JSON object");
    const json::object& o = v.as_object();
    if (auto* s = o.if_contains("sealed"); s && s->is_string())
      sealed = std::string(s->as_string());
    if (auto* d = o.if_contains("dial")) {
      if (!fds.empty())
        return done("error a dial takes no descriptors");
      auto* s = o.if_contains("server");
      if (!d->is_int64() || d->as_int64() < 0 || !s || !s->is_string())
        return done("error give dial as the pid of a program, and its server");
      for (int f : fds)
        ::close(f);
      fds.clear();
      return dial(conn, sealed, std::string(s->as_string()), static_cast<uint64_t>(d->as_int64()));
    }
    if (fds.size() != 3)
      return done("error a request needs 3 descriptors: stdin, stdout, stderr");
    if (auto* s = o.if_contains("server"); s && s->is_string() && !s->as_string().empty()) {
      std::string server(s->as_string());
      if (server.size() > kMaxServer || !wireText(server))
        return done("error the server is not a host:port");
      req.env["CESH_SERVER"] = server;
    }
    auto* a = o.if_contains("args");
    if (!a || !a->is_array() || a->as_array().size() > kMaxArgs)
      return done("error give args as a list of at most 256 strings");
    for (const auto& x : a->as_array()) {
      if (!x.is_string() || !wireText(std::string(x.as_string())))
        return done("error give args as a list of at most 256 strings");
      req.args.emplace_back(x.as_string());
    }
  } catch (const std::exception&) {
    return done("error the request is not JSON");
  }
  auto secret = unseal_(sealed);
  if (!secret)
    return done("error the key does not open");
  std::string& wallet = req.env["CESH_WALLET"];
  wallet = "00" + hex(*secret);
  secret->fill(0);

  std::string err;
  int c = ceshd::send(ceshdPath_, req, fds[0], fds[1], fds[2], err);
  std::fill(wallet.begin(), wallet.end(), '0');
  if (c < 0)
    return done(err.rfind("error ", 0) == 0 ? err : "error " + err);
  done("ok", c);
  ::close(c);
}

void CeshServer::dial(int conn, const std::string& sealed, const std::string& server,
                      uint64_t pid) const {
  auto done = [conn](const std::string& text, int fd = -1) {
    answer(conn, text, fd);
    ::close(conn);
  };
  if (server.size() > kMaxServer)
    return done("error the server is not a host:port");
  std::string why;
  auto target = resolve_(server, why);
  if (!target)
    return done("error " + why);
  auto secret = unseal_(sealed);
  if (!secret)
    return done("error the key does not open");
  ces::KeyPair key{minx::Hash(*secret)};
  secret->fill(0);
  int fd = dials_.dial(key, target->rpc, target->key, pid, why);
  if (fd < 0)
    return done("error " + why);
  done("ok", fd);
  ::close(fd);
}

} // namespace fc
