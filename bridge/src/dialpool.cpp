#include "dialpool.h"

#include <ces/buffer.h>
#include <ces/cesplex/wire.h>
#include <ces/client.h>
#include <ces/types.h>

#include <minx/minx.h>
#include <minx/rudp/rudp.h>
#include <minx/rudp/rudp_stream.h>
#include <minx/stdext.h>

#include <boost/asio/buffer.hpp>
#include <boost/asio/executor_work_guard.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/address.hpp>
#include <boost/asio/posix/stream_descriptor.hpp>
#include <boost/asio/post.hpp>
#include <boost/asio/read.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/write.hpp>

#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <future>
#include <list>
#include <optional>
#include <random>
#include <span>
#include <thread>
#include <utility>
#include <vector>

namespace fc {

namespace asio = boost::asio;

namespace {

constexpr const char* kLuaProto = "/ces/lua/1";
constexpr uint8_t kVerbAttach = 0x01;
// The bind and the attach together.
constexpr auto kHandshake = std::chrono::seconds(20);
// The RUDP tick while a stream is open, at a CES server's own rpc cadence.
constexpr auto kTick = std::chrono::milliseconds(10);
constexpr size_t kChunk = 4096;
// A hello rides the attach reply, up to this size.
constexpr uint32_t kMaxHello = 1 << 20;

uint64_t nowMicros() {
  return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

struct NoopListener : minx::MinxListener {};

class SendListener : public minx::Rudp::Listener {
public:
  minx::Minx* minx = nullptr;
  void onSend(const minx::SockAddr& peer, const minx::Bytes& bytes) override {
    if (!minx)
      return;
    try {
      minx->sendExtension(peer, bytes);
    } catch (const std::exception&) {
      // The socket closed under a late packet.
    }
  }
};

struct Sock {
  NoopListener listener;
  SendListener sender;
  std::unique_ptr<minx::Minx> minx;
  std::unique_ptr<minx::Rudp> rudp;
  size_t streams = 0;
};

using Result = std::pair<int, std::string>;

} // namespace

struct Pipe;

struct DialPool::Impl {
  using Guard = asio::executor_work_guard<asio::io_context::executor_type>;

  size_t perSocket;
  // Declared before the io contexts, so the sockets go after them.
  std::vector<std::unique_ptr<Sock>> socks;
  std::list<std::weak_ptr<Pipe>> pipes;
  asio::io_context netIO;
  asio::io_context taskIO;
  Guard netGuard{netIO.get_executor()};
  Guard taskGuard{taskIO.get_executor()};
  asio::steady_timer timer{taskIO};
  std::thread netThread;
  std::thread taskThread;
  // Touched only on taskIO.
  size_t open = 0;
  bool ticking = false;
  std::mt19937 rng{std::random_device{}()};
  // Read from any thread.
  std::atomic<size_t> openCount{0};
  std::atomic<size_t> sockCount{0};

  explicit Impl(size_t n) : perSocket(n) {
    netThread = std::thread([this] { netIO.run(); });
    taskThread = std::thread([this] { taskIO.run(); });
  }

  ~Impl();

  Sock* pick(const minx::SockAddr& peer);
  void start(const std::shared_ptr<Pipe>& p);
  void release(Sock* s);
  void startTick();
  void scheduleTick();
};

// One dial: its RUDP stream, and the bridge's end of the socket pair. It
// lives in its own pumps' handlers.
struct Pipe : std::enable_shared_from_this<Pipe> {
  DialPool::Impl* pool = nullptr;
  Sock* sock = nullptr;
  minx::SockAddr peer;
  minx::Hash serverKey{};
  uint64_t pid = 0;
  std::optional<ces::KeyPair> signer;  // until the attach is signed
  uint64_t token = 0;
  std::shared_ptr<std::promise<Result>> result;
  bool settled = false;
  bool closed = false;
  std::shared_ptr<minx::RudpStream> stream;
  std::unique_ptr<asio::posix::stream_descriptor> fd;
  std::array<uint8_t, kChunk> fromProgram{};
  std::array<uint8_t, kChunk> toProgram{};

