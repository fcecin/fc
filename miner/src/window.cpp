#include "window.h"

#include "engine.h"
#include "site.h"

#include <QDateTime>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPainter>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QSettings>
#include <QStackedWidget>
#include <QStyle>
#include <QThread>
#include <QVBoxLayout>

#include <algorithm>
#include <cstdio>

namespace {

constexpr int WIDTH = 380;
constexpr int RETRY_MS = 60'000;   // the wait before a failed lookup retries
constexpr int LOG_LINES = 300;
constexpr int HANDLE_HEIGHT = 40;  // the handle row, as tall as the keys
constexpr int DETAILS_HEIGHT = 190; // the open details panel, where the events or facts scroll

const char* STYLE = R"(
QWidget#root { background: #0c0916; }
QWidget { color: #d9d2f5; background: transparent; }
QFrame#chassis { border: 1px solid #000; border-radius: 12px;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3d3756, stop:0.03 #2b263f, stop:1 #16132a); }
QFrame#panel { border: 1px solid #000; border-radius: 10px;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2b263f, stop:1 #191530); }
QLabel#brand { font-family: Georgia, serif; font-style: italic; font-weight: 800; font-size: 20px; }
QLabel#grip { color: #4a4466; font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-size: 9px; letter-spacing: 1px; }
QPushButton#key { font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-weight: 800; font-size: 12px; letter-spacing: 2px; color: #d9d2f5;
  padding: 10px 4px; border-radius: 7px; border: 1px solid #07050d; border-top-color: #6e6894;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #4c4669, stop:1 #29243f); }
QPushButton#key:hover { color: #ffffff; }
QPushButton#key:pressed, QPushButton#key:checked { border-top-color: #07050d; border-bottom-color: #4c4669;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1b1730, stop:1 #2a2541); color: #ffffff; }
QPushButton#small { font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-size: 11px; color: #8f84bb; border: none; padding: 2px 4px; }
QPushButton#small:hover { color: #ffb347; }
QLabel#handleSet { color: #fffaf3; font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-weight: 700;
  font-size: 17px; padding-left: 4px; }
QLabel#handleSet:disabled { color: #6e6894; }
QLineEdit#lcdInput:disabled { color: #2c6a5a; }
QLineEdit#lcdInput { background: #031210; border: 1px solid #000; border-radius: 6px; color: #60ffd0;
  font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-weight: 700; font-size: 16px; padding: 7px 9px;
  selection-background-color: #60ffd0; selection-color: #031210; }
QPlainTextEdit#lcdList { background: #031210; border: 1px solid #000; border-radius: 6px; color: #60ffd0;
  font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-size: 11px; }
QLabel#facts { background: #031210; padding: 6px; }
QScrollArea#factsScroll { background: #031210; border: 1px solid #000; border-radius: 6px; }
QPushButton#tab { font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-size: 11px; color: #6e6894;
  border: none; padding: 2px 6px; }
QPushButton#tab[on="true"] { color: #ffb347; }
QPushButton#tab:hover { color: #fffaf3; }
QToolTip { background: #fffaf3; color: #251a47; border: 1px solid #251a47; padding: 4px; }
)";

QString credit(qint64 raw) {
  if (raw < 0)
    return "-";
  QString t = QString::number(static_cast<double>(raw) / 1e8, 'f', 8);
  t.remove(QRegularExpression("0+$"));
  t.remove(QRegularExpression("\\.$"));
  return t;
}

QString span(qint64 secs) {
  if (secs < 60) return QString("%1s").arg(secs);
  if (secs < 3600) return QString("%1m%2s").arg(secs / 60).arg(secs % 60, 2, 10, QChar('0'));
  if (secs < 86400) return QString("%1h%2m").arg(secs / 3600).arg(secs % 3600 / 60, 2, 10, QChar('0'));
  return QString("%1d%2h").arg(secs / 86400).arg(secs % 86400 / 3600);
}

QString when(const QDateTime& t) {
  return t.isValid() ? t.toString("HH:mm:ss") + " (" + span(t.secsTo(QDateTime::currentDateTime())) + " ago)" : "-";
}

QString rate(double r) {
  if (r <= 0) return "0 H/S";
  return r < 1000 ? QString::number(r, 'f', 0) + " H/S" : QString::number(r / 1000, 'f', 2) + " KH/S";
}

