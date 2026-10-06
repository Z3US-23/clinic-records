import json

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import get_template
from django.test import Client, override_settings
from django.templatetags.static import static
from django.urls import reverse

from .base import CoreTestCase


class ManifestTests(CoreTestCase):
    url = reverse("core:manifest")

    def test_manifest(self):
        response = self.client.get(self.url)  # public: the browser fetches it before sign-in
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        manifest = json.loads(response.content)
        self.assertEqual(manifest["name"], settings.PRODUCT_NAME)
        self.assertTrue(manifest["short_name"])
        self.assertEqual((manifest["start_url"], manifest["scope"]), ("/", "/"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["theme_color"], "#0f766e")
        self.assertTrue(manifest["background_color"])

        icons = {(icon["sizes"], icon["type"], icon["purpose"]): icon["src"] for icon in manifest["icons"]}
        self.assertEqual(icons[("192x192", "image/png", "any")], static("icons/icon-192.png"))
        self.assertEqual(icons[("512x512", "image/png", "any")], static("icons/icon-512.png"))
        self.assertEqual(icons[("512x512", "image/png", "maskable")], static("icons/maskable-512.png"))
        self.assertEqual(icons[("any", "image/svg+xml", "any")], static("icons/icon.svg"))

    def test_icon_files_exist(self):
        for path in ("icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png", "icons/maskable-512.png"):
            with self.subTest(path=path):
                found = finders.find(path)
                self.assertTrue(found, f"{path} is missing: run scripts/make_icons.py")
                if path.endswith(".png"):
                    with open(found, "rb") as f:
                        self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_read_only(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)


class ServiceWorkerTests(CoreTestCase):
    url = reverse("core:service_worker")

    def test_served_from_the_site_root_with_the_right_headers(self):
        self.assertEqual(self.url, "/sw.js")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/javascript"))
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(response["Cache-Control"], "no-cache")

    def test_signed_in_users_get_the_same_headers(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(self.url)["Cache-Control"], "no-cache")

    def test_precaches_only_design_files_and_the_offline_page(self):
        script = self.client.get(self.url).content.decode()
        precache_line = next(line for line in script.splitlines() if line.startswith("const PRECACHE_URLS"))
        precached = json.loads(precache_line.split("=", 1)[1].strip().rstrip(";"))
        self.assertEqual(
            set(precached),
            {
                static("css/app.css"), static("js/app.js"), static("icons/icon.svg"), static("icons/icon-192.png"),
                static("icons/icon-512.png"), static("icons/maskable-512.png"), reverse("core:offline"),
            },
        )
        # Patient pages and data are never cached; the rule is written down for the next developer.
        self.assertIn("NEVER caches HTML pages or JSON", script)
        self.assertNotIn("/patients/", script)
        self.assertIn('request.mode === "navigate"', script)
        self.assertIn("caches.delete", script)  # old versions are cleaned up

    def test_cache_name_is_versioned(self):
        script = self.client.get(self.url).content.decode()
        self.assertRegex(script, r'const CACHE_NAME = "clinic-records-v\d+-[0-9a-f]{12}";')

    def test_read_only(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)


class OfflinePageTests(CoreTestCase):
    def test_public_offline_page(self):
        response = self.client.get(reverse("core:offline"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "base_public.html")
        self.assertContains(response, "You're offline")
        self.assertContains(response, "Patient records need an internet connection.")


class ErrorPageTests(CoreTestCase):
    def test_404_page(self):
        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(response, "We couldn't find that page", status_code=404)
        self.assertContains(response, f'href="{reverse("core:dashboard")}"', status_code=404)

    def test_404_for_signed_in_staff(self):
        self.login(self.receptionist)
        response = self.client.get("/no-such-page/")
        self.assertContains(response, "Go to Today", status_code=404)

    def test_403_page(self):
        self.login(self.receptionist)
        response = self.client.get(reverse("core:audit_log"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403.html")
        self.assertContains(response, "You don't have access to this page", status_code=403)

    def test_500_template_renders_with_an_empty_context(self):
        html = get_template("500.html").render({})
        self.assertIn("Something went wrong", html)
        self.assertIn('href="/"', html)

    @override_settings(ROOT_URLCONF="apps.core.tests.urls_crash")
    def test_500_page_for_a_crash(self):
        client = Client(raise_request_exception=False)
        with self.assertLogs("django.request", "ERROR"):
            response = client.get("/test-crash/")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Something went wrong", response.content)
        self.assertNotIn(b"Deliberate crash", response.content)  # no technical details for staff
