"""The walk: map the CES network's graph once (`walk` in fc/net.py), every
hour from a systemd timer."""

import logging

from django.core.management.base import BaseCommand

from fc import net


class Command(BaseCommand):
    help = "Walk the peer tables of the CES network once, and tell the open pages when the graph changed."

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO)
        net.walk()