QString shortKey(const QString& k) { return k.size() == 64 ? k.left(8) + "…" + k.right(8) : k; }

QPushButton* key(const QString& text, bool checkable, const QString& tip) {
  auto* b = new QPushButton(text);
  b->setObjectName("key");
  b->setCheckable(checkable);
  b->setCursor(Qt::PointingHandCursor);
  b->setToolTip(tip);
  return b;
}

QPushButton* small(const QString& text) {
  auto* b = new QPushButton(text);
  b->setObjectName("small");
  b->setCursor(Qt::PointingHandCursor);
  return b;
}

}  // namespace

Window::Window(Engine* engine, Site* site, QString mineServer, QWidget* parent)
    : QWidget(parent), engine_(engine), site_(site), mineServer_(std::move(mineServer)) {
  setObjectName("root");
  setWindowTitle("fc miner");
  setWindowIcon(QIcon(":/fc-miner.png"));
  setStyleSheet(STYLE);
  setAttribute(Qt::WA_StyledBackground);

  auto* top = new QVBoxLayout(this);
  top->setContentsMargins(10, 10, 10, 10);
  top->setSpacing(8);
  top->setSizeConstraint(QLayout::SetFixedSize);

  auto* chassis = new QFrame;
  chassis->setObjectName("chassis");
  chassis->setFixedWidth(WIDTH - 20);
  auto* cv = new QVBoxLayout(chassis);
  cv->setContentsMargins(12, 8, 12, 12);
  cv->setSpacing(8);
  auto* head = new QHBoxLayout;
  auto* brand = new QLabel("<span style='color:#e2337f'>f</span><span style='color:#5fe0e0'>c</span>"
                           "<span style='font-family:DejaVu Sans Mono,Menlo,Consolas,monospace;font-style:normal;font-weight:700;font-size:11px;"
                           "color:#8f84bb;letter-spacing:3px'>&nbsp;&nbsp;MINER</span>");
  brand->setObjectName("brand");
  head->addWidget(brand);
  head->addStretch(1);
  auto* grip = new QLabel("||||||||||||||||||||");
  grip->setObjectName("grip");
  head->addWidget(grip);
  cv->addLayout(head);

  lcd_ = new Lcd;
  cv->addWidget(lcd_);

  // The handle row, where Enter looks the handle up at any time.
  askRow_ = new QFrame;
  auto* ar = new QHBoxLayout(askRow_);
  ar->setContentsMargins(0, 0, 0, 0);
  handle_ = new QLineEdit;
  handle_->setObjectName("lcdInput");
  handle_->setPlaceholderText("your handle");
  handle_->setMaxLength(32);
  handleSet_ = new QLabel;
  handleSet_->setObjectName("handleSet");
  handleSlot_ = new QStackedWidget;
  handleSlot_->addWidget(handle_);
  handleSlot_->addWidget(handleSet_);
  handleSlot_->setFixedHeight(HANDLE_HEIGHT);   // a size hint here would come before the style
  okKey_ = key("OK", false, "Look up the handle on " + site_->base().host() + ".");
  okKey_->setFixedSize(64, HANDLE_HEIGHT);
  ar->addWidget(handleSlot_, 1);
  ar->addWidget(okKey_);
  cv->addWidget(askRow_);

  // The threads: half of the hardware threads, or all of them with TURBO.
  cores_ = std::max(1, QThread::idealThreadCount());   // 1 when the count is unknown
  threads_ = std::max(1, cores_ / 2);
  const QVariant forced = QSettings().value("threads");   // only in the settings file
  if (forced.isValid())
    threads_ = std::clamp(forced.toInt(), 0, cores_);
  auto* keys = new QHBoxLayout;
  keys->setSpacing(8);
  turbo_ = key("TURBO", true, QString("One mining thread for each of the %1 hardware threads of this computer. "
                                      "It mines faster, and leaves little for you.").arg(cores_));
  pause_ = key("PAUSE", true, "Stop mining for now. The miner goes on when you press it again.");
  toHome_ = key("HOME", true, "Mine to fc account. When off, what the miner mines stays in its own key, at "
                                  + mineServer_ + ".");
  homeOn_ = QSettings().value("home", true).toBool();

  keys->addWidget(turbo_);
  keys->addWidget(pause_);
  keys->addWidget(toHome_);
  cv->addLayout(keys);
  top->addWidget(chassis);

  // The details panel: the event log, or everything that the miner knows,
  // each scrolling in one fixed height.
  auto* panel = new QFrame;
  panel->setObjectName("panel");
  panel->setFixedWidth(WIDTH - 20);
  auto* pv = new QVBoxLayout(panel);
  pv->setContentsMargins(10, 6, 10, 8);
  auto* ph = new QHBoxLayout;
  detailsKey_ = small("▸ DETAILS");
  detailsKey_->setCheckable(true);
  eventsTab_ = small("EVENTS");
  factsTab_ = small("FACTS");
  eventsTab_->setObjectName("tab");
  factsTab_->setObjectName("tab");
  ph->addWidget(detailsKey_);
  ph->addStretch(1);
  ph->addWidget(eventsTab_);
  ph->addWidget(factsTab_);
  pv->addLayout(ph);
  details_ = new QFrame;
  auto* ev = new QVBoxLayout(details_);
  ev->setContentsMargins(0, 0, 0, 0);
  detailPages_ = new QStackedWidget;
  detailPages_->setFixedHeight(DETAILS_HEIGHT);
  log_ = new QPlainTextEdit;
  log_->setObjectName("lcdList");
  log_->setReadOnly(true);
  log_->setMaximumBlockCount(LOG_LINES);
  detailPages_->addWidget(log_);
  facts_ = new QLabel;
  facts_->setObjectName("facts");
  facts_->setTextFormat(Qt::RichText);
  facts_->setTextInteractionFlags(Qt::TextSelectableByMouse);
  facts_->setWordWrap(true);
  facts_->setAlignment(Qt::AlignLeft | Qt::AlignTop);
  auto* factsScroll = new QScrollArea;
  factsScroll->setObjectName("factsScroll");
  factsScroll->setWidgetResizable(true);
  factsScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  factsScroll->setWidget(facts_);
  detailPages_->addWidget(factsScroll);
  ev->addWidget(detailPages_);
  details_->setVisible(false);
  eventsTab_->setVisible(false);
  factsTab_->setVisible(false);
  pv->addWidget(details_);
  top->addWidget(panel);


  auto submit = [this] {
    if (!homeOn_) {                   // EDIT with HOME off turns HOME on and asks
      setHome(true);
      return;
    }
    if (!editing_) {                  // EDIT opens the handle again
      setEditing(true);
      return;
    }
    const QString h = handle_->text().trimmed().toLower();
    if (h.isEmpty()) {                // with no handle, the credit stays in the miner's key
      QSettings().remove("handle");
      key_.clear();
      setHome(false);
      return;
    }
    lookUp(h);
  };
  connect(handle_, &QLineEdit::returnPressed, this, [this, submit] { if (editing_) submit(); });
  connect(okKey_, &QPushButton::clicked, this, submit);
  // A key flips what the person wants, and sync() then shows the state.
  connect(turbo_, &QPushButton::clicked, this, [this] {
    turboOn_ = !turboOn_;
    log(turboOn_ ? QString("TURBO: %1 threads").arg(cores_) : QString("%1 threads").arg(threads_));
    sync();
  });
  connect(pause_, &QPushButton::clicked, this, [this] {
    pauseOn_ = !pauseOn_;
    if (!pauseOn_ && editing_ && homeOn_)
      say_ = "the mining waits for the handle: type it and press enter";
    log(pauseOn_ ? "paused" : "pause off");
    sync();
  });
  connect(toHome_, &QPushButton::clicked, this, [this] { setHome(!homeOn_); });
  connect(detailsKey_, &QPushButton::toggled, this, [this](bool on) {
    detailsKey_->setText(on ? "▾ DETAILS" : "▸ DETAILS");
    details_->setVisible(on);
    eventsTab_->setVisible(on);
    factsTab_->setVisible(on);
    relayout();
  });
  auto tab = [this](int page) {
    detailPages_->setCurrentIndex(page);
    eventsTab_->setProperty("on", page == 0);
    factsTab_->setProperty("on", page == 1);
    for (auto* t : {eventsTab_, factsTab_}) {
      t->style()->unpolish(t);
      t->style()->polish(t);
    }
    fill();
  };
  connect(eventsTab_, &QPushButton::clicked, this, [tab] { tab(0); });
  connect(factsTab_, &QPushButton::clicked, this, [tab] { tab(1); });
  tab(0);

  connect(engine_, &Engine::changed, this, &Window::fill, Qt::QueuedConnection);
  connect(engine_, &Engine::said, this, &Window::log, Qt::QueuedConnection);
  connect(site_, &Site::scanned, this, [this](bool up, const QString& ledger, const QString& why) {
    if (!up) {
      siteLed_ = Lcd::Bad;
      homeLed_ = Lcd::Bad;
      log("the site does not answer: " + why);
      looked();
      fill();
      return;
    }
    siteLed_ = Lcd::On;
    ledger_ = ledger;
    log(site_->base().host() + " answers; home is " + ledger);
    probe(ledger, &homeLed_);
    fill();
  });
  connect(site_, &Site::found, this, [this](const QString& handle, const QString& key, const QString& ledger) {
    retry_.stop();
    key_ = key;
    ledger_ = ledger;
    QSettings().setValue("handle", handle);
    log("key found: " + handle + " is " + shortKey(key));
    say_ = "key found " + shortKey(key);
    setEditing(false);
    home();
    if (phase_ == Asking)
      QTimer::singleShot(1200, this, &Window::mine);
    fill();
  });
  connect(site_, &Site::failed, this, [this](const QString& handle, const QString& why) {
    log(why);
    if (why.startsWith("the site has no handle")) {
      say_ = why;
      setEditing(true);
      fill();
      return;
    }
    // The site is down, so mine now, and the credit waits in this miner's key.
    say_ = "the site does not answer: the credit waits in this miner's key";
    QSettings().setValue("handle", handle);
    setEditing(false);
    home();
    retry_.start(RETRY_MS);
    if (phase_ == Asking)
      QTimer::singleShot(1800, this, &Window::mine);
    fill();
  });
  retry_.setSingleShot(true);
  connect(&retry_, &QTimer::timeout, this, [this] {
    if (key_.isEmpty() && homeOn_ && !handle_->text().trimmed().isEmpty())
      lookUp(handle_->text().trimmed().toLower());
  });
  connect(&clock_, &QTimer::timeout, this, [this] {
    if (phase_ == Mining) {
      const quint64 all = engine_->world().allHashes;
      lcd_->sample(all >= lastHashes_ ? static_cast<double>(all - lastHashes_) : 0);
      lastHashes_ = all;
    }
    fill();
  });
  clock_.start(1000);

  log("this miner's key is " + shortKey(engine_->world().minerKey) + ", in " + engine_->world().keyFile);
  say_ = "looking at the fc site, home and the mine server";
  handle_->setText(QSettings().value("handle").toString());
  setEditing(handle_->text().isEmpty());
  site_->scan();
  probe(mineServer_, &mineLed_);
  sync();
}

