"""URLconf used only by the error-page tests: the real site plus one view that crashes."""

from django.urls import include, path


def crash(request):
    raise RuntimeError("Deliberate crash for the 500 page test")


urlpatterns = [
    path("test-crash/", crash),
    path("", include("config.urls")),
]
