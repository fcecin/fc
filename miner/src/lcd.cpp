#include "lcd.h"

#include <QFontDatabase>
#include <QPainter>
#include <QPainterPath>

#include <algorithm>
#include <cmath>
#include <cstdlib>

namespace {

const QColor LIT(96, 255, 208);
const QColor DIM(96, 255, 208, 20);
const QColor GLOW(96, 255, 208, 60);
const QColor AMBER(255, 179, 71);
const QColor RED(255, 90, 120);
constexpr int BARS = 28;
constexpr int BLOCKS = 8;
constexpr int SCROLL_MS = 40;
constexpr int ROLL_MS = 30;          // one frame of the digits' roll
constexpr double ROLL = 0.09;        // the share of the way that each frame covers

QFont mono(int px, bool bold = true) {
  QFont f = QFontDatabase::systemFont(QFontDatabase::FixedFont);
  f.setPixelSize(px);
  f.setBold(bold);
  f.setLetterSpacing(QFont::AbsoluteSpacing, 1);
  return f;
}

// A character's lit segments: a top, b top right, c bottom right, d bottom,
// e bottom left, f top left, g middle.
QString lit(QChar c) {
  switch (c.unicode()) {
    case '0': return "abcdef";
    case '1': return "bc";
    case '2': return "abged";
    case '3': return "abgcd";
    case '4': return "fgbc";
    case '5': return "afgcd";
    case '6': return "afgedc";
    case '7': return "abc";
    case '8': return "abcdefg";
    case '9': return "abcdfg";
    case '-': return "g";
    default: return "";
  }
}

}  // namespace

QString creditDigits(qint64 raw) {
  constexpr qint64 ONE = 100000000;   // raw units in a credit
  constexpr int DIGITS = 10;          // the digits that fit before "CR"
  const QString whole = QString::number(raw / ONE);
  const int decimals = std::clamp(DIGITS - static_cast<int>(whole.size()), 0, 8);
  if (decimals == 0)
    return whole;
  return whole + '.' + QString::number(raw % ONE).rightJustified(8, '0').left(decimals);
}

Lcd::Lcd(QWidget* parent) : QWidget(parent) {
  setMinimumSize(sizeHint());
  // The digits roll to a new value like a counter, fast at first, then slow.
  connect(&roll_, &QTimer::timeout, this, [this] {
    const qint64 left = target_ - shown_;
    const qint64 step = static_cast<qint64>(static_cast<double>(left) * ROLL);
    shown_ = (step == 0 || std::llabs(left) < 40) ? target_ : shown_ + step;
    credit_ = creditDigits(shown_);
    update(QRect(0, 26, width(), 56));
    if (shown_ == target_)
      roll_.stop();
  });
  connect(&scroll_, &QTimer::timeout, this, [this] {
    offset_ += 1;
    update(QRect(8, 104, width() - 16, 20));
  });
}

void Lcd::setLeds(Led site, Led home, Led mine) {
  if (leds_[0] == site && leds_[1] == home && leds_[2] == mine)
    return;
  leds_[0] = site;
  leds_[1] = home;
  leds_[2] = mine;
  update();
}

void Lcd::setCredit(qint64 raw) {
  raw = std::max<qint64>(0, raw);
  if (raw == target_)
    return;
  target_ = raw;
  if (!roll_.isActive())
    roll_.start(ROLL_MS);
}

void Lcd::setNumbers(const QString& text) {
  if (text != numbers_) {
    numbers_ = text;
    update();
  }
}

void Lcd::setMarquee(const QString& text) {
  const QString t = text.toUpper();
  if (t == marquee_)
    return;
  marquee_ = t;
  offset_ = 0;
  const bool wide = QFontMetrics(mono(13)).horizontalAdvance(marquee_) > width() - 20;
  if (wide && !scroll_.isActive())
    scroll_.start(SCROLL_MS);
  else if (!wide)
    scroll_.stop();
  update();
}

void Lcd::sample(double hashes) {
  rates_.push_back(std::max(0.0, hashes));
  while (rates_.size() > BARS)
    rates_.pop_front();
  update();
}

