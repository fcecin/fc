// fc-bridge: the only fc process that holds keys and speaks CES.
//
// Protocol: one JSON object per line on a Unix socket, and one JSON reply per
// line. A server is the "host:port" of a CES server's main UDP port. The
// ledger is this installation's CES server, named by its public address
// (--ledger) and reached at its local address (--home). The bridge talks to
// the ledger and to any server at a public address, and --allow-private adds
// private and loopback addresses, for a local network.
// Requests:
//   {"op":"ping"}                                     -> ledger
//   {"op":"newkey"}                                   -> pubkey, sealed
//   {"op":"info","server":S}                          -> pubkey, rpc_port, min_difficulty, pow_engine, tps
//   {"op":"peers","server":S,"from":I,"max":M}        -> count, peers [{pubkey, address}]
//   {"op":"balance","server":S,"pubkey":H}            -> exists, balance
//   {"op":"grant","pubkey":H}                         -> granted (once per key, on the ledger)
//   {"op":"transfer","sealed":K,"server":S,"dest":H,"amount":N} -> balance
//   {"op":"cross","sealed":K,"server":S,"dest":H,"amount":N,"dest_server":S2} -> balance
//   {"op":"export","sealed":K}                        -> private
//   {"op":"file_get","server":S,"path":P,"max":N}     -> found, size, balance, content_b64
//   {"op":"file_put","sealed":K,"server":S,"path":P,"content_b64":B,"deposit":N}
//                                                     -> size, balance
//   {"op":"file_deposit","sealed":K,"server":S,"path":P,"amount":N} -> balance (any file)
//   {"op":"launch","sealed":K,"server":S,"path":P}    -> pid, started_us
//   {"op":"kill","sealed":K,"server":S,"pid":N}       -> (ok)
//   {"op":"ps","sealed":K,"server":S}                 -> instances [{pid, path, started_us, balance, cpu_bp, rss}]
//   {"op":"instances","server":S,"path":P}            -> instances (the reader key asks)
//   {"op":"call","sealed":K?,"server":S,"pid":N,"value":N?,"memo_b64":B}
//                                                     -> reply_b64 (no K: the reader key calls, value 0)
//   {"op":"think","sealed":K,"server":S,"prompt":T,"stream":B?}
//                                                     -> reply, charged, the token counts and limits,
//                                                        after a {"piece":T} line per piece with "stream"
//   {"op":"think","sealed":K,"server":S,"chat":C,"stream":B?}
//                                                     -> the same, and tool_calls, finish_reason and
//                                                        reasoning_content (when there is some)
// C = a chat request {"messages":[...],"tools":[...]} (ces::thinkChatCheck).
// file_put and launch take only paths in /h/<owner>/. H = 64-hex public key.
// N = raw units. Every reply has "ok"; a failed reply has "error".
//
// With --ceshd, a second socket (--cesh-socket) runs cesh and dials programs
// as a person (ceshops.h, dialpool.h).

#include "cesops.h"
#include "ceshops.h"
#include "computeops.h"
#include "fileops.h"
#include "seal.h"
#include "thinkops.h"

#include <ces/keys.h>
#include <ces/util/ctrlc.h>

#include <boost/asio.hpp>
#include <boost/json.hpp>

#include <cryptopp/base64.h>
#include <cryptopp/filters.h>

#include <cctype>
#include <fstream>
#include <iostream>
#include <mutex>
#include <regex>
#include <set>
#include <string>
#include <sys/stat.h>
#include <thread>

namespace asio = boost::asio;
namespace json = boost::json;
using local = asio::local::stream_protocol;