// A part showed or went, so the window takes its new size now. Under Wayland,
// a fixed-size window keeps the old size until the next event, and the parts
// overlap.
void Window::relayout() {
  layout()->invalidate();
  layout()->activate();
  adjustSize();
}

// The handle is open for typing (the key says OK), or set (the key says
// EDIT).
void Window::setEditing(bool on) {
  editing_ = on;
  showHandle();
  if (on && homeOn_) {
    handle_->setFocus();
    handle_->selectAll();
  }
  if (phase_ == Mining && on && homeOn_)
    say_ = "type the handle and press enter: the mining waits";
  sync();
}

// The handle row: with HOME on, the open field (OK) or the handle (EDIT), and
// with HOME off, the miner's own key (EDIT turns HOME on).
void Window::showHandle() {
  const bool home = homeOn_;
  const bool open = home && editing_;
  handleSet_->setText(home ? "@" + handle_->text().trimmed().toLower() : "this miner's key");
  handleSet_->setEnabled(home);
  handleSlot_->setCurrentWidget(open ? static_cast<QWidget*>(handle_) : handleSet_);
  okKey_->setText(open ? "OK" : "EDIT");
  okKey_->setToolTip(open ? "Look up the handle on " + site_->base().host() + "."
                     : home ? "Another handle." : "Mine to an fc account: give its handle.");
}

