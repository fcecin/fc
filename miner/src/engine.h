// The miner's engine: one thread that mines to the miner's key on the mine
// server (MINE_SERVER), and moves the credit to the person's key on the
// site's ledger. Everything that it knows is in a World, which the window
// reads at every change.
#pragma once

#include <QDateTime>
#include <QList>
#include <QObject>
#include <QPair>
#include <QString>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <thread>

#include <ces/keys.h>

namespace ces { class CesClient; }

struct World {
  // What the engine does now, and what went wrong last.
  QString doing = "starting";
  QString problem;
  QDateTime since;
  // The mine server, as it describes itself.
  QString server;
  bool connected = false;
  QString serverKey;
  int rpcPort = 0;
  int minDifficulty = -1;
  int minSecsPow = -1;
  int pendingPows = -1;
  int powEngine = -1;                         // 1 yes, 0 no, -1 unknown
  QList<QPair<QString, QString>> serverInfo;  // its own list of fees and more
  // The miner's key, and its account on that server.
  QString minerKey;
  QString keyFile;
  bool account = false;
  qint64 balance = -1;                        // raw units, or -1 when unknown
  // The work.
  int threads = 1;
  double rate = 0;                            // this solution's hashes per second
  quint64 hashes = 0;                         // this solution's hashes
  quint64 allHashes = 0;
  int solutions = 0;
  int rejected = 0;
  qint64 mined = 0;                           // raw units, as the server credited them
  QDateTime lastSolution;
  // Home: the person's account on the site's ledger.
  bool toHome = false;
  QString handle;
  QString personKey;                          // as the site gave it, or empty when unknown
  QString ledger;                             // host:port, as the site gave it
  qint64 reserve = 0;                         // raw units that stay in the miner's key
  qint64 moved = 0;                           // raw units that went home
  int moves = 0;
  QDateTime lastMove;
};

// What a handshake with a CES server says. probe() blocks, so run it off the
// window's thread.
struct Probe {
  bool up = false;
  QString why;           // why it is down
  QString serverKey;
  int rpcPort = 0;
  int minDifficulty = -1;
  int powEngine = -1;
};
Probe probe(const QString& server);

class Engine : public QObject {
  Q_OBJECT

public:
  // `server` is host:port, and `keyFile` holds the miner's private key, which
  // the engine makes when the file is missing.
  Engine(QString server, QString keyFile, QObject* parent = nullptr);
  ~Engine() override;

  void start(int threads);
  void stop();
  World world() const;
  // The number of hashing threads, which restarts the search, and 0 pauses
  // the mining.
  void setThreads(int n);
  // Where the credit goes: the person's key on the ledger, or, when off or
  // unknown, the miner's key on its server.
  void setHome(bool on, const QString& handle, const QString& personKey, const QString& ledger);

signals:
  void changed();
  void said(const QString& line);   // one line for the window's log

private:
  void run();
  bool connectTo(ces::CesClient& client);
  void readAccount(ces::CesClient& client);
  void readServerInfo(ces::CesClient& client);
  void moveHome(ces::CesClient& client);
  bool pause(int secs);             // false when the engine stops
  void doing(const QString& what);
  void problem(const QString& what);
  void say(const QString& line);
  template <typename F> void edit(F f) {
    { std::lock_guard<std::mutex> lk(mu_); f(world_); }
    emit changed();
  }

  ces::KeyPair key_;
  mutable std::mutex mu_;
  World world_;
  std::thread thread_;
  std::atomic<bool> quit_{false};
  std::atomic<bool> again_{false};  // a new thread count, so search again
  std::atomic<int> threads_{1};
  bool serverInfoRead_ = false;
};
