#include "engine.h"

#include <ces/account.h>
#include <ces/client.h>
#include <ces/types.h>
#include <ces/util/ctrlc.h>
#include <ces/util/resolver.h>
#include <ces/util/wallet.h>
#include <minx/minx.h>

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QSaveFile>

#include <algorithm>
#include <chrono>

namespace {

constexpr int RETRY_SECS = 30;             // the wait before retrying a silent server
constexpr int NO_ENGINE_SECS = 300;        // the wait while the server isn't mining
constexpr qint64 MIN_RESERVE = 1'000'000;  // raw units that stay in the miner's key (0.01 credit)
constexpr qint64 FEE_TX = 320'000;         // a transfer's fee, until the server gives its own

// The miner's private key from its file, or a new one in a new file that only
// its owner reads.
ces::KeyPair loadKey(const QString& path) {
  QFile f(path);
  if (f.open(QIODevice::ReadOnly)) {
    const QByteArray hex = f.readAll().trimmed();
    if (hex.size() == 64)
      return ces::KeyPair(hex.toStdString());
  }
  ces::KeyPair k;
  QDir().mkpath(QFileInfo(path).absolutePath());
  QSaveFile out(path);
  if (out.open(QIODevice::WriteOnly)) {
    out.write(QByteArray::fromStdString(k.getPrivateKeyHexStr()) + "\n");
    out.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    out.commit();
  }
  return k;
}

bool hexToHash(const QString& hex, minx::Hash& out) {
  const QByteArray b = QByteArray::fromHex(hex.toLatin1());
  if (hex.size() != 64 || b.size() != 32)
    return false;
  std::copy(b.begin(), b.end(), out.begin());
  return true;
}

QString credit(qint64 raw) {
  return QString::number(static_cast<double>(raw) / 1e8, 'f', 8).remove(QRegularExpression("0+$")).remove(QRegularExpression("\\.$"));
}

}  // namespace

Probe probe(const QString& server) {
  Probe p;
  try {
    ces::CesClient client(ces::Resolver::resolveUdp(server.toStdString()), false);
    client.start(0);
    client.setKey(ces::KeyPair());   // a throwaway key, since a handshake needs one
    client.setTries(2);
    if (client.connect()) {
      p.up = true;
      p.serverKey = QString::fromStdString(minx::hashToString(client.getServerKey()));
      p.rpcPort = client.getServerRpcPort();
      p.minDifficulty = client.getMinDifficulty();
      p.powEngine = client.getServerPowEngine();
    } else {
      p.why = "it does not answer";
    }
    client.stop();
  } catch (const std::exception& e) {
    p.why = QString("its name does not resolve: %1").arg(e.what());
  }
  return p;
}

Engine::Engine(QString server, QString keyFile, QObject* parent)
    : QObject(parent), key_(loadKey(keyFile)) {
  world_.server = std::move(server);
  world_.keyFile = std::move(keyFile);
  world_.minerKey = QString::fromStdString(key_.getPublicKeyHexStr());
  world_.reserve = MIN_RESERVE;
}

Engine::~Engine() { stop(); }

void Engine::start(int threads) {
  if (thread_.joinable())
    return;
  threads_ = std::max(0, threads);
  quit_ = false;
  ces::internal::g_interrupted = 0;
  edit([&](World& w) { w.since = QDateTime::currentDateTime(); w.threads = threads_; });
  thread_ = std::thread([this] { run(); });
}

void Engine::stop() {
  quit_ = true;
  ces::internal::g_interrupted = 1;   // ends ces's search, waits and requests
  if (thread_.joinable())
    thread_.join();
}

World Engine::world() const {
  std::lock_guard<std::mutex> lk(mu_);
  return world_;
}

void Engine::setThreads(int n) {
  n = std::max(0, n);
  const int old = threads_.exchange(n);
  if (old == n)
    return;
  edit([&](World& w) { w.threads = n; });
  // A running search ends and restarts with n threads, or pauses. A pause
  // ends by itself, since an interrupt then would end the next search.
  if (old > 0) {
    again_ = true;
    ces::internal::g_interrupted = 1;
  }
}

