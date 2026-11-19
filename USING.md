# Using fc

## Parts

| Path | What it is |
| --- | --- |
| `app/` | The Django site, its WebSocket hub (`fc/hub.py`) and the shells (`fc/shell/`) |
| `bridge/` | `fc-bridge`, the C++ program that holds the sealed keys and talks to CES |
| `miner/` | The fc miner, a Qt app for Linux, macOS and Windows |
| `deploy/` | The systemd units, the Caddyfile and the install steps |
| `tools/` | A local CES network (`fcnet.mjs`), the local stack (`dev.sh`), the server configs (`netconf.py`), operator credit (`credit.sh`), a terminal view of a program window (`fcmon.mjs`) and the font ROM builder (`rom.py`) |

fc needs a build of [ces](https://github.com/fcecin/ces).

## Development

    node tools/fcnet.mjs init && node tools/fcnet.mjs up
    mkdir -p tmp/build-bridge && cd tmp/build-bridge
    cmake ../../bridge -DFETCHCONTENT_SOURCE_DIR_CES=<ces checkout> && make fc-bridge && cd ../..
    python3 -m venv tmp/venv && tmp/venv/bin/pip install -r app/requirements.txt
    tools/dev.sh ceshd && tools/dev.sh bridge && tools/dev.sh web

The site runs at http://127.0.0.1:8765/ and writes the login codes to `tmp/dev/web.log`. The tools take ces from a checkout next to this one (`../ces/build/debug`), or from `CES_BUILD`. In the local stack, the shells run as plain processes (`FC_SHELL_LOCAL`).

The tests:

    cd app && FC_DEBUG=1 ../tmp/venv/bin/python manage.py test fc
    node --test tools/test/md.test.mjs tools/test/vs1.test.mjs

The VS1 tests run VS1 in the CES sandbox with the LuaJIT checker in `LJCHECK` (`ljcheck -s`), and skip without it.

## Install and update

See `deploy/README.md`.

## How it works

**The site.** A person signs up with an email and a handle. The site makes them a CES key, sealed so that only the bridge can use it, and grants it credit on the site's ledger. The hub pushes every change to the open pages. `GET /key/<handle>` gives a handle's public key, `GET /ledger` the ledger's address, and `GET /stats.txt` the stats.

**The network.** `fc-walk` maps the CES network from the ledger's peers every hour. The sky places each server by a layout of that graph, so every page shows the same sky.

**The chat.** The model runs on the ledger (`builtin:think`), paid from the person's credit there. It works in the person's shell and reads `~/AGENTS.md`, which the person can edit. A shell job can wake it (`job run --wake`).

**The shells.** Every person gets bash in a systemd unit of its own, which `fc-shelld` starts. The unit has its own root with a read-only `/usr`, a dynamic user, no network and no devices, and limits on memory, tasks and CPU. Its home is a 16 MiB disk image, the only place it can write to disk, and the image goes when the person logs out. Its only way out is the hub's socket. Inside, `ptyhost.py` runs bash and the jobs, and keeps them through a hub restart. `cesh` runs a CES command through the hub and the bridge, so the person's key stays out of the shell, and `desk` runs a desk op.

**Program windows.** A program draws with VS1 (`ces.load("vs1/vs1.lua")`) and sends frames over a binary cable, and `monitor.js` draws them with the font ROM.

**Stats.** `fc-stats` writes a report to `/run/fc-stats/report.json` every 30 seconds, and the pages read only that file.

## The miner

See `miner/README.md`. The build takes the site and the mine server (`FC_SITE`, `FC_MINE_SERVER`), and CI builds a static miner for Linux, macOS and Windows on every push to `miner/`.
