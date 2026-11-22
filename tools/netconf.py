#!/usr/bin/env python3
"""Writes the CES configs of an fc network: the web box's ledger and N
network servers on one host, all peered by reserves. --main also peers them
with a server that already runs on the host, whose operator adds them to its
peers and gives them reserves there.

    tools/netconf.py --ces PATH/TO/ces --ledger-host fc.example --net-host net.example \
        --main net.example:53830=PUBKEY --out DIR

Each server gets a block of 1000 ports (main = base, rpc = base + 1, compute
from base + 100), so reserve the blocks out of the kernel's ephemeral range.
DIR gets the configs with new keys, and reserves-ledger.sh and
reserves-net.sh to run once on each host before the first start. Keep DIR
private.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path

COMMON = """log_level = "info"
data_dir = "{data}"
port = {port}
server_name = "{host}:{port}"
min_difficulty = 15
pow_delay = 0
spend_slot_size = 3600
# fc gives credit by grants, so the server runs no RandomX engine.
no_pow_engine = true
cache_only_pow = true
threads = 2
min_accounts = 65536
max_accounts = 4194304
min_assets = 65536
max_assets = 1048576
flush_value = 10000
max_log_size_gb = 2
fee_account = -1
fee_asset = 38400000
fee_tx = 320000
fee_query = 80000
fee_vm_mult = -1
peer_target = 0
max_peer_reserve_disturbance = 100000
server_key = "{priv}"
"""

NET = """
rpc_port = {rpc}
rpc_max_pending = 1000
rpc_response_timeout_ms = 30000
file_store_max_bytes = {store}
# Compute on, at the floor price. A Lua runtime instance uses about 10 MB, the
# worst case of RAM is instances x mem_max, and every instance takes two ports
# (its client and its rpc host).
compute_max_instances = {instances}
compute_process_mem_max = {memmax}
compute_port_base = {cport}
compute_port_count = {ports}
fee_compute_slot_sec = 1
fee_compute_rss_byte_day = 1
fee_compute_cpu_sec = 50000
fee_bucket_byte_sec = 1

[cesplex_mounts]
"/ces/file/1"    = "builtin:file"
"/ces/lua/1"     = "builtin:lua"
"/ces/compute/1" = "builtin:compute"
"/ces/peer/1"    = "builtin:peer"

[rpc_rudp]
max_channels_per_peer = 4
max_reorder_bytes_per_channel = -1
max_reorder_msgs_per_channel = -1
channel_idle_secs = 180
"""


LEDGER = """
# builtin:think: the site's chat, paid from each person's credit on the ledger,
# which only keeps credit. The model's API key is in {thinkkey} (fc-keys, mode
# 0400). The prices are the main CES server's peg, in raw units per 1M tokens,
# and the context window comes from the model API, capped at 128K tokens.
rpc_port = {rpc}
rpc_max_pending = 1000
rpc_response_timeout_ms = 30000
think_base_url        = "https://api.deepseek.com"
think_model           = "deepseek-flash"
think_api_key_file    = "{thinkkey}"
think_reasoning       = false
think_fee_in          = 600000000000
think_fee_cached_in   = 12000000000
think_fee_out         = 2400000000000
think_max_out_tokens  = 4096
think_max_input_bytes = 1048576
think_context_tokens  = 131072
think_timeout_secs    = 300
think_max_concurrent  = 8
# The main CES server's global subsidy: up to 99.99% off while nobody thinks.
# It also shrinks a call's hold, so an account with little credit can start a
# call. The worst case is 2000 credits at once and 1000 credits a day.
think_subsidy_budget   = 200000000000
think_subsidy_tau_secs = 172800
think_subsidy_floor_bp = 1

[cesplex_mounts]
"/ces/think/1" = "builtin:think"

