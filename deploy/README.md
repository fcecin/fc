# Deployment

An fc install has a web box, which runs Caddy, the site, the hub, the bridge, `ceshd`, `fc-shelld` and the site's ledger (`fc-ces`), and network servers, which are CES servers with compute on that run the people's programs. All the servers peer with each other by reserves.

## 1. Configs

On a machine with `ces`, run this, and keep the output private:

    tools/netconf.py --ces /path/to/ces --ledger-host <domain> --net-host <net-host> --out <dir>

It writes `ledger.toml`, `net1.toml` to `netN.toml` and two reserve scripts, and prints each server's address and key.

## 2. Network servers

1. Copy the `net*.toml` files to `/etc/fcnet/` (root, 0600), make `/var/lib/fcnet/net1` to `netN`, and make the no-login user `cesluad`.
2. Run `reserves-net.sh` once.
3. Install `systemd/fcnet@.service`, and run `systemctl enable --now fcnet@net1` for each server.
4. Open the servers' UDP ports, and keep them out of the ephemeral range (`net.ipv4.ip_local_reserved_ports`).

## 3. The web box

1. Make the users `fc` and `fc-keys`, with `fc-keys` in the group `fc`.
2. In `/etc/fc/`, owned by `fc-keys` and readable only by it, put `ces.toml` (the `ledger.toml`), `master.key` (`openssl rand -hex 32`), `reader.key` (a private key from `ces --genkeypair`) and `think.key` (the model's API key). Back up `master.key`, since it seals the people's keys.
3. Fill in `/etc/fc/fc.env` and `/etc/fc/bridge.env` from the examples, and run `reserves-ledger.sh` once as `fc-keys`. `FC_GRANT` in `bridge.env` sets the grant to each new key.
4. Install `ces` built with `--think` in `/opt/fc/ces/`, `fc-bridge` in `/opt/fc/bridge/`, and `ceshd` (Linux 6.7 or later) in `/opt/fc/ceshd/` with the no-login user `ceshd`.
5. Copy `app/` to `/opt/fc/app/`, make the venv in `/opt/fc/venv`, and run `manage.py migrate --run-syncdb` and `collectstatic`.
6. Install the units in `systemd/` and the `Caddyfile`, start them, enable `fc-walk.timer`, and open the ledger's UDP port.
7. Put the ces source of the commit that `bridge/CMakeLists.txt` pins in `/opt/fc/library/ces`, readable by all (`git fetch --depth 1` of that commit). The shells read it at `/library/ces`.

## 4. Credit

`tools/credit.sh --server <host>:<port> --key-file <server key> <key> <credits>` gives credit from a server's account, which refills to 2^46 raw units every day. Give the reader key credit on every network server, since it pays for what the site reads. `manage.py fc_who EMAIL` prints an account's key.

## 5. Updates

Copy `app/`, run `migrate --run-syncdb` and `collectstatic`, and `systemctl reload fc-app`, which lets running requests finish. fc keeps no migrations: after a change to the models, delete the database and run `migrate --run-syncdb` again, which starts it empty. Restart `fc-bridge`, `fc-ceshd` or `fc-hub` only when their code changed, and the shells reconnect to a restarted hub. When the ces pin of the bridge changes, put that commit in `/opt/fc/library/ces`.
