"""The stats process, run by the unit fc-stats: it writes the site's report
(fc/stats.py) to FC_STATS_FILE every REPORT_SECS."""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from fc import stats


class Command(BaseCommand):
    help = "Write the stats of the site to FC_STATS_FILE, again and again. --once writes it one time."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO)
        if opts["once"]:
            stats.write(stats.build()[0], settings.FC_STATS_FILE)
            return
        stats.loop(settings.FC_STATS_FILE)
