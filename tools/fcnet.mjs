#!/usr/bin/env node
// fcnet: a local CES network for fc, with one ledger (the site's CES server,
// compute off) and N network servers (file store and compute on, at the floor
// fee), all peered with each other by reserves.
//
//   fcnet init [--dir D] [--n 5] [--base 55100] [--think-key FILE]
//   fcnet up | down | status [--dir D] [--gdb]
//   fcnet restart NAME [--dir D]    restart one server, as "ledger" or "net3"
//
// It lives in ./tmp/net, and takes ces from $CES_BUILD or ../ces/build/debug.
// With --gdb, up and restart run each server under gdb, to catch a hang:
// `kill -INT <server pid>` makes gdb print the stack of every thread into the
// server's log, and the server stops. The pids file keeps gdb's pid.
// With --think-key, the ledger serves builtin:think like the prod ledger: the
// same model and prices, with the model's API key in FILE. That needs a ces
// build with the think backend (build.sh --think).

import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync, openSync, closeSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';

const args = process.argv.slice(2);
const cmd = args.shift();
const opt = (name, dflt) => {
  const i = args.indexOf('--' + name);
  return i >= 0 ? args[i + 1] : dflt;
};
const dir = resolve(opt('dir', 'tmp/net'));
const build = process.env.CES_BUILD || new URL('../../ces/build/debug', import.meta.url).pathname;
const cesBin = join(build, 'ces');
const thinkKey = opt('think-key') ? resolve(opt('think-key')) : null;

// The trust in each peer, as on prod: 2^46 raw, about 703,687 credits. A cross
// to a peer uses the peer's reserve here, so a small reserve stops crosses.
const RESERVE = 2 ** 46;

function genKey() {
  const out = execFileSync(cesBin, ['--genkeypair'], { encoding: 'utf8' });
  return { priv: out.match(/Private Key:\s*(\w+)/)[1], pub: out.match(/Public Key:\s*(\w+)/)[1] };
}

function toml(s, all) {
  const peers = all.filter((p) => p.name !== s.name)
    .map((p) => `[[peers]]\nkey = "${p.pub}"\naddress = "127.0.0.1:${p.port}"\n`).join('\n');
  const common = `log_level = "info"
data_dir = "${s.dataDir}"
port = ${s.port}
server_key = "${s.priv}"
server_name = "127.0.0.1:${s.port}"
min_difficulty = 1
pow_delay = 0
# A test network restarts often: no restart cutoff for nonceless requests.
nonceless_delay = 0
spend_slot_size = 3600
no_pow_engine = true
cache_only_pow = false
threads = 1
min_accounts = 100
max_accounts = 100000
min_assets = 100
max_assets = 100000
flush_value = 0
max_log_size_gb = 1
fee_account = -1
fee_asset = 38400000
fee_tx = 320000
fee_query = 80000
peer_target = 0
peer_miner_interval = 2
`;
  if (s.role === 'ledger') return common + (thinkKey ? think(s) : '') + '\n' + peers;
  return common + `
rpc_port = ${s.port + 1}
file_store_max_bytes = 268435456
compute_max_instances = 16
compute_process_mem_max = 67108864
compute_port_base = ${s.computePortBase}
compute_port_count = 16
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

` + peers;
}

// The think settings of the prod ledger. The channel cap is high because the
// site's chat opens many channels at once from one address.
function think(s) {
  return `
rpc_port = ${s.port + 1}
think_base_url        = "https://api.deepseek.com"
think_model           = "deepseek-flash"
think_api_key_file    = "${thinkKey}"
think_reasoning       = false
think_fee_in          = 600000000000
think_fee_cached_in   = 12000000000
think_fee_out         = 2400000000000
think_max_out_tokens  = 4096
think_max_input_bytes = 1048576
think_context_tokens  = 131072
think_timeout_secs    = 300
think_max_concurrent  = 8
think_subsidy_budget   = 200000000000
think_subsidy_tau_secs = 172800
think_subsidy_floor_bp = 1

[cesplex_mounts]
"/ces/think/1" = "builtin:think"

[rpc_rudp]
max_channels_per_peer = 64
max_reorder_bytes_per_channel = -1
max_reorder_msgs_per_channel = -1
channel_idle_secs = 600
`;
}

