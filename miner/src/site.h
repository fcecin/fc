// The fc site: a handle's key (GET /key/<handle>) and its ledger's address
// (GET /ledger), each one line of plain text.
#pragma once

#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <QUrl>

class Site : public QObject {
  Q_OBJECT

public:
  explicit Site(QUrl base, QObject* parent = nullptr);
  QUrl base() const { return base_; }
  // Look up a handle, and get found() or failed(). A newer lookup makes an
  // older one's answers stale.
  void look(const QString& handle);

  // Check the site, whose answer gives its ledger's address.
  void scan();

signals:
  void scanned(bool up, const QString& ledger, const QString& why);
  void found(const QString& handle, const QString& key, const QString& ledger);
  void failed(const QString& handle, const QString& why);

private:
  void get(const QString& path, std::function<void(int status, const QString& line, const QString& why)> done);

  QNetworkAccessManager net_;
  QUrl base_;
  int seq_ = 0;
};
