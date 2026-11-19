#pragma once

// CES think for the bridge (L2 builtin:think over CesPlex). It sends one
// prompt or one chat request, signed and paid by a person's key, and gets one
// reply. The server reports what the call used and its limits (the model's
// window), for the site's chat. A chat reply also has the tool calls.

#include "fileops.h"

#include <ces/keys.h>
#include <ces/l2/think_backend.h>   // ThinkReport

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

namespace fc {

struct ThinkResult {
  uint8_t code = 0;      // CES error code; 0 = CES_OK
  std::string error;
  std::string reply;
  uint64_t charged = 0;  // raw units
  ces::ThinkReport report;
  ces::ThinkChatEnd chat;   // the tool calls and the finish reason of a chat reply
};

// The optional `onText` gets each piece of the reply as it arrives. With
// `input.chat`, the input is a chat request (ces::thinkChatCheck).
ThinkResult think(const FileServer& s, const ces::KeyPair& signer, const ces::ThinkInput& input,
                  const std::function<void(std::string_view)>& onText = {});

} // namespace fc