  void fail(const std::string& why) {
    if (!settled) {
      settled = true;
      result->set_value({-1, why});
    }
    close();
  }

  void close() {
    if (closed)
      return;
    closed = true;
    signer.reset();
    if (stream)
      stream->close();
    if (fd) {
      boost::system::error_code ec;
      fd->close(ec);
    }
    pool->release(sock);
    sock = nullptr;
  }

  // The signed bind on /ces/lua/1, whose reply must carry the server key that
  // the bridge knows.
  void bind() {
    auto self = shared_from_this();
    const uint64_t t = nowMicros();
    const std::string name = kLuaProto;
    auto req = std::make_shared<minx::Bytes>(ces::buildBindRequest(name, t, *signer));
    const auto& pk = signer->getPublicKeyAsHash();
    auto digest = std::make_shared<std::array<uint8_t, ces::CES_PLEX_SHA256_SIZE>>(
        ces::computeBindRequestDigest(
            std::span<const uint8_t>(reinterpret_cast<const uint8_t*>(name.data()), name.size()), t,
            std::span<const uint8_t>(pk.data(), pk.size())));
    asio::async_write(*stream, asio::buffer(*req),
                      [self, req, digest](const boost::system::error_code& ec, size_t) {
      if (ec)
        return self->fail("bind: " + ec.message());
      auto reply = std::make_shared<std::array<uint8_t, ces::CES_PLEX_BIND_REPLY_TOTAL_SIZE>>();
      asio::async_read(*self->stream, asio::buffer(*reply),
                       [self, reply, digest](const boost::system::error_code& ec2, size_t) {
        if (ec2)
          return self->fail("bind: " + ec2.message());
        auto r = ces::parseBindReply(
            std::span<const uint8_t, ces::CES_PLEX_BIND_REPLY_TOTAL_SIZE>(reply->data(), reply->size()));
        if (r.status != ces::CES_PLEX_OK)
          return self->fail("the server refused the bind");
        if (!ces::verifyBindReply(r, std::span<const uint8_t>(digest->data(), digest->size())))
          return self->fail("the bind reply does not verify");
        if (std::memcmp(self->serverKey.data(), r.serverPubkey.data(), self->serverKey.size()) != 0)
          return self->fail("the bind reply has the key of another server");
        self->token = r.channelSessionToken;
        self->attach();
      });
    });
  }

  // The signed ATTACH to the program, whose reply carries the hello.
  void attach() {
    auto self = shared_from_this();
    ces::Bytes preamble;
    ces::Buffer::put<uint64_t>(preamble, pid);
    ces::Signature sig = ces::signPerOp(
        *signer, kVerbAttach, std::span<const uint8_t>(preamble.data(), preamble.size()), token);
    signer.reset();
    const size_t total = ces::CES_PLEX_VERB_SIZE + ces::CES_PLEX_PREAMBLE_LEN_SIZE +
                         preamble.size() + sig.size();
    auto wire = std::make_shared<minx::Bytes>(total);
    minx::Buffer buf(*wire);
    buf.put<uint8_t>(kVerbAttach);
    buf.put<uint32_t>(static_cast<uint32_t>(preamble.size()));
    buf.put(std::span<const uint8_t>(preamble.data(), preamble.size()));
    buf.put(sig);
    asio::async_write(*stream, asio::buffer(*wire),
                      [self, wire](const boost::system::error_code& ec, size_t) {
      if (ec)
        return self->fail("attach: " + ec.message());
      auto st = std::make_shared<std::array<uint8_t, 1>>();
      asio::async_read(*self->stream, asio::buffer(*st),
                       [self, st](const boost::system::error_code& ec2, size_t) {
        if (ec2)
          return self->fail("attach: " + ec2.message());
        if ((*st)[0] != ces::CES_OK)
          return self->fail(ces::errorString((*st)[0]));
        // [conn id u64][hello length u32], then the hello and the trailer.
        auto head = std::make_shared<std::array<uint8_t, 12>>();
        asio::async_read(*self->stream, asio::buffer(*head),
                         [self, head](const boost::system::error_code& ec3, size_t) {
          if (ec3)
            return self->fail("attach: " + ec3.message());
          const uint32_t helloLen =
              ces::Buffer::peek<uint32_t>(std::span<const uint8_t>(head->data(), head->size()), 8);
          if (helloLen > kMaxHello)
            return self->fail("the hello of the program is too big");
          auto rest = std::make_shared<ces::Bytes>(static_cast<size_t>(helloLen) +
                                                   ces::CES_PLEX_RESP_TRAILER_SIZE);
          asio::async_read(*self->stream, asio::buffer(*rest),
                           [self, rest, helloLen](const boost::system::error_code& ec4, size_t) {
            if (ec4)
              return self->fail("attach: " + ec4.message());
            self->open(std::string(reinterpret_cast<const char*>(rest->data()), helloLen));
          });
        });
      });
    });
  }