namespace {

struct Config {
  std::string socketPath = "/run/fc/bridge.sock";
  std::string masterKeyFile;
  std::string ledger;   // the ledger's public address
  std::string home;     // the ledger's local address
  std::string homeKeyFile;
  uint64_t grantAmount = 100000000;  // 1.00000000 credit
  std::string grantsFile;
  std::string readerKeyFile;
  bool allowPrivate = false;     // also talk to private and loopback addresses
  std::string ceshSocketPath = "/run/fc/cesh.sock";
  std::string ceshdPath;         // ceshd's socket, or empty for no cesh socket
};

void usage() {
  std::cerr <<
    "usage: fc-bridge --master-key FILE --ledger HOST:PORT --home HOST:PORT\n"
    "                 --home-key FILE --grants-file FILE [--allow-private]\n"
    "                 [--socket PATH] [--grant-amount RAW] [--reader-key FILE]\n"
    "                 [--ceshd PATH [--cesh-socket PATH]]\n";
}

bool isHex64(const std::string& s) {
  if (s.size() != 64)
    return false;
  for (char c : s)
    if (!std::isxdigit(static_cast<unsigned char>(c)))
      return false;
  return true;
}

std::string b64encode(const ces::Bytes& b) {
  std::string out;
  CryptoPP::StringSource(b.data(), b.size(), true,
    new CryptoPP::Base64Encoder(new CryptoPP::StringSink(out), false));
  return out;
}

std::optional<ces::Bytes> b64decode(const std::string& s) {
  for (char c : s)
    if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '+' || c == '/' || c == '='))
      return std::nullopt;
  std::string out;
  CryptoPP::StringSource(s, true,
    new CryptoPP::Base64Decoder(new CryptoPP::StringSink(out)));
  return ces::Bytes(out.begin(), out.end());
}

// A file path the bridge accepts: a known zone, printable ASCII, no "..".
bool goodPath(const std::string& p) {
  if (p.size() < 4 || p.size() > 200 || p[0] != '/')
    return false;
  if (p.rfind("/h/", 0) != 0 && p.rfind("/f/", 0) != 0 &&
      p.rfind("/p/", 0) != 0 && p.rfind("/s/", 0) != 0)
    return false;
  if (p.find("..") != std::string::npos || p.find("//") != std::string::npos)
    return false;
  for (char c : p)
    if (c < 0x21 || c > 0x7e)
      return false;
  return true;
}

// Accepts a bare 64-hex key or a TOML line such as server_key = "...".
std::string readKeyHex(const std::string& path) {
  std::ifstream f(path);
  if (!f)
    throw std::runtime_error("cannot read key file: " + path);
  std::string all((std::istreambuf_iterator<char>(f)), {});
  std::smatch m;
  static const std::regex hex64("[0-9a-fA-F]{64}");
  if (!std::regex_search(all, m, hex64))
    throw std::runtime_error("no 64-hex key in " + path);
  return m.str();
}

class Bridge {
public:
  Bridge(const Config& cfg)
      : cfg_(cfg), mk_(fc::loadMasterKey(cfg.masterKeyFile)),
        ops_(cfg.ledger, cfg.home, ces::KeyPair(readKeyHex(cfg.homeKeyFile)), cfg.allowPrivate),
        reader_(cfg.readerKeyFile.empty() ? ces::KeyPair::generate()
                                          : ces::KeyPair(readKeyHex(cfg.readerKeyFile))),
        files_(reader_), compute_(reader_) {
    std::ifstream f(cfg.grantsFile);
    std::string line;
    while (std::getline(f, line))
      if (isHex64(line))
        granted_.insert(line);
  }

  void warm() { ops_.warm(); }

  // Opens a sealed key, for the cesh socket.
  std::optional<std::array<uint8_t, 32>> unseal(const std::string& sealed) const {
    return fc::unseal(mk_, sealed);
  }

