#include "../src/lcd.h"

#include <cstdio>

int main() {
  const struct { qint64 raw; const char* shown; } cases[] = {
    {0, "0.00000000"},
    {1, "0.00000001"},
    {9999999999, "99.99999999"},
    {10000000000, "100.0000000"},
    {12345678912345, "123456.7891"},
    {99999999999999, "999999.9999"},     // truncated
    {140737488355328, "1407374.883"},    // 2^47, the largest balance
    {999999999900000000, "9999999999"},
  };
  int bad = 0;
  for (const auto& c : cases) {
    const QString got = creditDigits(c.raw);
    if (got != c.shown) {
      std::printf("creditDigits(%lld) = %s, want %s\n", static_cast<long long>(c.raw),
                  got.toUtf8().constData(), c.shown);
      ++bad;
    }
  }
  std::printf("%s\n", bad ? "FAIL" : "OK");
  return bad ? 1 : 0;
}
