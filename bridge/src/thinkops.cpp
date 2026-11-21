#include "thinkops.h"

#include <ces/l2/think_client.h>
#include <ces/types.h>

namespace fc {

ThinkResult think(const FileServer& s, const ces::KeyPair& signer, const ces::ThinkInput& input,
                  const std::function<void(std::string_view)>& onText) {
  ThinkResult r;
  ces::CesThinkClient c;
  if (s.serverKey)
    c.setServerPubkey(*s.serverKey);
  uint8_t rc = c.connect(s.host, s.rpcPort, signer);
  if (rc == ces::CES_OK && input.chat)
    rc = c.chat(input.text, onText, r.reply, r.chat, r.charged, &r.report);
  else if (rc == ces::CES_OK)
    rc = c.think(input.text, onText, r.reply, r.charged, {}, nullptr, &r.report);
  c.disconnect();
  if (rc != ces::CES_OK) {
    r.code = rc;
    r.error = ces::errorString(rc);
  }
  return r;
}

} // namespace fc
