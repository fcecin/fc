// The miner's display: an LCD with three LEDs (the site, home, the mine
// server), the credit mined in seven-segment digits, a line of numbers, a
// marquee of what happens, and a hash rate meter.
#pragma once

#include <QString>
#include <QTimer>
#include <QWidget>

#include <deque>

// The credit as the display shows it, in at most 10 digits. The whole part
// keeps its digits first, and the decimals are truncated.
QString creditDigits(qint64 raw);

class Lcd : public QWidget {
  Q_OBJECT

public:
  explicit Lcd(QWidget* parent = nullptr);

  enum Led { Off, Wait, On, Bad };
  void setLeds(Led site, Led home, Led mine);
  void setCredit(qint64 raw);                 // the digits roll to it
  void setNumbers(const QString& text);       // the line under the digits
  void setMarquee(const QString& text);       // scrolls when wider than the display
  void sample(double hashes);                 // one second's hashes, for the meter

  QSize sizeHint() const override { return {336, 182}; }

protected:
  void paintEvent(QPaintEvent*) override;

private:
  void segments(class QPainter& p, const QRectF& box, QChar c);

  Led leds_[3] = {Wait, Wait, Wait};
  QString credit_ = "0.00000000";
  qint64 shown_ = 0;                          // raw units on the digits now
  qint64 target_ = 0;                         // raw units that they roll to
  QTimer roll_;
  QString numbers_;
  QString marquee_;
  int offset_ = 0;
  QTimer scroll_;
  std::deque<double> rates_;
};
