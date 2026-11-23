"""Put and launch the websites of the browser checks, and publish them.

Run it with the environment of a local fc (the one of `fc.sh web`), from its
`app` directory:

    python tools/websitecheck_setup.py --owner maker --server 127.0.0.1:56110 --out http://127.0.0.1:8797

The owner is a person with a handle. It prints the links as JSON. Each run
launches new instances and stops the old ones."""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fcsite.settings")
import django  # noqa: E402

django.setup()

from fc import account, bridge, publications  # noqa: E402
from fc.models import Profile, Publication  # noqa: E402

FC = Path(__file__).resolve().parent.parent
CREDIT = 100_000_000   # raw units in one credit
SITES = {
    "shop": FC / "app/fc/shell/website/booklet.lua",
    "victim": FC / "tools/test/websites/victim.lua",
    "hostile": FC / "tools/test/websites/hostile.lua",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--out", required=True)
    o = ap.parse_args()
    owner = Profile.objects.get(handle=o.owner)
    _, balance = account.balance(owner, o.server)
    if (balance or 0) < 20 * CREDIT:
        account.move(owner, account.here(owner), o.server, 50 * CREDIT)
    running = bridge.call("ps", sealed=owner.sealed, server=o.server)["instances"]
    links = {}
    for name, source in SITES.items():
        text = source.read_text().replace("@OWNER@", owner.handle).replace("@OUT@", o.out)
        # A fresh store of buyers for the shop, so that the same visitors can buy again.
        text = text.replace('"/booklet.kv"', f'"/wc-booklet-{int(time.time())}.kv"')
        path = f"/h/{owner.pubkey}/wc-{name}.lua"
        for i in running:
            if i["path"] == path:
                bridge.call("kill", sealed=owner.sealed, server=o.server, pid=i["pid"])
        bridge.call("file_put", sealed=owner.sealed, server=o.server, path=path,
                    content_b64=base64.b64encode(text.encode()).decode(), deposit=CREDIT)
        bridge.call("launch", sealed=owner.sealed, server=o.server, path=path)
        Publication.objects.filter(owner=owner, name=f"wc-{name}").delete()
        links[name] = publications.publish(owner, f"wc-{name}", o.server, path)
    print(json.dumps(links))


main()