  // The program answered, so the window gets one end of a socket pair, with
  // the hello first on it.
  void open(std::string hello) {
    int sv[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sv) != 0)
      return fail(std::string("socketpair: ") + std::strerror(errno));
    fd = std::make_unique<asio::posix::stream_descriptor>(pool->taskIO, sv[0]);
    settled = true;
    result->set_value({sv[1], ""});
    auto self = shared_from_this();
    auto h = std::make_shared<std::string>(std::move(hello));
    asio::async_write(*fd, asio::buffer(*h), [self, h](const boost::system::error_code& ec, size_t) {
      if (ec)
        return self->close();
      self->pumpOut();
    });
    pumpIn();
  }

  // The program's bytes, to the window. The next read waits for the write, so
  // a slow window slows the stream and nothing piles up here.
  void pumpOut() {
    auto self = shared_from_this();
    stream->async_read_some(asio::buffer(fromProgram),
                            [self](const boost::system::error_code& ec, size_t n) {
      if (ec)
        return self->close();
      asio::async_write(*self->fd, asio::buffer(self->fromProgram, n),
                        [self](const boost::system::error_code& ec2, size_t) {
        if (ec2)
          return self->close();
        self->pumpOut();
      });
    });
  }

  void pumpIn() {
    auto self = shared_from_this();
    fd->async_read_some(asio::buffer(toProgram), [self](const boost::system::error_code& ec, size_t n) {
      if (ec)
        return self->close();
      asio::async_write(*self->stream, asio::buffer(self->toProgram, n),
                        [self](const boost::system::error_code& ec2, size_t) {
        if (ec2)
          return self->close();
        self->pumpIn();
      });
    });
  }
};

DialPool::Impl::~Impl() {
  // Close every stream and socket on the task thread, stop the threads, and
  // only then free the sockets.
  std::promise<void> closed;
  asio::post(taskIO, [this, &closed] {
    timer.cancel();
    for (auto& w : pipes)
      if (auto p = w.lock())
        p->close();
    for (auto& s : socks)
      s->minx->closeSocket(false);
    closed.set_value();
  });
  closed.get_future().wait_for(std::chrono::seconds(5));
  netGuard.reset();
  taskGuard.reset();
  netIO.stop();
  taskIO.stop();
  if (netThread.joinable())
    netThread.join();
  if (taskThread.joinable())
    taskThread.join();
}

Sock* DialPool::Impl::pick(const minx::SockAddr& peer) {
  for (auto& s : socks)
    if (s->rudp->channelCount(peer) < perSocket)
      return s.get();
  auto s = std::make_unique<Sock>();
  minx::MinxConfig mc = ces::CesClient::clientConfig("fc-dial");
  mc.randomXVMsToKeep = 0;
  mc.randomXInitThreads = 0;
  mc.trustLoopback = true;
  s->minx = std::make_unique<minx::Minx>(&s->listener, mc);
  minx::RudpConfig rc{};
  rc.maxChannelsPerPeer = perSocket;
  rc.baseTickInterval = std::chrono::milliseconds(1);
  s->sender.minx = s->minx.get();
  s->rudp = std::make_unique<minx::Rudp>(&s->sender, rc);
  minx::Rudp* rudp = s->rudp.get();
  minx::MinxStdExtensions ext;
  ext.registerExtension(minx::Rudp::KEY_V0,
                        [rudp](const minx::SockAddr& from, uint64_t key, const minx::Bytes& payload) {
                          rudp->onPacket(from, key, payload, nowMicros());
                        });
  s->minx->setExtensionHandler(std::move(ext).build());
  if (s->minx->openSocket(asio::ip::address_v6::any(), 0, netIO, taskIO) == 0)
    return nullptr;
  socks.push_back(std::move(s));
  sockCount = socks.size();
  return socks.back().get();
}

void DialPool::Impl::start(const std::shared_ptr<Pipe>& p) {
  Sock* s = pick(p->peer);
  if (!s)
    return p->fail("cannot open a UDP socket");
  s->rudp->tick(nowMicros());
  p->stream = std::make_shared<minx::RudpStream>(taskIO.get_executor());
  uint32_t channel = 0;
  for (int i = 0; i < 8 && channel == 0; ++i) {
    uint32_t c = rng();
    if (c != 0 && s->rudp->registerChannel(p->peer, c, p->stream))
      channel = c;
  }
  if (channel == 0)
    return p->fail("no RUDP channel to the server");
  // The server ends an idle channel, and this side keeps it until then.
  s->rudp->setChannelPersistent(p->peer, channel);
  p->sock = s;
  s->streams++;
  open++;
  openCount = open;
  for (auto it = pipes.begin(); it != pipes.end();)
    it = it->expired() ? pipes.erase(it) : std::next(it);
  pipes.push_back(p);
  startTick();
  p->bind();
}

void DialPool::Impl::release(Sock* s) {
  if (!s)
    return;
  if (s->streams > 0)
    s->streams--;
  if (open > 0)
    open--;
  openCount = open;
}

void DialPool::Impl::startTick() {
  if (ticking)
    return;
  ticking = true;
  scheduleTick();
}

// Ticks every socket while a stream is open, and once more after the last
// one closes, so its close goes out.
void DialPool::Impl::scheduleTick() {
  timer.expires_after(kTick);
  timer.async_wait([this](const boost::system::error_code& ec) {
    if (ec) {
      ticking = false;
      return;
    }
    const uint64_t now = nowMicros();
    for (auto& s : socks)
      s->rudp->tick(now);
    if (open == 0) {
      ticking = false;
      return;
    }
    scheduleTick();
  });
}

DialPool::DialPool(size_t perSocket) : impl_(std::make_unique<Impl>(perSocket)) {}

DialPool::~DialPool() = default;

int DialPool::dial(const ces::KeyPair& signer, const minx::SockAddr& rpc,
                   const minx::Hash& serverKey, uint64_t pid, std::string& err) {
  auto p = std::make_shared<Pipe>();
  p->pool = impl_.get();
  p->peer = rpc;
  p->serverKey = serverKey;
  p->pid = pid;
  p->signer.emplace(signer);
  p->result = std::make_shared<std::promise<Result>>();
  auto fut = p->result->get_future();
  asio::post(impl_->taskIO, [p] { p->pool->start(p); });
  if (fut.wait_for(kHandshake) != std::future_status::ready)
    asio::post(impl_->taskIO, [p] {
      if (!p->settled)
        p->fail("the program did not answer");
    });
  auto [fd, why] = fut.get();
  err = why;
  return fd;
}

size_t DialPool::streams() const { return impl_->openCount; }
size_t DialPool::sockets() const { return impl_->sockCount; }

} // namespace fc
