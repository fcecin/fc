// fc miner: it mines to its own key on the site's CES server, and moves the
// credit to the person's account on the site's ledger.
#include "engine.h"
#include "site.h"
#include "window.h"

#include <ces/util/ctrlc.h>

#include <QApplication>
#include <QStandardPaths>
#include <QTimer>

#include <csignal>

namespace {
// The site and the mine server, from the build (FC_SITE and FC_MINE_SERVER
// in CMakeLists.txt).
constexpr const char* SITE = FC_SITE;
constexpr const char* MINE_SERVER = FC_MINE_SERVER;

// ces turns SIGINT and SIGTERM into its interrupt flag, which only stops its
// waits, so the miner quits on them, and a logout or a shutdown ends it.
volatile std::sig_atomic_t g_quit = 0;
extern "C" void onQuit(int) {
  g_quit = 1;
  ces::internal::g_interrupted = 1;
}
}

int main(int argc, char** argv) {
  QApplication app(argc, argv);
  QCoreApplication::setOrganizationName("fc");
  QCoreApplication::setApplicationName("fc miner");
  const QString dir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
  Engine engine(MINE_SERVER, dir + "/miner.key");
  Site site{QUrl(SITE)};
  Window window(&engine, &site, MINE_SERVER);
  window.show();
  std::signal(SIGINT, onQuit);
  std::signal(SIGTERM, onQuit);
  QTimer quit;
  QObject::connect(&quit, &QTimer::timeout, &app, [&app] { if (g_quit) app.quit(); });
  quit.start(250);
  return app.exec();
}
