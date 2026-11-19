"""The handle, the person's name, which they choose on their first visit."""

from django.core.exceptions import ValidationError

from .models import Profile, validate_handle


def clean_handle_value(value):
    handle = (value or "").strip().lower()
    validate_handle(handle)
    if Profile.objects.filter(handle=handle).exists():
        raise ValidationError("Someone already has this handle. Choose another one.")
    return handle