// A key that shows its state with a LED in its color.
void Window::keyLed(QPushButton* k, const QColor& color) {
  QPixmap pm(10, 10);
  pm.fill(Qt::transparent);
  QPainter p(&pm);
  p.setRenderHint(QPainter::Antialiasing);
  p.setPen(QPen(QColor(0, 0, 0), 1));
  p.setBrush(k->isChecked() ? color : QColor(color.red() / 5, color.green() / 5, color.blue() / 5));
  p.drawEllipse(QRectF(1, 1, 8, 8));
  k->setIcon(QIcon(pm));
}

// One handshake with a CES server, off the window's thread.
void Window::probe(const QString& server, Lcd::Led* led) {
  QThread* t = QThread::create([this, server, led] {
    const Probe p = ::probe(server);
    QMetaObject::invokeMethod(this, [this, server, led, p] {
      *led = p.up ? Lcd::On : Lcd::Bad;
      const QString mining = led == &mineLed_ ? (p.powEngine == 1 ? ", takes mining" : ", takes no mining now")
                                              : QString();
      log(server + (p.up ? QString(" answers: difficulty %1%2").arg(p.minDifficulty).arg(mining) : " " + p.why));
      looked();
      fill();
    }, Qt::QueuedConnection);
  });
  connect(t, &QThread::finished, t, &QObject::deleteLater);
  t->start();
}

