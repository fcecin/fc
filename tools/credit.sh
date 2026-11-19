#!/bin/bash
# Gives credit to a key from a CES server's account, which the server sets to
# 2^46 raw units (about 703,687 credits) at every start and daily maintenance,
# so this gives at most that much a day. It needs the server key.
#
#   tools/credit.sh --server HOST:PORT --key-file FILE KEY CREDITS
#
# FILE holds the 64-hex server key, alone or in a TOML line, and KEY is the
# account's 64-hex public key (manage.py fc_who EMAIL prints it). Run it once
# per server where the account needs credit.
set -euo pipefail
CESH="${CESH:-cesh}"
server="" keyfile=""
while [ $# -gt 2 ]; do
  case "$1" in
    --server) server="$2"; shift 2 ;;
    --key-file) keyfile="$2"; shift 2 ;;
    *) break ;;
  esac
done
[ -n "$server" ] && [ -n "$keyfile" ] && [ $# -eq 2 ] || { sed -n 2,11p "$0"; exit 2; }
key="$1" credits="$2"
[[ "$key" =~ ^[0-9a-f]{64}$ ]] || { echo "KEY must be 64 hex" >&2; exit 2; }
raw=$(python3 -c "from decimal import Decimal; import sys; print(int(Decimal(sys.argv[1]) * 100000000))" "$credits")
wallet=$(mktemp)
trap 'rm -f "$wallet"' EXIT
grep -oE '[0-9a-fA-F]{64}' "$keyfile" | head -1 > "$wallet"
"$CESH" --server "$server" -r "$wallet" -a @0 transfer --open "$key" "$raw"