void Engine::setHome(bool on, const QString& handle, const QString& personKey, const QString& ledger) {
  edit([&](World& w) {
    w.toHome = on;
    w.handle = handle;
    w.personKey = personKey;
    w.ledger = ledger;
  });
}

void Engine::doing(const QString& what) {
  edit([&](World& w) { w.doing = what; });
}

void Engine::problem(const QString& what) {
  edit([&](World& w) { w.problem = what; });
  say(what);
}

void Engine::say(const QString& line) { emit said(line); }

bool Engine::pause(int secs) {
  for (int i = 0; i < secs * 10 && !quit_; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  return !quit_;
}

// The mine server, as its handshake describes it.
bool Engine::connectTo(ces::CesClient& client) {
  client.start(0);
  client.setKey(key_);
  client.setTries(3);
  if (!client.connect())
    return false;
  edit([&](World& w) {
    w.connected = true;
    w.serverKey = QString::fromStdString(minx::hashToString(client.getServerKey()));
    w.rpcPort = client.getServerRpcPort();
    w.minDifficulty = client.getMinDifficulty();
    w.minSecsPow = client.getMinSecsPoW();
    w.pendingPows = client.getPendingPoWs();
    w.powEngine = client.getServerPowEngine();
  });
  return true;
}

void Engine::readAccount(ces::CesClient& client) {
  int64_t balance = 0;
  uint32_t nonce = 0;
  if (client.queryAccount(ces::Account::getMapKey(key_.getPublicKeyAsHash()), balance, nonce) != ces::CES_OK)
    return;
  edit([&](World& w) {
    w.balance = balance;
    w.account = balance != 0 || nonce != 0;
  });
}

// The server's own list of fees and more, which costs a query fee, so the
// miner asks once per run, after its key has credit.
void Engine::readServerInfo(ces::CesClient& client) {
  if (serverInfoRead_ || world().balance <= MIN_RESERVE)
    return;
  std::vector<ces::ServerInfoEntry> entries;
  if (client.queryServerInfo(entries) != ces::CES_OK)
    return;
  serverInfoRead_ = true;
  qint64 feeTx = FEE_TX;
  QList<QPair<QString, QString>> list;
  for (const auto& e : entries) {
    list.append({QString::fromStdString(e.key), QString::fromStdString(e.value)});
    if (e.key == "fee_tx")
      feeTx = QString::fromStdString(e.value).toLongLong();
  }
  edit([&](World& w) {
    w.serverInfo = list;
    w.reserve = std::max(MIN_RESERVE, 3 * feeTx);
  });
}

// Move what is above the reserve to the person's key on the ledger, when the
// person wants it there and the site said where.
void Engine::moveHome(ces::CesClient& client) {
  const World w = world();
  minx::Hash dest{};
  if (!w.toHome || w.ledger.isEmpty() || !hexToHash(w.personKey, dest) || w.balance <= w.reserve)
    return;
  const qint64 amount = w.balance - w.reserve;
  doing("moving " + credit(amount) + " credit home");
  int64_t left = 0;
  const uint8_t rc = client.crossTransfer(dest, static_cast<uint64_t>(amount), w.ledger.toStdString(), left);
  if (rc != ces::CES_OK) {
    problem(QString("the move home failed: %1; the credit stays in the miner's key").arg(ces::errorString(rc)));
    return;
  }
  edit([&](World& x) {
    x.balance = left;
    x.moved += amount;
    x.moves += 1;
    x.lastMove = QDateTime::currentDateTime();
    x.problem.clear();
  });
  say(QString("moved %1 credit home to %2 (%3)").arg(credit(amount), w.handle, w.ledger));
}

void Engine::run() {
  while (!quit_) {
    ces::internal::g_interrupted = 0;
    serverInfoRead_ = false;
    edit([](World& w) { w.connected = false; });
    doing("connecting to " + world_.server);
    boost::asio::ip::udp::endpoint ep;
    try {
      ep = ces::Resolver::resolveUdp(world_.server.toStdString());
    } catch (const std::exception& e) {
      problem(QString("the name of the server does not resolve: %1").arg(e.what()));
      doing("waiting to try again");
      if (!pause(RETRY_SECS)) break;
      continue;
    }
    ces::CesClient client(ep, true);
    if (!connectTo(client)) {
      client.stop();
      if (quit_) break;
      problem("the server does not answer");
      doing("waiting to try again");
      if (!pause(RETRY_SECS)) break;
      continue;
    }
    say("connected to " + world_.server);
    if (client.getServerPowEngine() != 1) {
      client.stop();
      problem("the server does not take mining now");
      doing("waiting for the server to mine");
      if (!pause(NO_ENGINE_SECS)) break;
      continue;
    }
    readAccount(client);
    readServerInfo(client);
    moveHome(client);   // what an earlier run mined and couldn't move

    int failures = 0;
    bool first = true;
    while (!quit_ && failures < 3) {
      if (threads_ == 0) {              // paused, with the connection kept
        doing("paused");
        edit([](World& w) { w.rate = 0; });
        while (threads_ == 0 && !quit_)
          std::this_thread::sleep_for(std::chrono::milliseconds(200));
        again_ = false;
        ces::internal::g_interrupted = 0;
        continue;
      }
      const int threads = threads_;
      doing(first ? "preparing the proof-of-work engine (the first time takes a while)" : "mining");
      auto t0 = std::chrono::steady_clock::now();
      bool started = false;
      uint64_t h0 = 0;
      auto progress = [&](uint64_t hashes) {
        auto now = std::chrono::steady_clock::now();
        if (!started) {
          started = true;
          first = false;
          h0 = hashes;
          t0 = now;
          doing("mining");
        }
        const double secs = std::chrono::duration<double>(now - t0).count();
        edit([&](World& w) {
          w.allHashes += hashes - w.hashes;
          w.hashes = hashes;
          w.rate = secs > 0 ? (hashes - h0) / secs : 0;
        });
      };
      auto submitted = [&](int) { doing("the server checks a solution"); };
      edit([](World& w) { w.hashes = 0; w.rate = 0; });
      const ces::MineResult m = ces::mineOnce(client, 0, {}, threads, submitted, progress, 256ULL * threads);
      if (quit_)
        break;
      if (again_) {                     // a new number of threads
        again_ = false;
        ces::internal::g_interrupted = 0;
        if (threads_ > 0)
          say(QString("mining with %1 threads").arg(threads_.load()));
        else
          say("paused");
        continue;
      }
      if (m.success) {
        failures = 0;
        const qint64 before = world().balance < 0 ? 0 : world().balance;
        edit([&](World& w) {
          w.solutions += 1;
          w.mined += static_cast<qint64>(m.credit);
          w.lastSolution = QDateTime::currentDateTime();
          w.problem.clear();
        });
        // The credit lands a moment later, so read until it shows.
        for (int i = 0; i < 20 && !quit_; ++i) {
          readAccount(client);
          if (world().balance != before) break;
          std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
        say(QString("solution %1 accepted: +%2 credit").arg(world().solutions).arg(credit(static_cast<qint64>(m.credit))));
        readServerInfo(client);
        moveHome(client);
      } else if (m.status == minx::MINX_SOLUTION_UNTIMELY) {
        edit([](World& w) { w.rejected += 1; });
        say("the server did not take a solution in time; next one");
      } else if (m.status == minx::MINX_SOLUTION_UNKNOWN) {
        edit([](World& w) { w.rejected += 1; });
        say("the server did not confirm a solution; next one");
      } else {
        ++failures;
        problem("mining failed with this server");
      }
    }
    client.stop();
    edit([](World& w) { w.connected = false; w.rate = 0; });
    if (!quit_) {
      doing("waiting to connect again");
      if (!pause(RETRY_SECS)) break;
    }
  }
  doing("stopped");
}
