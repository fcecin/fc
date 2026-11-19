"""The CES network, as this site finds it.

Every WALK_SECS (an hour), the walk (`manage.py fc_walk`) follows the peer
tables from the ledger and every person's server, up to MAX_SERVERS, and
caches the graph. A server's state is read only when a person looks
(/api/net), and `layout` gives every page the same sky for the same graph.
"""

import hashlib
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache

from . import bridge, push

WALK_SECS = 3600
MAX_SERVERS = 64               # the most servers on the graph
PARALLEL = 16                  # servers that the walk reads at once
GRAPH_KEY = "net:graph"        # {address: [the addresses in its peer table]}
GRAPH_TTL = 2 * WALK_SECS
INFO_TTL = 30                  # how long a server's state stays cached
COMPUTE_TTL = 300
PROBE_PATH = "/p/fc-probe.lua"
# The layout, in sky units (a planet's radius is 30 in sky.js).
EDGE = 300.0                   # the length of a link
GAP = 200.0                    # the least distance between two servers, room for a planet and its name
HEIGHT = (-60.0, 80.0)         # the planets float between these heights
STEPS = 300                    # steps of the force layout

log = logging.getLogger("fc.net")


# --- One server, asked now -----------------------------------------------------

def _ping(address):
    """The free GetInfo: pubkey, rpc_port, tps. None if it doesn't answer."""
    try:
        r = bridge.call("info", server=address)
        return {"pubkey": r["pubkey"], "rpc_port": r["rpc_port"], "tps": r["tps"]}
    except bridge.BridgeError:
        return None


def _peers(address):
    try:
        return [p["address"] for p in bridge.call("peers", server=address, max=32)["peers"]]
    except bridge.BridgeError:
        return []


def _probe(address, i):
    """True if the server runs programs. The reader key asks for a path's
    instances, which only a server with compute answers."""
    if not i or not i["rpc_port"]:
        return False
    try:
        bridge.call("instances", server=address, path=PROBE_PATH)
        return True
    except bridge.BridgeError:
        return False


# --- A server's state ------------------------------------------------------

def info(address, fresh=False):
    """A server's GetInfo, cached or read now. None if it doesn't answer."""
    key = f"net:info:{address}"
    r = None if fresh else cache.get(key)
    if r is None:
        r = _ping(address) or {}
        cache.set(key, r, INFO_TTL)
    return r or None


def compute(address, fresh=False):
    """True if the server runs programs, cached or read now."""
    key = f"net:compute:{address}"
    r = None if fresh else cache.get(key)
    if r is None:
        r = _probe(address, info(address))
        cache.set(key, r, COMPUTE_TTL)
    return r


def refresh(address):
    info(address, fresh=True)
    compute(address, fresh=True)


# --- The graph -----------------------------------------------------------------

def graph():
    return cache.get(GRAPH_KEY)


def peers(address):
    """The addresses in a server's peer table, from the graph, or read now
    for a server that the last walk missed."""
    g = graph()
    if g is not None and address in g:
        return g[address]
    key = f"net:peers:{address}"
    r = cache.get(key)
    if r is None:
        r = _peers(address)
        cache.set(key, r, INFO_TTL)
    return r


def around(address):
    """A server and the neighbors in its peer table. Each node is {address,
    ledger, main, up, compute, pos}, with pos its place in the sky, and the
    links go from the server to each neighbor."""
    ledger, main = settings.FC_LEDGER, settings.FC_MAIN_SERVER
    near = [address] + [p for p in peers(address) if p != address]
    g = dict(graph() or {})
    g.setdefault(address, near[1:])
    pos = places(g)
    nodes = []
    for a in near:
        up = info(a) is not None
        nodes.append({"address": a, "ledger": a == ledger, "main": bool(main) and a == main, "up": up,
                      "compute": up and compute(a), "pos": pos[a]})
    return {"nodes": nodes, "edges": [[address, x["address"]] for x in nodes[1:]]}


# --- The layout ----------------------------------------------------------------

def _unit(address, what):
    """A number in [0, 1) from the address, the same for the same address and
    `what`."""
    return int.from_bytes(hashlib.sha256(f"{what} {address}".encode()).digest()[:8], "big") / 2.0**64


