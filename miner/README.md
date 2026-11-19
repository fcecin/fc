# fc miner

A Qt app that mines CES credit for a person on an fc site, on Linux, macOS and Windows.

## What it does

The miner makes its own key on first start and keeps it in its data folder (`~/.local/share/fc/fc miner/miner.key` on Linux), and mines to that key on the mine server. With HOME on and a handle set, it looks up the handle's key and the site's ledger, and after every solution it moves its credit to the person's key on that ledger, less a small reserve for fees. While the site is down, the credit waits in the miner's key.

The window shows what the miner is doing, and everything it knows about the server, its own key and what went home.

## Build

It needs Qt 6 (Widgets, Network), Boost and a C++20 compiler, and links the ces client library (`CES_CLIENT_ONLY`).

    cmake -B build -S miner -DCMAKE_PREFIX_PATH=<Qt 6 prefix> \
      -DFC_SITE=https://<site> -DFC_MINE_SERVER=<host>:<port>
    cmake --build build -j --target fc-miner

The build fetches ces at `CES_TAG`, or uses `-DCES_SOURCE_DIR=<ces checkout>`.

`-DFC_STATIC=ON` with a static Qt makes one executable that holds Qt, Boost and the C++ runtime. On Linux it still uses the system's C library, X11, GL and fontconfig. CI builds it this way on all three systems, and checks what each executable links.

Qt is LGPL, and the miner's source is public, so anyone can relink it with another Qt.

To release, push a tag `miner-v<version>`. CI builds the three executables, and when all three pass, it attaches them and their `SHA256SUMS` to a GitHub Release for that tag.

## Files

- `src/engine.*` mines, reads the balance and moves the credit, on its own thread.
- `src/site.*` asks the site for a handle's key and for the ledger.
- `src/lcd.*` draws the display.
- `src/window.*` is the window.
