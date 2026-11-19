#pragma once

// CES compute operations for the bridge (L2 builtin:compute over CesPlex). A
// program is a Lua source file, and a launch starts an instance of it. The
// owner key signs launch, kill and list, a person's key signs their calls,
// and the reader key signs the reads. Every operation opens its own
// connection and closes it.

#include "fileops.h"

#include <ces/keys.h>
#include <ces/l2/compute_client.h>
#include <ces/types.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace fc {

struct ComputeResult {
  uint8_t code = 0;      // CES error code; 0 = CES_OK
  std::string error;
  uint64_t pid = 0;
  uint64_t startedUs = 0;
  ces::Bytes reply;
  std::vector<ces::CesComputeClient::InstanceInfo> instances;
};

class ComputeOps {
public:
  explicit ComputeOps(const ces::KeyPair& readerKey);

  ComputeResult launch(const FileServer& s, const ces::KeyPair& owner, const std::string& path);
  ComputeResult kill(const FileServer& s, const ces::KeyPair& owner, uint64_t pid);
  ComputeResult list(const FileServer& s, const ces::KeyPair& owner);

  // The live instances of a source path, paid by the reader key.
  ComputeResult instances(const FileServer& s, const std::string& path);

  // A call into on_l2call. With a key, that key signs and pays `value`, and
  // with none, the reader key signs and value must be 0.
  ComputeResult call(const FileServer& s, const ces::KeyPair* caller, uint64_t pid,
                     uint64_t value, const ces::Bytes& memo);

private:
  template <class F>
  ComputeResult withReader(const FileServer& s, F&& f);

  ces::KeyPair readerKey_;
};

} // namespace fc
