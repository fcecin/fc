"""Security headers for fc's pages."""

from urllib.parse import urlparse

from django.conf import settings


def csp():
    """The pages' CSP. In development the hub has a ws:// address of its own."""
    connect = "'self'"
    hub = urlparse(settings.FC_HUB_URL)
    if hub.scheme in ("ws", "wss") and hub.netloc:
        connect += f" {hub.scheme}://{hub.netloc}"
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src {connect}; frame-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'; object-src 'none'")


class SecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = csp()

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.csp)
        response.setdefault("X-Frame-Options", "DENY")
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", "same-origin")
        return response