  // The rpc address and key of the server at `server`, for a dial, with the
  // same address checks as the other ops.
  std::optional<fc::CeshServer::Target> dialTarget(const std::string& server, std::string& why) {
    json::object req{{"server", server}};
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs) {
      why = err.contains("error") ? std::string(err["error"].as_string()) : "CANNOT_CONNECT";
      return std::nullopt;
    }
    asio::io_context ioc;
    asio::ip::udp::resolver res(ioc);
    boost::system::error_code ec;
    auto found = res.resolve(fs->host, std::to_string(fs->rpcPort), ec);
    if (ec || found.empty()) {
      why = "CANNOT_RESOLVE";
      return std::nullopt;
    }
    auto addr = found.begin()->endpoint().address();
    if (addr.is_v4())
      addr = asio::ip::make_address_v6(asio::ip::v4_mapped, addr.to_v4());
    if (!fs->serverKey) {
      why = "NO_SERVER_KEY";
      return std::nullopt;
    }
    fc::CeshServer::Target t;
    t.rpc = minx::SockAddr(addr, fs->rpcPort);
    t.key = *fs->serverKey;
    return t;
  }


  // The optional `emit` sends a line before the reply, for the pieces of a
  // streamed think, and returns false when the client is gone.
  json::object handle(const json::object& req,
                      const std::function<bool(const json::object&)>& emit = {}) {
    std::string op = str(req, "op");
    if (op == "ping") {
      json::object o = ok();
      o["ledger"] = ops_.ledger();
      return o;
    }
    if (op == "info")
      return info(req);
    if (op == "peers")
      return peers(req);
    if (op == "newkey")
      return newKey();
    if (op == "balance")
      return balance(req);
    if (op == "grant")
      return grant(req);
    if (op == "transfer")
      return transfer(req);
    if (op == "cross")
      return cross(req);
    if (op == "export")
      return exportKey(req);
    if (op == "file_get")
      return fileGet(req);
    if (op == "file_put")
      return filePut(req);
    if (op == "file_deposit")
      return fileDeposit(req);
    if (op == "launch")
      return launch(req);
    if (op == "kill")
      return kill(req);
    if (op == "ps")
      return ps(req);
    if (op == "instances")
      return instances(req);
    if (op == "call")
      return call(req);
    if (op == "think")
      return think(req, emit);
    return fail("UNKNOWN_OP");
  }

