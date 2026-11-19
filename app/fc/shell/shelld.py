#!/usr/bin/python3
"""fc-shelld: starts and stops a person's shell unit, as root.

The hub asks on a Unix socket (SOCK_SEQPACKET, one JSON message each way),
{"start": UID}, {"stop": UID} or {"list": true}, and only a peer of the user
FCSHELLD_PEER gets an answer. The unit fcsh-u<uid>.service has fixed settings
(UNIT), its own uid, its home and no network, and its only way out is
/run/fc-shells/u<uid>, which it sees as /run/fcsh.
"""

import json
import os
import pwd
import re
import shutil
import socket
import struct
import subprocess
import sys

RUN = "/run/fc-shells"           # one directory per shell, for the hub's socket
CODE = "/opt/fc/app/fc/shell"    # ptyhost.py and bin/, which the unit sees as /opt/fcsh
ROOT = "/var/lib/fc-shells-root"  # the file system that a shell sees (skeleton())
STATE = "/var/lib/private/fc-shells"  # the homes, where systemd hands them to the units' users
HOMES = "/var/lib/fc-shells-homes"    # the home images, one per person
LIBRARY = "/opt/fc/library"      # the site's files for every shell, read-only at /library
IMAGE_MIB = 22                   # 16 MiB free after ext4's journal and tables
MAX_MSG = 4096
UNIT = re.compile(r"^fcsh-u(\d+)\.service$")

# Every shell unit's settings, where only the user id comes from a request.
# The unit sees ROOT as its /, with the host's /usr, a few files of /etc and
# its home.
SETTINGS = [
    f"RootDirectory={ROOT}",
    "MountAPIVFS=yes",
    "BindReadOnlyPaths=/usr /etc/alternatives",
    f"BindReadOnlyPaths={CODE}:/opt/fcsh",
    f"BindReadOnlyPaths=-{LIBRARY}:/library",      # "-": a box with no library skips it
    # A fresh /h and /var in every unit, with its home on top, so other homes'
    # mount points stay hidden. They live in memory, smaller than MemoryMax, so a
    # big write there fails with "no space" and the shell survives.
    "TemporaryFileSystem=/h:size=1M /var:size=16M",
    "DynamicUser=yes",
    "UMask=0077",
    "PrivateNetwork=yes",
    "RestrictAddressFamilies=AF_UNIX",
    "PrivateDevices=yes",
    # DynamicUser gives every unit a private /tmp, a directory under the box's
    # /tmp, on disk.
    "PrivateTmp=yes",
    "ProtectSystem=strict",
    # Writes go to the home, whose image caps them, and not to the box's disk.
    # The "+" makes /tmp the shell's own, inside RootDirectory.
    "ReadOnlyPaths=+/tmp",
    "ProtectHome=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectProc=invisible",
    "ProcSubset=pid",
    "LockPersonality=yes",
    "RestrictNamespaces=yes",
    "RestrictRealtime=yes",
    "RestrictSUIDSGID=yes",
    "SystemCallArchitectures=native",
    # The system calls of a usual service only, to shrink the attack surface.
    "SystemCallFilter=@system-service",
    "SystemCallErrorNumber=EPERM",
    "CapabilityBoundingSet=",
    "PrivateIPC=yes",
    "MemoryMax=256M",
    "MemorySwapMax=0",
    "TasksMax=64",
    "CPUQuota=50%",
    "LimitFSIZE=64M",
    "LimitNOFILE=1024",
    "LimitCORE=0",
    "KillMode=control-group",
    "TimeoutStopSec=5",
]
ENV = {
    "PATH": "/opt/fcsh/bin:/usr/local/bin:/usr/bin:/bin",
    "TERM": "xterm-256color",
    "LANG": "C.UTF-8",
    "FCSH_SOCKET": "/run/fcsh/hub.sock",
}


# A shell's own /etc, apart from the host's.
ETC = {
    "passwd": "root:x:0:0:root:/root:/usr/sbin/nologin\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n",
    "group": "root:x:0:\nnogroup:x:65534:\n",
    "nsswitch.conf": "passwd: files systemd\ngroup: files systemd\nshadow: files\nhosts: files\n",
    "hosts": "127.0.0.1 localhost\n::1 localhost\n",
    "bash.bashrc": "[ -z \"$PS1\" ] && return\nshopt -s checkwinsize\n",
    "profile": "export PATH LANG TERM\n[ -n \"$BASH\" ] && [ -f ~/.bashrc ] && . ~/.bashrc\n",
}
# Public files of the host's /etc that the loader, readline and Python need,
# copied in.
ETC_COPY = ["inputrc", "ld.so.conf", "ld.so.conf.d", "ld.so.cache", "os-release", "shells", "python3.12"]


