#include "seal.h"

#include <cryptopp/aes.h>
#include <cryptopp/filters.h>
#include <cryptopp/gcm.h>
#include <cryptopp/hex.h>
#include <cryptopp/osrng.h>

#include <fstream>
#include <stdexcept>

namespace fc {

namespace {

constexpr size_t NONCE = 12;
constexpr size_t TAG = 16;
constexpr size_t SECRET = 32;

std::string toHex(const uint8_t* p, size_t n) {
  std::string out;
  CryptoPP::HexEncoder enc(new CryptoPP::StringSink(out), false);
  enc.Put(p, n);
  enc.MessageEnd();
  return out;
}

std::optional<std::string> fromHex(const std::string& hex) {
  if (hex.size() % 2 != 0)
    return std::nullopt;
  for (char c : hex)
    if (!std::isxdigit(static_cast<unsigned char>(c)))
      return std::nullopt;
  std::string out;
  CryptoPP::StringSource(hex, true,
    new CryptoPP::HexDecoder(new CryptoPP::StringSink(out)));
  return out;
}

} // namespace

MasterKey loadMasterKey(const std::string& path) {
  std::ifstream f(path);
  if (!f)
    throw std::runtime_error("cannot read master key file: " + path);
  std::string hex;
  f >> hex;
  auto raw = fromHex(hex);
  if (!raw || raw->size() != 32)
    throw std::runtime_error("master key file must hold 64 hex chars");
  MasterKey mk;
  std::copy(raw->begin(), raw->end(), mk.begin());
  return mk;
}

std::string seal(const MasterKey& mk, const std::array<uint8_t, 32>& secret) {
  CryptoPP::AutoSeededRandomPool rng;
  uint8_t nonce[NONCE];
  rng.GenerateBlock(nonce, NONCE);

  CryptoPP::GCM<CryptoPP::AES>::Encryption enc;
  enc.SetKeyWithIV(mk.data(), mk.size(), nonce, NONCE);
  std::string ct;
  CryptoPP::AuthenticatedEncryptionFilter f(enc, new CryptoPP::StringSink(ct),
                                            false, TAG);
  f.Put(secret.data(), secret.size());
  f.MessageEnd();

  std::string blob(reinterpret_cast<const char*>(nonce), NONCE);
  blob += ct;
  return toHex(reinterpret_cast<const uint8_t*>(blob.data()), blob.size());
}

std::optional<std::array<uint8_t, 32>> unseal(const MasterKey& mk,
                                              const std::string& sealedHex) {
  auto blob = fromHex(sealedHex);
  if (!blob || blob->size() != NONCE + SECRET + TAG)
    return std::nullopt;
  try {
    CryptoPP::GCM<CryptoPP::AES>::Decryption dec;
    dec.SetKeyWithIV(mk.data(), mk.size(),
                     reinterpret_cast<const uint8_t*>(blob->data()), NONCE);
    std::string pt;
    CryptoPP::AuthenticatedDecryptionFilter f(
      dec, new CryptoPP::StringSink(pt),
      CryptoPP::AuthenticatedDecryptionFilter::DEFAULT_FLAGS, TAG);
    f.Put(reinterpret_cast<const uint8_t*>(blob->data()) + NONCE,
          blob->size() - NONCE);
    f.MessageEnd();
    if (pt.size() != SECRET)
      return std::nullopt;
    std::array<uint8_t, 32> out;
    std::copy(pt.begin(), pt.end(), out.begin());
    return out;
  } catch (const CryptoPP::Exception&) {
    return std::nullopt;
  }
}

} // namespace fc