def layout(g):
    """A place [x, y, z] for each server of the graph `g` ({address: [its
    peers]}), from a force layout seeded by the addresses, with links pulling
    to EDGE and every two servers at least GAP apart."""
    nodes = sorted(set(g) | {p for ps in g.values() for p in ps})
    at = {a: i for i, a in enumerate(nodes)}
    links = sorted({tuple(sorted((at[a], at[b]))) for a, ps in g.items() for b in ps if a != b})
    n = len(nodes)
    x = [EDGE * (0.4 + _unit(a, "r")) * math.cos(2 * math.pi * _unit(a, "a")) for a in nodes]
    z = [EDGE * (0.4 + _unit(a, "r")) * math.sin(2 * math.pi * _unit(a, "a")) for a in nodes]

    def apart(i, j):
        """The line from j to i, and its length, always above 0."""
        dx, dz = x[i] - x[j], z[i] - z[j]
        d = math.hypot(dx, dz)
        if d < 1e-6:                  # two servers in one place: their addresses give a direction
            t = 2 * math.pi * _unit(nodes[i] + " " + nodes[j], "a")
            return math.cos(t), math.sin(t), 1e-6
        return dx / d, dz / d, d

    heat = EDGE
    for _ in range(STEPS):
        fx, fz = [0.0] * n, [0.0] * n
        for i in range(n):
            for j in range(i + 1, n):
                ux, uz, d = apart(i, j)
                f = EDGE * EDGE / d
                fx[i] += ux * f; fz[i] += uz * f
                fx[j] -= ux * f; fz[j] -= uz * f
        for i, j in links:
            ux, uz, d = apart(i, j)
            f = d * d / EDGE
            fx[i] -= ux * f; fz[i] -= uz * f
            fx[j] += ux * f; fz[j] += uz * f
        for i in range(n):
            # A weak pull to the middle keeps the graph's parts together.
            fx[i] -= 0.05 * x[i]; fz[i] -= 0.05 * z[i]
            m = math.hypot(fx[i], fz[i])
            if m > 0:
                s = min(m, heat) / m
                x[i] += fx[i] * s; z[i] += fz[i] * s
        heat = max(1.0, heat * 0.98)
    for _ in range(200):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                ux, uz, d = apart(i, j)
                if d < GAP:
                    k = (GAP - d) / 2 + 0.5
                    x[i] += ux * k; z[i] += uz * k
                    x[j] -= ux * k; z[j] -= uz * k
                    moved = True
        if not moved:
            break
    lo, hi = HEIGHT
    return {a: [round(x[i]), round(lo + (hi - lo) * _unit(a, "y")), round(z[i])] for i, a in enumerate(nodes)}


def places(g):
    """The layout of `g`, from the cache when the graph is the same."""
    key = "net:layout:" + hashlib.sha256(json.dumps(g, sort_keys=True).encode()).hexdigest()
    r = cache.get(key)
    if r is None:
        r = layout(g)
        cache.set(key, r, GRAPH_TTL)
    return r


def walk():
    """Map the graph: from the ledger and every person's server, follow the
    peer tables one level at a time, reading each level at once. When the
    graph changed, every open page gets {"net": true}. Returns the graph."""
    from .models import Profile
    start = [settings.FC_LEDGER] + list(Profile.objects.exclude(server="").values_list("server", flat=True))
    g = {}
    level = list(dict.fromkeys(start))
    with ThreadPoolExecutor(PARALLEL) as pool:
        while level and len(g) < MAX_SERVERS:
            level = level[:MAX_SERVERS - len(g)]
            for a, p in zip(level, pool.map(_peers, level)):
                g[a] = p
            level = list(dict.fromkeys(x for a in level for x in g[a] if x not in g))
    old = graph()
    cache.set(GRAPH_KEY, g, GRAPH_TTL)
    places(g)
    log.info("the walk found %d servers", len(g))
    if old is not None and json.dumps(old, sort_keys=True) != json.dumps(g, sort_keys=True):
        push.send("all", {"net": True})
        log.info("the graph changed")
    return g
