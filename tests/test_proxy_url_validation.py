import unittest

from fastapi import HTTPException

from src.web.app import _validate_proxy_url


class TestProxyUrlValidation(unittest.TestCase):
    def test_accepts_http_https(self):
        # No exception means accepted.
        _validate_proxy_url("http://proxy.example.com:8080")
        _validate_proxy_url("https://proxy.example.com:8080")

    def test_accepts_socks(self):
        _validate_proxy_url("socks4://proxy.example.com:1080")
        _validate_proxy_url("socks5://proxy.example.com:1080")

    def test_accepts_user_password_proxy(self):
        # Embedded credentials are allowed (the auth layer will redact on display).
        _validate_proxy_url("http://user:pass@proxy.example.com:8080")

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_proxy_url("javascript:alert(1)")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_file_scheme(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_proxy_url("file:///etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_data_scheme(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_proxy_url("data:text/plain,abc")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_missing_host(self):
        with self.assertRaises(HTTPException):
            _validate_proxy_url("http://")

    def test_rejects_unspecified_ip(self):
        with self.assertRaises(HTTPException):
            _validate_proxy_url("http://0.0.0.0:8080")

    def test_rejects_link_local(self):
        with self.assertRaises(HTTPException):
            _validate_proxy_url("http://169.254.169.254:80")

    def test_allows_loopback_proxy(self):
        # Users do legitimately point at on-host proxies.
        _validate_proxy_url("http://127.0.0.1:8080")

    def test_allows_rfc1918_proxy(self):
        # In-network proxies are common.
        _validate_proxy_url("http://10.0.0.5:8080")
        _validate_proxy_url("http://192.168.1.1:3128")


if __name__ == "__main__":
    unittest.main()
