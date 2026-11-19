#include "site.h"

#include <QNetworkReply>
#include <QNetworkRequest>

namespace {
constexpr int TIMEOUT_MS = 15000;
}

Site::Site(QUrl base, QObject* parent) : QObject(parent), base_(std::move(base)) {}

void Site::get(const QString& path, std::function<void(int, const QString&, const QString&)> done) {
  QUrl url = base_;
  url.setPath(path);
  QNetworkRequest req(url);
  req.setTransferTimeout(TIMEOUT_MS);
  req.setHeader(QNetworkRequest::UserAgentHeader, "fc-miner");
  QNetworkReply* r = net_.get(req);
  connect(r, &QNetworkReply::finished, this, [r, done] {
    r->deleteLater();
    const int status = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QString line = QString::fromUtf8(r->readAll()).trimmed().section('\n', 0, 0);
    const QString why = status ? QString() : r->errorString();
    done(status, line, why);
  });
}

void Site::scan() {
  get("/ledger", [this](int status, const QString& ledger, const QString& why) {
    if (status == 200 && ledger.contains(':'))
      emit scanned(true, ledger, QString());
    else
      emit scanned(false, QString(), why.isEmpty() ? QString("HTTP %1").arg(status) : why);
  });
}

void Site::look(const QString& handle) {
  const int seq = ++seq_;
  const QString h = handle.trimmed().toLower();
  get("/key/" + QUrl::toPercentEncoding(h), [this, seq, h](int status, const QString& key, const QString& why) {
    if (seq != seq_)
      return;
    if (status == 404) {
      emit failed(h, "the site has no handle " + h);
      return;
    }
    if (status != 200 || key.size() != 64) {
      emit failed(h, "the site does not answer" + (why.isEmpty() ? QString(" (HTTP %1)").arg(status) : ": " + why));
      return;
    }
    get("/ledger", [this, seq, h, key](int status, const QString& ledger, const QString& why) {
      if (seq != seq_)
        return;
      if (status != 200 || !ledger.contains(':')) {
        emit failed(h, "the site did not give its ledger" + (why.isEmpty() ? QString(" (HTTP %1)").arg(status) : ": " + why));
        return;
      }
      emit found(h, key, ledger);
    });
  });
}