void Lcd::segments(QPainter& p, const QRectF& box, QChar c) {
  const double x = box.x(), y = box.y(), w = box.width(), h = box.height();
  const double t = w * 0.2;
  const double half = h / 2;
  const QRectF seg[7] = {
      {x + t, y, w - 2 * t, t},                          // a
      {x + w - t, y + t * 0.6, t, half - t * 1.1},       // b
      {x + w - t, y + half + t * 0.5, t, half - t * 1.1},// c
      {x + t, y + h - t, w - 2 * t, t},                  // d
      {x, y + half + t * 0.5, t, half - t * 1.1},        // e
      {x, y + t * 0.6, t, half - t * 1.1},               // f
      {x + t, y + half - t / 2, w - 2 * t, t},           // g
  };
  const QString on = lit(c);
  for (int i = 0; i < 7; ++i) {
    const bool l = on.contains(QChar('a' + i));
    if (l) {
      p.setBrush(GLOW);
      p.drawRoundedRect(seg[i].adjusted(-1.5, -1.5, 1.5, 1.5), t / 2, t / 2);
    }
    p.setBrush(l ? LIT : DIM);
    p.drawRoundedRect(seg[i], t / 3, t / 3);
  }
}

void Lcd::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.setRenderHint(QPainter::Antialiasing);
  const QRectF all = rect().adjusted(1, 1, -1, -1);

  // The background, with scan lines.
  QLinearGradient bg(0, 0, 0, height());
  bg.setColorAt(0, QColor(4, 18, 16));
  bg.setColorAt(1, QColor(2, 9, 8));
  p.setPen(QPen(QColor(0, 0, 0), 2));
  p.setBrush(bg);
  p.drawRoundedRect(all, 8, 8);
  p.setPen(Qt::NoPen);
  p.setBrush(QColor(255, 255, 255, 6));
  for (int yy = 3; yy < height() - 3; yy += 3)
    p.drawRect(QRectF(4, yy, width() - 8, 1));

  // The LEDs.
  const char* names[3] = {"SITE", "HOME", "MINE"};
  p.setFont(mono(9));
  for (int i = 0; i < 3; ++i) {
    const double lx = 12 + i * 58;
    const QColor c = leds_[i] == On ? LIT : leds_[i] == Wait ? AMBER : leds_[i] == Bad ? RED : DIM;
    if (leds_[i] != Off) {
      p.setBrush(QColor(c.red(), c.green(), c.blue(), 70));
      p.drawEllipse(QPointF(lx + 4, 14), 6, 6);
    }
    p.setBrush(c);
    p.drawEllipse(QPointF(lx + 4, 14), 3.5, 3.5);
    p.setPen(leds_[i] == Off ? DIM : LIT);
    p.drawText(QPointF(lx + 12, 18), names[i]);
    p.setPen(Qt::NoPen);
  }

  // The credit, in slanted seven-segment digits.
  p.save();
  p.translate(14, 30);
  p.setTransform(QTransform().shear(-0.1, 0), true);
  double cx = 4;
  for (QChar c : credit_) {
    if (c == '.') {
      p.setBrush(LIT);
      p.drawEllipse(QRectF(cx + 1, 40, 5, 5));
      cx += 9;
      continue;
    }
    segments(p, QRectF(cx, 0, 21, 45), c);
    cx += 27;
  }
  p.restore();
  p.setFont(mono(10));
  p.setPen(LIT);
  p.drawText(QPointF(width() - 36, 74), "CR");

  // The numbers.
  p.setFont(mono(11));
  p.drawText(QRectF(12, 84, width() - 24, 16), Qt::AlignLeft | Qt::AlignVCenter, numbers_);

  // The marquee.
  p.save();
  p.setClipRect(QRect(10, 104, width() - 20, 20));
  p.setFont(mono(13));
  const QFontMetrics fm(p.font());
  const int tw = fm.horizontalAdvance(marquee_);
  if (tw <= width() - 20) {
    p.drawText(QPointF(12, 119), marquee_);
  } else {
    const int gap = 40;
    const int x = 12 - offset_ % (tw + gap);
    p.drawText(QPointF(x, 119), marquee_);
    p.drawText(QPointF(x + tw + gap, 119), marquee_);
  }
  p.restore();

  // The work meter: each second's hashes, the newest on the right.
  const double top = 130, mh = 42;
  const double bw = (width() - 24.0) / BARS;
  const double peak = rates_.empty() ? 1 : std::max(1.0, *std::max_element(rates_.begin(), rates_.end()));
  const int start = BARS - static_cast<int>(rates_.size());
  for (int i = 0; i < BARS; ++i) {
    const double r = i >= start ? rates_[i - start] : 0;
    // The window's peak sits at three quarters, and red marks a peak.
    const int on = static_cast<int>(std::lround(r / (peak * 1.33) * BLOCKS));
    for (int b = 0; b < BLOCKS; ++b) {
      const QColor c = b < 5 ? LIT : b < 7 ? AMBER : RED;
      p.setBrush(b < on ? c : QColor(c.red(), c.green(), c.blue(), 7));
      p.drawRect(QRectF(12 + i * bw, top + mh - (b + 1) * (mh / BLOCKS) + 1, bw - 2, mh / BLOCKS - 2));
    }
  }
}