# The channel cap is high because the site's chat opens many channels at once
# from one address; 4 broke its bursts of calls.
[rpc_rudp]
max_channels_per_peer = 64
max_reorder_bytes_per_channel = -1
max_reorder_msgs_per_channel = -1
channel_idle_secs = 600
"""


def genkey(ces):
    out = subprocess.run([ces, "--genkeypair"], capture_output=True, text=True, check=True).stdout
    return (re.search(r"Private Key:\s*(\w+)", out).group(1), re.search(r"Public Key:\s*(\w+)", out).group(1))


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--ces", required=True, help="the ces binary, to make keys")
    a.add_argument("--ledger-host", required=True)
    a.add_argument("--ledger-port", type=int, default=54000)
    a.add_argument("--ledger-data", default="/var/lib/fc-ces")
    a.add_argument("--net-host", required=True)
    a.add_argument("--net-base", type=int, default=55000,
                   help="port block of net1; netK uses base + 1000(K-1): main, rpc +1, compute +100")
    a.add_argument("--net-data", default="/var/lib/fcnet")
    a.add_argument("--n", type=int, default=4)
    a.add_argument("--main", metavar="HOST:PORT=PUBKEY", help="a CES server that runs already, to peer with")
    a.add_argument("--instances", type=int, default=200)
    a.add_argument("--mem-max", type=int, default=33554432)
    a.add_argument("--store", type=int, default=40 * 2**30)
    a.add_argument("--reserve", type=int, default=2**46,
                   help="raw trust in each peer: 2^46, the middle of the 48-bit balance range")
    a.add_argument("--think-key", default="/etc/fc/think.key",
                   help="the model API key file of the ledger's builtin:think")
    a.add_argument("--out", required=True)
    o = a.parse_args()

    main = None
    if o.main:
        addr, _, pub = o.main.partition("=")
        host, _, port = addr.rpartition(":")
        if not (host and port.isdigit() and re.fullmatch(r"[0-9a-f]{64}", pub)):
            a.error("--main is HOST:PORT=PUBKEY")
        main = dict(name="main", host=host, port=int(port), pub=pub)
    out = Path(o.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    servers = []
    priv, pub = genkey(o.ces)
    servers.append(dict(name="ledger", host=o.ledger_host, port=o.ledger_port,
                        data=o.ledger_data, priv=priv, pub=pub))
    for k in range(1, o.n + 1):
        priv, pub = genkey(o.ces)
        base = o.net_base + 1000 * (k - 1)
        servers.append(dict(name=f"net{k}", host=o.net_host, port=base,
                            data=f"{o.net_data}/net{k}", priv=priv, pub=pub, cport=base + 100))
    for s in servers:
        text = COMMON.format(**s)
        if s["name"] == "ledger":
            text += LEDGER.format(rpc=s["port"] + 1, thinkkey=o.think_key)
        else:
            text += NET.format(rpc=s["port"] + 1, store=o.store, instances=o.instances,
                               memmax=o.mem_max, cport=s["cport"], ports=2 * o.instances)
        for p in servers + ([main] if main else []):
            if p is not s:
                text += f'\n[[peers]]\nkey = "{p["pub"]}"\naddress = "{p["host"]}:{p["port"]}"\n'
        f = out / f"{s['name']}.toml"
        f.write_text(text)
        os.chmod(f, 0o600)
    for host, names, conf in (("ledger", ["ledger"], "/etc/fc/ces.toml"),
                              ("net", [s["name"] for s in servers[1:]], "/etc/fcnet/{name}.toml")):
        lines = ["#!/bin/sh", "# Gives every peer its reserve. Run once, before the first start.", "set -e"]
        for n in names:
            for p in servers + ([main] if main else []):
                if p["name"] != n:
                    lines.append(f'ces --config {conf.format(name=n)} credit {o.reserve} {p["pub"]}')
        f = out / f"reserves-{host}.sh"
        f.write_text("\n".join(lines) + "\n")
        os.chmod(f, 0o700)
    for s in servers:
        print(f"{s['name']}\t{s['host']}:{s['port']}\t{s['pub']}")


if __name__ == "__main__":
    main()