function cmdInit() {
  if (existsSync(join(dir, 'fcnet.json'))) throw new Error('already a network: ' + dir);
  if (thinkKey) {
    if (!existsSync(thinkKey)) throw new Error('no key file ' + thinkKey);
    const cache = existsSync(join(build, 'CMakeCache.txt')) ? readFileSync(join(build, 'CMakeCache.txt'), 'utf8') : '';
    if (!cache.includes('CES_THINK:BOOL=ON'))
      throw new Error(build + ' has no think backend: build it with build.sh --think');
  }
  const n = parseInt(opt('n', '5'), 10);
  const base = parseInt(opt('base', '55100'), 10);
  mkdirSync(dir, { recursive: true });
  const servers = [];
  for (let i = 0; i <= n; i++) {
    const name = i === 0 ? 'ledger' : `net${i}`;
    const dataDir = join(dir, name, 'data');
    mkdirSync(dataDir, { recursive: true });
    servers.push({ name, role: i === 0 ? 'ledger' : 'net', port: base + i * 10,
                   computePortBase: base + 400 + i * 20, dataDir, ...genKey() });
  }
  for (const s of servers) {
    writeFileSync(join(dir, `${s.name}.toml`), toml(s, servers));
    const pd = servers.filter((p) => p.name !== s.name)
      .map((p) => `[[peers]]\nkey = "${p.pub}"\naddress = "127.0.0.1:${p.port}"\nreachable = true\nverified = true\noutbound = true\n`)
      .join('\n');
    writeFileSync(join(s.dataDir, 'peerdata.toml'), pd);
  }
  for (const s of servers)
    for (const p of servers)
      if (p.name !== s.name)
        execFileSync(cesBin, ['--config', join(dir, `${s.name}.toml`), 'credit', String(RESERVE), p.pub], { stdio: 'ignore' });
  writeFileSync(join(dir, 'fcnet.json'), JSON.stringify({ servers }, null, 2));
  for (const s of servers) console.log(`${s.name}\t127.0.0.1:${s.port}\t${s.pub}`);
}

function load() { return JSON.parse(readFileSync(join(dir, 'fcnet.json'), 'utf8')); }
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
// The server of a pid in the pids file: gdb's child when gdb runs it.
function server(pid) {
  try {
    if (readFileSync(`/proc/${pid}/comm`, 'utf8').trim() !== 'gdb') return pid;
    const kids = readFileSync(`/proc/${pid}/task/${pid}/children`, 'utf8').trim().split(/\s+/).filter(Boolean);
    return kids.length ? Number(kids[0]) : pid;
  } catch { return pid; }
}
const stop = (pid) => { if (alive(pid)) process.kill(server(pid), 'SIGTERM'); };
const pidFile = () => join(dir, 'fcnet.pids');

// gdb runs the server and passes SIGTERM to it, so a stop keeps its data.
// SIGINT stops the server, and gdb prints the stacks of all its threads.
const GDB = ['-q', '-batch', '-ex', 'set pagination off', '-ex', 'set print thread-events off',
             '-ex', 'handle SIGPIPE nostop noprint pass', '-ex', 'handle SIGTERM nostop noprint pass',
             '-ex', 'handle SIGINT stop print nopass', '-ex', 'run',
             '-ex', 'echo \\n=== fcnet: the stacks of all threads ===\\n', '-ex', 'thread apply all bt',
             '--args'];

function launch(s) {
  const fd = openSync(join(dir, `${s.name}.log`), 'a');
  const argv = ['--config', join(dir, `${s.name}.toml`), '-x'];
  const c = args.includes('--gdb')
    ? spawn('gdb', [...GDB, cesBin, ...argv], { stdio: ['ignore', fd, fd], detached: true, cwd: join(dir, s.name) })
    : spawn(cesBin, argv, { stdio: ['ignore', fd, fd], detached: true, cwd: join(dir, s.name) });
  c.unref(); closeSync(fd);
  console.log(`${s.name}\tpid ${c.pid}`);
  return c.pid;
}

// A restart also refills the server's account.
function cmdRestart() {
  const { servers } = load();
  const name = args.find((a) => !a.startsWith('--') && args[args.indexOf(a) - 1] !== '--dir');
  const i = servers.findIndex((s) => s.name === name);
  if (i < 0) throw new Error('no server ' + name);
  const pids = JSON.parse(readFileSync(pidFile(), 'utf8'));
  stop(pids[i]);
  for (let n = 0; n < 50 && alive(pids[i]); n++) execFileSync('sleep', ['0.2']);
  pids[i] = launch(servers[i]);
  writeFileSync(pidFile(), JSON.stringify(pids));
}

function cmdUp() {
  const { servers } = load();
  if (existsSync(pidFile()) && JSON.parse(readFileSync(pidFile(), 'utf8')).some(alive))
    throw new Error('already up');
  const pids = servers.map(launch);
  writeFileSync(pidFile(), JSON.stringify(pids));
}

function cmdDown() {
  if (!existsSync(pidFile())) return;
  for (const pid of JSON.parse(readFileSync(pidFile(), 'utf8')))
    stop(pid);
  rmSync(pidFile());
}

function cmdStatus() {
  const { servers } = load();
  const pids = existsSync(pidFile()) ? JSON.parse(readFileSync(pidFile(), 'utf8')) : [];
  servers.forEach((s, i) => console.log(`${s.name}\t127.0.0.1:${s.port}\t${pids[i] && alive(pids[i]) ? 'up' : 'down'}`));
}

({ init: cmdInit, up: cmdUp, down: cmdDown, status: cmdStatus, restart: cmdRestart }[cmd] ||
 (() => { console.error('usage: fcnet init|up|down|status|restart NAME [--dir D]'); process.exit(1); }))();