// The servers answered or timed out, so mine with the saved handle, or ask.
void Window::looked() {
  if (phase_ != Looking || siteLed_ == Lcd::Wait || homeLed_ == Lcd::Wait || mineLed_ == Lcd::Wait)
    return;
  QTimer::singleShot(700, this, [this] {
    if (phase_ != Looking)
      return;
    say_.clear();                     // the lookup is over
    const QString saved = handle_->text().trimmed().toLower();
    if (!homeOn_) {
      mine();
    } else if (!saved.isEmpty()) {
      lookUp(saved);
      mine();
    } else {
      ask();
    }
  });
}

void Window::ask() {
  phase_ = Asking;
  say_ = "what's your handle? type it below and press enter";
  setEditing(true);
  fill();
}

void Window::lookUp(const QString& handle) {
  key_.clear();
  say_ = "looking for " + handle;
  log("asking " + site_->base().host() + " for the key of " + handle);
  site_->look(handle);
  fill();
}

void Window::mine() {
  phase_ = Mining;
  home();
  engine_->start(threads());
  started_ = true;
  sync();                           // the engine already runs, paused while the handle was open
}

void Window::home() {
  const QString handle = handle_->text().trimmed().toLower();
  engine_->setHome(homeOn_ && !handle.isEmpty(), handle, key_, ledger_);
}

// The threads now, or none while the person pauses, or types the handle that
// the credit goes to.
int Window::threads() const {
  if (paused())
    return 0;
  return turboOn_ ? cores_ : threads_;
}

bool Window::paused() const { return pauseOn_ || (editing_ && homeOn_); }

// HOME: where the credit goes. With HOME on and no handle, the miner asks for one.
void Window::setHome(bool on) {
  homeOn_ = on;
  QSettings().setValue("home", on);
  log(on ? "mining to the fc account" : "mining stays in this miner's key");
  showHandle();
  if (on && handle_->text().trimmed().isEmpty() && phase_ != Looking) {
    ask();
    return;
  }
  if (!on && phase_ == Asking) {
    mine();
    return;
  }
  home();
  if (on && key_.isEmpty() && !handle_->text().trimmed().isEmpty())
    lookUp(handle_->text().trimmed().toLower());
  sync();
}

// The keys show the bools and the resulting pause, and the engine takes the
// threads.
void Window::sync() {
  const bool lit[3] = {turboOn_, paused(), homeOn_};
  QPushButton* keys[3] = {turbo_, pause_, toHome_};
  const QColor colors[3] = {QColor(255, 179, 71), QColor(159, 232, 214), QColor(226, 51, 127)};
  for (int i = 0; i < 3; ++i) {
    keys[i]->setChecked(lit[i]);
    keyLed(keys[i], colors[i]);
  }
  if (started_)                     // even while the miner asks for a handle
    engine_->setThreads(threads());
  fill();
}

void Window::log(const QString& line) {
  const QString stamped = QString("%1  %2  %3").arg(++lines_, 3, 10, QChar('0'))
                              .arg(QDateTime::currentDateTime().toString("HH:mm:ss"), line);
  log_->appendPlainText(stamped);
  lastLine_ = line;
  std::fprintf(stderr, "%s\n", stamped.toLocal8Bit().constData());
}

