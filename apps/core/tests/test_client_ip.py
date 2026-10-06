"""get_client_ip(): the IP address written to the audit log and used by the sign-in lockout."""

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core.audit import get_client_ip


class ClientIpTests(SimpleTestCase):
    def request(self, forwarded=None):
        extra = {"REMOTE_ADDR": "10.0.0.5"}
        if forwarded is not None:
            extra["HTTP_X_FORWARDED_FOR"] = forwarded
        return RequestFactory().get("/", **extra)

    @override_settings(USE_X_FORWARDED_FOR=False)
    def test_header_is_ignored_unless_switched_on(self):
        self.assertEqual(get_client_ip(self.request("203.0.113.9")), "10.0.0.5")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_COUNT=1)
    def test_behind_one_proxy_the_address_it_added_is_used(self):
        self.assertEqual(get_client_ip(self.request("203.0.113.9")), "203.0.113.9")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_COUNT=1)
    def test_addresses_made_up_by_the_visitor_are_ignored(self):
        # The visitor sent "1.2.3.4" themselves; the proxy appended the real address.
        self.assertEqual(get_client_ip(self.request("1.2.3.4, 203.0.113.9")), "203.0.113.9")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_COUNT=2)
    def test_two_proxies(self):
        self.assertEqual(get_client_ip(self.request("1.2.3.4, 203.0.113.9, 10.1.1.1")), "203.0.113.9")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_COUNT=2)
    def test_falls_back_to_the_connection_address(self):
        self.assertEqual(get_client_ip(self.request()), "10.0.0.5")
        self.assertEqual(get_client_ip(self.request("203.0.113.9")), "10.0.0.5")

    def test_no_request(self):
        self.assertIsNone(get_client_ip(None))
