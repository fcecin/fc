"""Prints the CES key of an account: manage.py fc_who EMAIL|HANDLE."""

from django.core.management.base import BaseCommand, CommandError

from fc.models import Profile


class Command(BaseCommand):
    help = "Print the CES key of an account, by email or handle."

    def add_arguments(self, parser):
        parser.add_argument("who")

    def handle(self, *args, who, **opts):
        p = (Profile.objects.filter(user__email__iexact=who).first()
             or Profile.objects.filter(handle=who.lower()).first())
        if p is None:
            raise CommandError(f"no account for {who}")
        self.stdout.write(p.pubkey)