def skeleton(root=ROOT, etc="/etc"):
    """Build the file system that a shell sees, with empty mount points, the
    links of a merged /usr and its /etc, at every start of fc-shelld."""
    for d in ("h", "var"):
        # Empty mount points that older units left here.
        for dirpath, _, _ in os.walk(os.path.join(root, d), topdown=False):
            if dirpath != os.path.join(root, d):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    for d in ("usr", "etc", "etc/alternatives", "h", "tmp", "var", "dev", "proc", "sys",
              "run/fcsh", "opt/fcsh", "library"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for name in ("bin", "sbin", "lib", "lib64"):
        link = os.path.join(root, name)
        if not os.path.islink(link):
            os.symlink(f"usr/{name}", link)
    for name, text in ETC.items():
        with open(os.path.join(root, "etc", name), "w") as f:
            f.write(text)
    for name in ETC_COPY:
        src, dst = os.path.join(etc, name), os.path.join(root, "etc", name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        elif os.path.exists(src):
            shutil.copy2(src, dst)
    zone = os.path.join(root, "etc", "localtime")
    if not os.path.lexists(zone):
        os.symlink("/usr/share/zoneinfo/UTC", zone)
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o1777 if dirpath.endswith("/tmp") else 0o755)


def unit(uid):
    return f"fcsh-u{uid}.service"


def command(uid):
    """The systemd-run command that starts the shell of `uid`."""
    home = f"/h/u{uid}"         # the home inside the shell's root, since ProtectHome hides /home
    env = {**ENV, "HOME": home, "USER": f"u{uid}", "TMPDIR": f"{home}/.tmp"}
    args = ["systemd-run", f"--unit={unit(uid)}", f"--description=fc shell of user {uid}",
            "--slice=fc-shells.slice", "--collect", "--quiet",
            "-p", f"User=fcsh-u{uid}",
            # systemd makes the home on the host for the unit's uid, and the
            # bind puts it in the shell's root.
            "-p", f"StateDirectory=fc-shells/u{uid}",
            "-p", f"BindPaths=/var/lib/private/fc-shells/u{uid}:{home}",
            "-p", f"WorkingDirectory={home}",
            "-p", f"BindPaths={RUN}/u{uid}:/run/fcsh"]
    for s in SETTINGS:
        args += ["-p", s]
    for k, v in env.items():
        args += ["--setenv", f"{k}={v}"]
    return args + ["/usr/bin/python3", "/opt/fcsh/ptyhost.py"]


def mount_home(uid, state=STATE, homes=HOMES, run=subprocess.run, mounted=os.path.ismount):
    """Mount the home image of `uid` on its home, and make the image at the
    first start. A plain directory in its place goes."""
    home, image = os.path.join(state, f"u{uid}"), os.path.join(homes, f"u{uid}.img")
    if mounted(home):
        return
    if os.path.isdir(home):
        shutil.rmtree(home)
    os.makedirs(home, mode=0o700)
    if not os.path.exists(image):
        os.makedirs(homes, mode=0o700, exist_ok=True)
        with open(image, "wb") as f:
            f.truncate(IMAGE_MIB << 20)
        run(["mkfs.ext4", "-q", "-F", "-m", "0", image], check=True, capture_output=True)
    run(["mount", "-o", "loop,nosuid,nodev", image, home], check=True, capture_output=True)


def wipe(uid, state=STATE, homes=HOMES, run=subprocess.run, mounted=os.path.ismount):
    """The person logged out: stop the shell of `uid`, and delete its home."""
    home, image = os.path.join(state, f"u{uid}"), os.path.join(homes, f"u{uid}.img")
    run(["systemctl", "stop", unit(uid)], capture_output=True)
    if mounted(home):
        run(["umount", home], check=True, capture_output=True)
    if os.path.isdir(home):
        shutil.rmtree(home)
    if os.path.exists(image):
        os.unlink(image)


def active(uid):
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit(uid)])
    return r.returncode == 0


def shells():
    r = subprocess.run(["systemctl", "list-units", "--type=service", "--state=active",
                        "--plain", "--no-legend", "fcsh-u*.service"],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        m = UNIT.match(line.split()[0]) if line.split() else None
        if m:
            out.append(int(m.group(1)))
    return out


def directory(uid, owner):
    """The directory of `uid`'s shell, where the hub (`owner`) listens, and
    which the unit sees as /run/fcsh."""
    d = f"{RUN}/u{uid}"
    os.makedirs(d, exist_ok=True)
    os.chown(d, owner.pw_uid, owner.pw_gid)
    os.chmod(d, 0o755)
    return d


def handle(req, owner):
    if req.get("list") is True:
        return {"shells": shells()}
    for verb in ("start", "stop", "wipe"):
        uid = req.get(verb)
        if uid is None:
            continue
        if not isinstance(uid, int) or isinstance(uid, bool) or not 0 < uid < 2**31:
            return {"error": "give a user id"}
        if verb == "stop":
            subprocess.run(["systemctl", "stop", unit(uid)], capture_output=True)
            return {"ok": True}
        if verb == "wipe":
            wipe(uid)
            return {"ok": True}
        if active(uid):
            return {"ok": True, "was": "running"}
        mount_home(uid)
        directory(uid, owner)
        r = subprocess.run(command(uid), capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout or "systemd-run failed").strip()[-300:]}
        return {"ok": True}
    return {"error": "give start, stop, wipe or list"}


def peer_uid(conn):
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def serve(path, owner):
    skeleton()
    os.makedirs(RUN, exist_ok=True)
    os.chown(RUN, 0, owner.pw_gid)
    os.chmod(RUN, 0o750)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.bind(path)
    os.chown(path, 0, owner.pw_gid)
    os.chmod(path, 0o660)
    s.listen(16)
    print(f"fc-shelld: ready on {path}", flush=True)
    while True:
        conn, _ = s.accept()
        with conn:
            conn.settimeout(10)
            try:
                if peer_uid(conn) != owner.pw_uid:
                    conn.send(json.dumps({"error": "not the hub"}).encode())
                    continue
                req = json.loads(conn.recv(MAX_MSG))
                reply = handle(req if isinstance(req, dict) else {}, owner)
            except (OSError, ValueError) as e:
                reply = {"error": str(e)}
            try:
                conn.send(json.dumps(reply).encode())
            except OSError:
                pass


def main():
    owner = pwd.getpwnam(os.environ.get("FCSHELLD_PEER", "fc"))
    serve(os.environ.get("FCSHELLD_SOCKET", "/run/fc-shelld/shelld.sock"), owner)


if __name__ == "__main__":
    sys.exit(main())