void Window::fill() {
  const World w = engine_->world();
  const Lcd::Led mineLed = phase_ != Mining ? mineLed_
                           : !w.problem.isEmpty() ? Lcd::Bad : w.connected ? Lcd::On : Lcd::Wait;
  lcd_->setLeds(siteLed_, homeLed_, mineLed);
  lcd_->setCredit(w.mined);
  const qint64 here = std::max<qint64>(0, w.balance);
  const int thr = phase_ == Mining ? w.threads : threads();   // before mining, what it will use
  lcd_->setNumbers(QString("%1  %2/%3 THR  SOL %4").arg(rate(w.rate)).arg(thr).arg(cores_).arg(w.solutions));

  QString line;
  if (phase_ != Mining) {
    line = say_;
  } else {
    QStringList parts;
    parts << w.doing;
    if (!w.problem.isEmpty())
      parts << w.problem;
    else if (!say_.isEmpty())
      parts << say_;
    parts << (w.toHome ? QString("home %1 · waiting %2").arg(credit(w.moved), credit(here))
                       : QString("in this miner's key %1").arg(credit(here)));
    line = parts.join("  ·  ");
  }
  lcd_->setMarquee(line);

  if (!details_->isVisible() || detailPages_->currentIndex() != 1)
    return;
  // A long word (a key, a path) may break every 16 characters, so it wraps.
  auto breakable = [](const QString& v) {
    QString out;
    int run = 0;
    for (QChar c : v) {
      out += c;
      run = c.isSpace() ? 0 : run + 1;
      if (run == 16) {
        out += QChar(0x200B);
        run = 0;
      }
    }
    return out;
  };
  auto row = [&breakable](const QString& k, const QString& v) {
    return "<tr><td style='color:#ffb347;padding-right:10px'>" + k + "</td><td style='color:#60ffd0'>" +
           breakable(v).toHtmlEscaped() + "</td></tr>";
  };
  auto head = [](const QString& t) {
    return "<tr><td colspan='2' style='color:#e2337f;padding-top:6px'><b>" + t + "</b></td></tr>";
  };
  QString t = "<table cellspacing='1' style='font-family:DejaVu Sans Mono,Menlo,Consolas,monospace;font-size:11px'>";
  t += head("now");
  t += row("doing", w.doing);
  t += row("up", w.since.isValid() ? span(w.since.secsTo(QDateTime::currentDateTime())) : "-");
  t += row("threads", QString("%1 of %2").arg(w.threads).arg(cores_));
  t += row("hashes", QString("%1 this solution, %2 in all").arg(w.hashes).arg(w.allHashes));
  t += row("solutions", QString("%1 taken, %2 not").arg(w.solutions).arg(w.rejected));
  t += row("last solution", when(w.lastSolution));
  t += head("this miner's key");
  t += row("key", w.minerKey);
  t += row("file", w.keyFile);
  t += row("account", w.balance < 0 ? "-" : w.account ? "yes" : "none yet: the first solution opens it");
  t += row("balance", credit(w.balance) + " credit");
  t += row("reserve", credit(w.reserve) + " credit, for the fees");
  t += head("home");
  t += row("handle", w.handle.isEmpty() ? "-" : w.handle);
  t += row("key", w.personKey.isEmpty() ? "-" : w.personKey);
  t += row("ledger", w.ledger.isEmpty() ? "-" : w.ledger);
  t += row("moved", credit(w.moved) + QString(" credit in %1 moves").arg(w.moves));
  t += row("last move", when(w.lastMove));
  t += head("mine server");
  t += row("address", w.server + (w.connected ? ", connected" : ""));
  t += row("key", w.serverKey.isEmpty() ? "-" : w.serverKey);
  t += row("rpc port", w.rpcPort ? QString::number(w.rpcPort) : "-");
  t += row("difficulty", w.minDifficulty < 0 ? "-" : QString::number(w.minDifficulty));
  t += row("proofs waiting", w.pendingPows < 0 ? "-" : QString::number(w.pendingPows));
  t += row("proof engine", w.powEngine < 0 ? "-" : w.powEngine ? "yes" : "no");
  if (!w.serverInfo.isEmpty()) {
    t += head("the server says");
    for (const auto& [k, v] : w.serverInfo)
      t += row(k, v);
  }
  t += "</table>";
  if (facts_->text() != t)
    facts_->setText(t);
}
