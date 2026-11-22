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

### Local CES websites

Rebuild `fc-bridge` after updating its sources. With the local CES network, ceshd and bridge running, start the web service with `FC_PUBLICATIONS_PORT=8765 tools/dev.sh web` and sign in at `http://fc.localhost:8765/`. The development server adds the publication tables with its usual `migrate --run-syncdb`. The port setting enables this feature only in debug mode. Use a browser that resolves subdomains of `.localhost` to loopback.

For a working example, copy [the guestbook](examples/guestbook.lua) into the fc shell as `board.lua`. On a funded local compute server, run `cesh file put board.lua board.lua --deposit 500000000 --server 127.0.0.1:55110` and `cesh compute launch board.lua --server 127.0.0.1:55110`. Two visitors can sign the same guestbook with their own keys.

A website is a running Lua program that accepts HTTP/1.1 requests on its CES dial and closes the connection after each reply. The caller's key is `conn.pubkey`. Once you have uploaded and launched the program on a funded local CES server, publish it from the fc shell:

    desk publish board board.lua on 127.0.0.1:55110

The command prints `http://fc.localhost:8765/link/<your-handle>/board/`. Send the link, optionally with a page path and query appended, to another account on this development installation. The visitor signs in and sees the website in its own frame. HTML forms, CSS, images and redirects within the website work. This first version runs script-free app pages, with a 1 MiB request limit and an 8 MiB response limit. Each visitor needs credit on the serving CES server for the dial.

A website can ask the visitor for a payment. It replies to a form post with the type `application/vnd.fc.payment+json` and a JSON object: an `order` name, a `description`, an `amount` in raw units as a decimal string, and an optional `return` path. fc shows the request in its own bar, outside the website's frame. An approval sends one paid call as the visitor to the website's program, with the order as the memo, and the program's `on_l2call` gets it. fc sends a payment at most once. A request for the same order gives the same payment again. If no result comes back, `/payments` shows that the outcome is unknown, and fc does not send the payment again. `desk website` prints the guide that the assistant reads, with [the booklet shop](app/fc/shell/booklet.lua) as its example.

`desk publish <window> <name>` also accepts a program window on one of your own instances. `desk unpublish <name>` withdraws the link and its visits. The Lua program keeps running.

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
