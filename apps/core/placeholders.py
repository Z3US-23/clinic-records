"""Temporary stand-in view so every URL name resolves while features are being built.

Delete a URL's use of `placeholder` once its real view exists.
"""

from django.http import HttpResponse


def placeholder(request, *args, **kwargs):
    return HttpResponse("This page is not built yet.", status=501, content_type="text/plain")
