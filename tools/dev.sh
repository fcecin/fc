#!/bin/bash
# The local fc stack: the bridge and the web app, on the network from fcnet.mjs.
#
#   tools/dev.sh ceshd     start ceshd on tmp/dev/ceshd.sock (the shells run cesh there)
#   tools/dev.sh bridge    start fc-bridge on tmp/dev/bridge.sock (and its cesh socket)
#   tools/dev.sh web       start the web app on 127.0.0.1:8765, and the hub
#   tools/dev.sh stop      stop them
#
# Run `node tools/fcnet.mjs init` and `node tools/fcnet.mjs up` first.
set -euo pipefail
FC="$(cd "$(dirname "$0")/.." && pwd)"
DEV="$FC/tmp/dev"
NET="$FC/tmp/net"
CES_BUILD="${CES_BUILD:-$FC/../ces/build/debug}"   # a ces build, by default in a ces checkout next to this one
CESH="$CES_BUILD/cesh"
CESHD="$CES_BUILD/ceshd"
mkdir -p "$DEV"

servers() {  # name host:port priv, one line for each server
  python3 -c "
import json
for s in json.load(open('$NET/fcnet.json'))['servers']:
    print(s['name'], '127.0.0.1:%d' % s['port'], s['priv'])"
}

start_ceshd() {
  mkdir -p "$DEV/ceshd-homes" "$DEV/ceshd-scratch"
  [ -f "$DEV/ceshd.pid" ] && kill "$(cat "$DEV/ceshd.pid")" 2>/dev/null && sleep 1
  nohup "$CESHD" --socket "$DEV/ceshd.sock" --homes "$DEV/ceshd-homes" --scratch "$DEV/ceshd-scratch" \
    --timeout 3600 --home-mb 32 --file-mb 8 > "$DEV/ceshd.log" 2>&1 &
  echo $! > "$DEV/ceshd.pid"
  echo "ceshd pid $(cat "$DEV/ceshd.pid")"
}

start_bridge() {
  [ -f "$DEV/master.key" ] || python3 -c "import secrets; print(secrets.token_hex(32))" > "$DEV/master.key"
  if [ ! -f "$DEV/reader.key" ]; then
    "$CES_BUILD/ces" --genkeypair > "$DEV/reader.pair"
    grep -oP 'Private Key:\s*\K\w+' "$DEV/reader.pair" > "$DEV/reader.key"
  fi
  local reader_pub ledger_toml
  reader_pub=$(grep -oP 'Public Key:\s*\K\w+' "$DEV/reader.pair")
  ledger_toml="$NET/ledger.toml"
  while read -r name addr priv; do
    [ "$name" = ledger ] && continue
    # The reader key pays for the reads, so give it 1000 credits on each server.
    echo "$priv" > "$DEV/wallet.$name"
    "$CESH" --server "$addr" -r "$DEV/wallet.$name" -a @0 -q transfer --open "$reader_pub" 100000000000 \
      > /dev/null 2>&1 || true
  done < <(servers)
  touch "$DEV/grants.txt"
  [ -f "$DEV/bridge.pid" ] && kill "$(cat "$DEV/bridge.pid")" 2>/dev/null && sleep 1
  nohup "$FC/tmp/build-bridge/fc-bridge" --socket "$DEV/bridge.sock" --master-key "$DEV/master.key" \
    --ledger 127.0.0.1:55100 --home 127.0.0.1:55100 --home-key "$ledger_toml" \
    --grants-file "$DEV/grants.txt" --reader-key "$DEV/reader.key" --allow-private \
    --ceshd "$DEV/ceshd.sock" --cesh-socket "$DEV/cesh.sock" \
    > "$DEV/bridge.log" 2>&1 &
  echo $! > "$DEV/bridge.pid"
  echo "bridge pid $(cat "$DEV/bridge.pid")"
}

start_web() {
  [ -f "$DEV/web.pid" ] && kill "$(cat "$DEV/web.pid")" 2>/dev/null && sleep 1
  cd "$FC/app"
  FC_DEBUG=1 FC_BRIDGE_SOCKET="$DEV/bridge.sock" FC_DB="$DEV/fc.sqlite3" \
    "$FC/tmp/venv/bin/python" manage.py migrate --run-syncdb --noinput > "$DEV/migrate.log" 2>&1
  FC_DEBUG=1 FC_BRIDGE_SOCKET="$DEV/bridge.sock" FC_DB="$DEV/fc.sqlite3" \
    FC_HUB_SOCKET="$DEV/hub.sock" FC_HUB_URL=ws://127.0.0.1:8766/hub FC_CESH_SOCKET="$DEV/cesh.sock" \
    nohup "$FC/tmp/venv/bin/python" manage.py runserver 127.0.0.1:8765 --noreload \
    > "$DEV/web.log" 2>&1 &
  echo $! > "$DEV/web.pid"
  echo "web pid $(cat "$DEV/web.pid") on http://127.0.0.1:8765/"
  # The hub, for the pages' WebSockets, with FC_DEBUG's secret.
  [ -f "$DEV/hub.pid" ] && kill "$(cat "$DEV/hub.pid")" 2>/dev/null && sleep 1
  FC_DEBUG=1 FC_SECRET_KEY=dev-only-key FC_HUB_SOCKET="$DEV/hub.sock" FC_HUB_BIND=127.0.0.1:8766 \
    FC_CESH_SOCKET="$DEV/cesh.sock" FC_SHELL_LOCAL="$DEV/shells" FC_APP_URL=http://127.0.0.1:8765 nohup "$FC/tmp/venv/bin/python" -m fc.hub > "$DEV/hub.log" 2>&1 &
  echo $! > "$DEV/hub.pid"
  echo "hub pid $(cat "$DEV/hub.pid") on ws://127.0.0.1:8766/hub"
  # The walk maps the network once, so run it again for a new graph.
  FC_DEBUG=1 FC_BRIDGE_SOCKET="$DEV/bridge.sock" FC_DB="$DEV/fc.sqlite3" FC_HUB_SOCKET="$DEV/hub.sock" \
    "$FC/tmp/venv/bin/python" manage.py fc_walk > "$DEV/walk.log" 2>&1 || true
}

case "${1:-}" in
  ceshd) start_ceshd ;;
  bridge) start_bridge ;;
  web) start_web ;;
  stop)
    for p in bridge web hub ceshd; do
      [ -f "$DEV/$p.pid" ] && kill "$(cat "$DEV/$p.pid")" 2>/dev/null || true
      rm -f "$DEV/$p.pid"
    done ;;
  *) echo "usage: tools/dev.sh ceshd|bridge|web|stop" >&2; exit 2 ;;
esac
