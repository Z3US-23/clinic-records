from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from apps.core.checks import check_site_url_host_is_allowed
from config.settings import _site_url


class SiteUrlSettingTests(SimpleTestCase):
    """SITE_URL goes into every confirmation link we send patients: a wrong one must stop the app starting."""

    def test_development_default(self):
        self.assertEqual(_site_url("", debug=True), "http://127.0.0.1:8000")
        self.assertEqual(_site_url(None, debug=True), "http://127.0.0.1:8000")
        self.assertEqual(_site_url("http://localhost:8000/", debug=True), "http://localhost:8000")

    def test_required_in_production(self):
        for value in ("", None, "   "):
            with self.subTest(value=value), self.assertRaisesMessage(ImproperlyConfigured, "Set SITE_URL"):
                _site_url(value, debug=False)

    def test_production_needs_the_public_https_address(self):
        for value in (
            "http://clinic.example.com",  # not https
            "https://localhost",
            "https://127.0.0.1:8000",
            "https://0.0.0.0",
            "https://clinic.example.com/app",  # links would become /app/c/<token>/ by mistake
            "https://clinic.example.com/?x=1",
            "clinic.example.com",  # no scheme
            "https://",
        ):
            with self.subTest(value=value), self.assertRaisesMessage(ImproperlyConfigured, "SITE_URL must be"):
                _site_url(value, debug=False)

    def test_good_production_address(self):
        self.assertEqual(_site_url("https://x.onrender.com/", debug=False), "https://x.onrender.com")
        self.assertEqual(_site_url("  https://clinic.example.com  ", debug=False), "https://clinic.example.com")


class SiteUrlDeployCheckTests(SimpleTestCase):
    @override_settings(SITE_URL="https://clinic.example.com", ALLOWED_HOSTS=["clinic.example.com"])
    def test_no_warning_when_the_host_is_allowed(self):
        self.assertEqual(check_site_url_host_is_allowed(None), [])

    @override_settings(SITE_URL="https://clinic.example.com", ALLOWED_HOSTS=[".example.com"])
    def test_subdomain_pattern_counts(self):
        self.assertEqual(check_site_url_host_is_allowed(None), [])

    @override_settings(SITE_URL="https://new-name.onrender.com", ALLOWED_HOSTS=["old-name.onrender.com"])
    def test_warns_when_links_would_be_refused(self):
        warnings = check_site_url_host_is_allowed(None)
        self.assertEqual([w.id for w in warnings], ["core.W001"])
        self.assertIn("new-name.onrender.com", warnings[0].hint)