private:
  static std::string str(const json::object& o, const char* k) {
    auto* v = o.if_contains(k);
    return v && v->is_string() ? std::string(v->as_string()) : std::string();
  }

  static std::optional<uint64_t> amount(const json::object& o) {
    auto* v = o.if_contains("amount");
    if (!v)
      return std::nullopt;
    if (v->is_uint64())
      return v->as_uint64();
    if (v->is_int64() && v->as_int64() > 0)
      return static_cast<uint64_t>(v->as_int64());
    return std::nullopt;
  }

  static json::object ok() { return {{"ok", true}}; }

  static json::object fail(const std::string& e) {
    return {{"ok", false}, {"error", e}};
  }

  static json::object fromResult(const fc::OpResult& r) {
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["balance"] = r.balance;
    return o;
  }

  std::optional<minx::Hash> hashArg(const json::object& req, const char* k) {
    std::string h = str(req, k);
    if (!isHex64(h))
      return std::nullopt;
    minx::Hash out;
    minx::stringToHash(out, h);
    return out;
  }

  std::optional<ces::KeyPair> keyArg(const json::object& req) {
    auto secret = fc::unseal(mk_, str(req, "sealed"));
    if (!secret)
      return std::nullopt;
    return ces::KeyPair(minx::Hash(*secret));
  }

  json::object newKey() {
    auto kp = ces::KeyPair::generate();
    json::object o = ok();
    o["pubkey"] = minx::hashToString(kp.getPublicKeyAsHash());
    o["sealed"] = fc::seal(mk_, kp.getPrivateKey());
    return o;
  }

  // The address to dial for the "server" field, or nothing and an error.
  std::optional<std::string> dialArg(const json::object& req, const char* k, json::object& err) {
    std::string why;
    std::string d = ops_.target(str(req, k), why);
    if (d.empty()) {
      err = fail(why);
      return std::nullopt;
    }
    return d;
  }

  static uint16_t smallInt(const json::object& o, const char* k, uint16_t dflt, uint16_t hi) {
    auto* v = o.if_contains(k);
    if (!v || !v->is_int64() || v->as_int64() < 0)
      return dflt;
    return static_cast<uint16_t>(std::min<int64_t>(v->as_int64(), hi));
  }

  json::object info(const json::object& req) {
    json::object err;
    auto d = dialArg(req, "server", err);
    if (!d)
      return err;
    auto in = ops_.info(*d);
    if (!in)
      return fail("CANNOT_CONNECT");
    json::object o = ok();
    o["pubkey"] = in->pubkey;
    o["rpc_port"] = in->rpcPort;
    o["min_difficulty"] = in->minDifficulty;
    o["pow_engine"] = in->powEngine == 1;
    o["tps"] = in->tps;
    o["ledger"] = str(req, "server") == ops_.ledger();
    return o;
  }

  json::object peers(const json::object& req) {
    json::object err;
    auto d = dialArg(req, "server", err);
    if (!d)
      return err;
    uint16_t from = smallInt(req, "from", 0, 1000);
    uint16_t max = smallInt(req, "max", 16, 32);
    uint16_t count = 0;
    std::vector<fc::Peer> list;
    auto r = ops_.peers(*d, from, max == 0 ? 1 : max, count, list);
    if (r.code != 0)
      return fail(r.error);
    json::array arr;
    for (auto& p : list)
      arr.push_back(json::object{{"pubkey", p.pubkey}, {"address", p.address}});
    json::object o = ok();
    o["count"] = count;
    o["peers"] = std::move(arr);
    return o;
  }

  json::object balance(const json::object& req) {
    json::object err;
    auto d = dialArg(req, "server", err);
    if (!d)
      return err;
    auto pk = hashArg(req, "pubkey");
    if (!pk)
      return fail("BAD_PUBKEY");
    auto r = ops_.balance(*d, *pk);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["exists"] = r.exists;
    o["balance"] = r.balance;
    return o;
  }




  static std::optional<uint64_t> uintArg(const json::object& o, const char* k) {
    auto* v = o.if_contains(k);
    if (!v) return std::nullopt;
    if (v->is_uint64()) return v->as_uint64();
    if (v->is_int64() && v->as_int64() >= 0) return static_cast<uint64_t>(v->as_int64());
    return std::nullopt;
  }







  // The file server behind an address: its host, with the rpc port and key
  // from a free ping.
  std::optional<fc::FileServer> fileServer(const json::object& req, json::object& err) {
    auto d = dialArg(req, "server", err);
    if (!d)
      return std::nullopt;
    auto in = ops_.info(*d);
    if (!in) {
      err = fail("CANNOT_CONNECT");
      return std::nullopt;
    }
    if (in->rpcPort == 0) {
      err = fail("NO_FILE_STORE");
      return std::nullopt;
    }
    fc::FileServer fs;
    fs.host = d->substr(0, d->rfind(':'));
    fs.rpcPort = in->rpcPort;
    minx::Hash h;
    minx::stringToHash(h, in->pubkey);
    fs.serverKey = h;
    return fs;
  }

  // One grant per key, at the configured amount, which the web app can't
  // choose, and a repeated call grants nothing.
  json::object grant(const json::object& req) {
    std::string h = str(req, "pubkey");
    auto pk = hashArg(req, "pubkey");
    if (!pk)
      return fail("BAD_PUBKEY");
    std::lock_guard lk(grantsMu_);
    if (granted_.count(h))
      return fail("ALREADY_GRANTED");
    auto r = ops_.grant(*pk, cfg_.grantAmount);
    if (r.code != 0)
      return fail(r.error);
    granted_.insert(h);
    std::ofstream(cfg_.grantsFile, std::ios::app) << h << "\n";
    json::object o = ok();
    o["granted"] = cfg_.grantAmount;
    return o;
  }

  json::object transfer(const json::object& req) {
    auto kp = keyArg(req);
    json::object err;
    auto d = dialArg(req, "server", err);
    auto dest = hashArg(req, "dest");
    auto amt = amount(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    if (!d)
      return err;
    if (!dest)
      return fail("BAD_DEST");
    if (!amt || *amt == 0)
      return fail("BAD_AMOUNT");
    return fromResult(ops_.transfer(*kp, *d, *dest, *amt));
  }

  json::object cross(const json::object& req) {
    auto kp = keyArg(req);
    json::object err;
    auto d = dialArg(req, "server", err);
    // The origin server settles with dest_server, which it finds by this
    // address in its peer table.
    std::string destServer = str(req, "dest_server");
    std::string why;
    auto dest = hashArg(req, "dest");
    auto amt = amount(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    if (!d)
      return err;
    if (destServer.empty() || destServer == str(req, "server") ||
        (destServer != ops_.ledger() && ops_.target(destServer, why).empty()))
      return fail("BAD_DEST_SERVER");
    if (!dest)
      return fail("BAD_DEST");
    if (!amt || *amt == 0)
      return fail("BAD_AMOUNT");
    return fromResult(ops_.cross(*kp, *d, *dest, *amt, destServer));
  }

  json::object exportKey(const json::object& req) {
    auto kp = keyArg(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    json::object o = ok();
    o["private"] = kp->getPrivateKeyHexStr();
    return o;
  }

  json::object fileGet(const json::object& req) {
    std::string path = str(req, "path");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    if (!goodPath(path))
      return fail("BAD_PATH");
    uint32_t max = 262144;
    if (auto* v = req.if_contains("max"); v && v->is_int64() && v->as_int64() > 0)
      max = static_cast<uint32_t>(std::min<int64_t>(v->as_int64(), 1048576));
    auto r = files_.get(*fs, path, max);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["found"] = r.found;
    o["size"] = r.size;
    o["balance"] = r.fileBalance;
    o["content_b64"] = b64encode(r.content);
    return o;
  }

  json::object filePut(const json::object& req) {
    auto kp = keyArg(req);
    std::string path = str(req, "path");
    auto content = b64decode(str(req, "content_b64"));
    uint64_t deposit = 0;
    if (auto* v = req.if_contains("deposit"); v && (v->is_int64() || v->is_uint64())) {
      if (v->is_int64() && v->as_int64() < 0)
        return fail("BAD_AMOUNT");
      deposit = v->is_uint64() ? v->as_uint64() : static_cast<uint64_t>(v->as_int64());
    }
    if (!kp)
      return fail("BAD_SEALED_KEY");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    std::string home = "/h/" + minx::hashToString(kp->getPublicKeyAsHash()) + "/";
    if (!goodPath(path) || path.rfind(home, 0) != 0 || path.size() == home.size())
      return fail("BAD_PATH");
    if (!content || content->size() > 65536)
      return fail("BAD_CONTENT");
    auto r = files_.put(*fs, *kp, path, *content, deposit);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["size"] = r.size;
    o["balance"] = r.fileBalance;
    return o;
  }

  static json::array instancesJson(const std::vector<ces::CesComputeClient::InstanceInfo>& v) {
    json::array arr;
    for (const auto& i : v)
      arr.push_back(json::object{{"pid", i.pid}, {"path", i.sourceName},
                                 {"started_us", i.startedAtUs}, {"balance", i.fileBalance},
                                 {"cpu_bp", i.cpuBasisPoints}, {"rss", i.rssBytes}});
    return arr;
  }

  static json::object fromCompute(const fc::ComputeResult& r) {
    return r.code != 0 ? fail(r.error) : ok();
  }

  // A source path in the key's home zone: /h/<key>/<name>.
  static bool ownPath(const ces::KeyPair& kp, const std::string& path) {
    std::string home = "/h/" + minx::hashToString(kp.getPublicKeyAsHash()) + "/";
    return goodPath(path) && path.rfind(home, 0) == 0 && path.size() > home.size();
  }

  json::object fileDeposit(const json::object& req) {
    auto kp = keyArg(req);
    std::string path = str(req, "path");
    auto amt = amount(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    if (!amt || *amt == 0)
      return fail("BAD_AMOUNT");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    if (!goodPath(path))
      return fail("BAD_PATH");
    auto r = files_.deposit(*fs, *kp, path, *amt);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["balance"] = r.fileBalance;
    return o;
  }

  json::object launch(const json::object& req) {
    auto kp = keyArg(req);
    std::string path = str(req, "path");
    if (!kp)
      return fail("BAD_SEALED_KEY");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    if (!ownPath(*kp, path))
      return fail("BAD_PATH");
    auto r = compute_.launch(*fs, *kp, path);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["pid"] = r.pid;
    o["started_us"] = r.startedUs;
    return o;
  }

  // One prompt or chat request to builtin:think, signed and paid by the
  // person's key.
  json::object think(const json::object& req,
                     const std::function<bool(const json::object&)>& emit) {
    auto kp = keyArg(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    ces::ThinkInput input;
    if (auto* chat = req.if_contains("chat")) {
      // Check it here, so that a bad request gets its reason.
      std::string why;
      if (!chat->is_object() ||
          !ces::thinkChatCheck(json::serialize(*chat), input.text, &why)) {
        json::object o = fail("BAD_CHAT");
        o["why"] = chat->is_object() ? why : "the chat request is not an object";
        return o;
      }
      input.chat = true;
    } else {
      input.text = str(req, "prompt");
      if (input.text.empty())
        return fail("BAD_PROMPT");
    }
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    const bool stream = emit && req.if_contains("stream") && req.at("stream").is_bool() &&
                        req.at("stream").as_bool();
    std::function<void(std::string_view)> onText;
    if (stream)
      onText = [&](std::string_view piece) { emit(json::object{{"piece", json::string(piece)}}); };
    auto r = fc::think(*fs, *kp, input, onText);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["reply"] = r.reply;
    if (input.chat) {
      json::value calls = json::array{};
      if (!r.chat.toolCalls.empty()) {
        boost::system::error_code ec;
        calls = json::parse(r.chat.toolCalls, ec);
        if (ec || !calls.is_array())
          return fail("BAD_REPLY");
      }
      o["tool_calls"] = std::move(calls);
      if (!r.chat.reasoning.empty())
        o["reasoning_content"] = r.chat.reasoning;
      o["finish_reason"] = r.chat.finishReason;
    }
    o["charged"] = r.charged;
    o["context_tokens"] = r.report.contextTokens;
    o["max_input_bytes"] = r.report.maxInputBytes;
    o["max_out_tokens"] = r.report.maxOutTokens;
    if (r.report.usageKnown) {
      o["prompt_tokens"] = r.report.usage.promptTokens;
      o["cached_tokens"] = r.report.usage.cachedTokens;
      o["output_tokens"] = r.report.usage.outputTokens;
    }
    return o;
  }

  json::object kill(const json::object& req) {
    auto kp = keyArg(req);
    auto pid = uintArg(req, "pid");
    if (!kp)
      return fail("BAD_SEALED_KEY");
    if (!pid || *pid == 0)
      return fail("BAD_PID");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    return fromCompute(compute_.kill(*fs, *kp, *pid));
  }

  json::object ps(const json::object& req) {
    auto kp = keyArg(req);
    if (!kp)
      return fail("BAD_SEALED_KEY");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    auto r = compute_.list(*fs, *kp);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["instances"] = instancesJson(r.instances);
    return o;
  }

  json::object instances(const json::object& req) {
    std::string path = str(req, "path");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    if (!goodPath(path))
      return fail("BAD_PATH");
    auto r = compute_.instances(*fs, path);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["instances"] = instancesJson(r.instances);
    return o;
  }

  // A call into on_l2call. With "sealed", that key signs and pays. With no
  // "sealed", the reader key signs and the value must be 0.
  json::object call(const json::object& req) {
    std::optional<ces::KeyPair> kp;
    if (req.if_contains("sealed")) {
      kp = keyArg(req);
      if (!kp)
        return fail("BAD_SEALED_KEY");
    }
    auto pid = uintArg(req, "pid");
    uint64_t value = 0;
    if (req.if_contains("value")) {
      auto v = uintArg(req, "value");
      if (!v)
        return fail("BAD_AMOUNT");
      value = *v;
    }
    auto memo = b64decode(str(req, "memo_b64"));
    if (!pid || *pid == 0)
      return fail("BAD_PID");
    if (!memo || memo->size() > ces::CES_L2_CALL_MAX_MEMO)
      return fail("BAD_MEMO");
    json::object err;
    auto fs = fileServer(req, err);
    if (!fs)
      return err;
    auto r = compute_.call(*fs, kp ? &*kp : nullptr, *pid, value, *memo);
    if (r.code != 0)
      return fail(r.error);
    json::object o = ok();
    o["reply_b64"] = b64encode(r.reply);
    return o;
  }

  Config cfg_;
  fc::MasterKey mk_;
  fc::CesOps ops_;
  ces::KeyPair reader_;  // pays for the reads, so visitors pay nothing
  fc::FileOps files_;
  fc::ComputeOps compute_;
  std::set<std::string> granted_;
  std::mutex grantsMu_;
};

void serve(Bridge& bridge, local::socket sock) {
  try {
    asio::streambuf buf;
    for (;;) {
      boost::system::error_code ec;
      size_t n = asio::read_until(sock, buf, '\n', ec);
      if (ec)
        return;
      std::string line(asio::buffers_begin(buf.data()),
                       asio::buffers_begin(buf.data()) + n);
      buf.consume(n);
      json::object reply;
      try {
        json::value v = json::parse(line);
        auto emit = [&sock](const json::object& o) {
          std::string out = json::serialize(o) + "\n";
          boost::system::error_code wec;
          asio::write(sock, asio::buffer(out), wec);
          return !wec;
        };
        reply = v.is_object() ? bridge.handle(v.as_object(), emit)
                              : json::object{{"ok", false}, {"error", "BAD_JSON"}};
      } catch (const std::exception&) {
        reply = {{"ok", false}, {"error", "BAD_JSON"}};
      }
      std::string out = json::serialize(reply) + "\n";
      asio::write(sock, asio::buffer(out), ec);
      if (ec)
        return;
    }
  } catch (const std::exception& e) {
    std::cerr << "connection error: " << e.what() << "\n";
  }
}

} // namespace

int main(int argc, char** argv) {
  Config cfg;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) {
        usage();
        std::exit(2);
      }
      return argv[++i];
    };
    if (a == "--socket")
      cfg.socketPath = next();
    else if (a == "--master-key")
      cfg.masterKeyFile = next();
    else if (a == "--home")
      cfg.home = next();
    else if (a == "--home-key")
      cfg.homeKeyFile = next();
    else if (a == "--grants-file")
      cfg.grantsFile = next();
    else if (a == "--reader-key")
      cfg.readerKeyFile = next();
    else if (a == "--ledger")
      cfg.ledger = next();
    else if (a == "--allow-private")
      cfg.allowPrivate = true;
    else if (a == "--grant-amount")
      cfg.grantAmount = std::stoull(next());
    else if (a == "--ceshd")
      cfg.ceshdPath = next();
    else if (a == "--cesh-socket")
      cfg.ceshSocketPath = next();
    else {
      usage();
      return 2;
    }
  }
  if (cfg.masterKeyFile.empty() || cfg.ledger.empty() || cfg.home.empty() || cfg.homeKeyFile.empty() ||
      cfg.grantsFile.empty()) {
    usage();
    return 2;
  }

  try {
    Bridge bridge(cfg);
    asio::io_context io;
    ::unlink(cfg.socketPath.c_str());
    local::acceptor acc(io, local::endpoint(cfg.socketPath));
    // Owner and group only, and the web app runs in the socket's group.
    ::chmod(cfg.socketPath.c_str(), 0660);
    std::cerr << "fc-bridge listening on " << cfg.socketPath << "\n";
    std::optional<fc::CeshServer> cesh;
    // The program windows' streams, for the life of the process.
    static fc::DialPool dials;
    if (!cfg.ceshdPath.empty()) {
      cesh.emplace(cfg.ceshSocketPath, cfg.ceshdPath,
                   [&bridge](const std::string& sealed) { return bridge.unseal(sealed); },
                   [&bridge](const std::string& server, std::string& why) {
                     return bridge.dialTarget(server, why);
                   },
                   dials);
      cesh->start();
    }
    std::thread([&bridge] { bridge.warm(); }).detach();
    // ceslib's SIGINT/SIGTERM handler only sets a flag, after which every CES
    // wait returns "interrupted", so exit when it is set.
    std::thread([path = cfg.socketPath, ceshPath = cesh ? cfg.ceshSocketPath : std::string()] {
      while (ces::notInterrupted())
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
      ::unlink(path.c_str());
      if (!ceshPath.empty())
        ::unlink(ceshPath.c_str());
      std::cerr << "fc-bridge: signal received, exiting\n";
      std::_Exit(0);
    }).detach();
    for (;;) {
      local::socket s(io);
      acc.accept(s);
      std::thread(serve, std::ref(bridge), std::move(s)).detach();
    }
  } catch (const std::exception& e) {
    std::cerr << "fatal: " << e.what() << "\n";
    return 1;
  }
}
