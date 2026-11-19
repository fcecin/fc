// The miner window: an LCD, the handle under it, three keys (TURBO, PAUSE,
// HOME) and a details panel. At start it checks the servers, asks for the
// handle, and mines.
#pragma once

#include <QString>
#include <QTimer>
#include <QWidget>

#include "lcd.h"

class Engine;
class Site;
class QFrame;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QPushButton;
class QStackedWidget;

class Window : public QWidget {
  Q_OBJECT

public:
  Window(Engine* engine, Site* site, QString mineServer, QWidget* parent = nullptr);

private:
  enum Phase { Looking, Asking, Mining };

  void probe(const QString& server, Lcd::Led* led);
  void looked();
  void ask();
  void lookUp(const QString& handle);
  void mine();
  void home();
  int threads() const;
  bool paused() const;
  void setHome(bool on);
  void sync();
  void keyLed(QPushButton* key, const QColor& color);
  void fill();
  void relayout();
  void setEditing(bool on);
  void showHandle();
  void log(const QString& line);

  Engine* engine_;
  Site* site_;
  QString mineServer_;
  Phase phase_ = Looking;
  Lcd* lcd_;
  Lcd::Led siteLed_ = Lcd::Wait, homeLed_ = Lcd::Wait, mineLed_ = Lcd::Wait;
  QString say_;                    // what the LCD says while it checks and asks
  QFrame* askRow_;
  QLineEdit* handle_;
  QLabel* handleSet_;              // the set handle, a label in the field's place
  QStackedWidget* handleSlot_;
  QPushButton* okKey_;             // OK while the handle is open, EDIT once it is set
  bool editing_ = true;
  QPushButton* turbo_;
  QPushButton* pause_;
  QPushButton* toHome_;
  QPushButton* detailsKey_;        // opens and closes the panel
  QPushButton* eventsTab_;
  QPushButton* factsTab_;
  QFrame* details_;
  QStackedWidget* detailPages_;    // the log or the facts, in one fixed height
  QPlainTextEdit* log_;
  QLabel* facts_;
  int lines_ = 0;
  quint64 lastHashes_ = 0;         // for each second's hashes on the meter
  QString lastLine_;
  QString key_, ledger_;           // as the site gave them for the handle
  int threads_ = 1;                // without TURBO
  // What the person set with the keys, which the keys show with the
  // resulting pause (sync).
  bool turboOn_ = false;
  bool pauseOn_ = false;
  bool homeOn_ = true;
  bool started_ = false;           // the engine runs, and takes the threads from sync
  int cores_ = 1;
  QTimer clock_;
  QTimer retry_;                   // retries a failed lookup
};
