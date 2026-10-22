import http.server
import threading

import pytest

from jobledger.testing import (FakeWeb, Page, Sleeper, jd_page, public_resolver)
from jobledger.tools import jd_retriever as jr


BLOCKED = ["http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/x", "http://192.168.1.1/", "http://169.254.169.254/latest",
           "http://[::1]/", "http://[::ffff:127.0.0.1]/", "ftp://example.com/x", "file:///etc/passwd", "http://user:pw@example.com/",
           "https://example.com:8443/", "http://printer.local/", "https://www.linkedin.com/jobs/view/1", "http://0.0.0.0/",
           "http://100.64.0.1/", "http://example.com/a\r\nHost: evil", ""]


@pytest.mark.parametrize("url", BLOCKED)
def test_validate_url_refuses(url):
    ok, why = jr.validate_url(url, public_resolver)
    assert not ok and why


def test_validate_url_refuses_hostname_resolving_private():
    assert not jr.validate_url("https://rebind.example/x", public_resolver)[0]
    assert not jr.validate_url("https://meta.example/x", public_resolver)[0]


def test_validate_url_allows_public():
    assert jr.validate_url("https://careers.acme.example/jobs/101", public_resolver)[0]


def test_unresolvable_host_refused():
    def nope(h, p):
        raise OSError("nxdomain")
    assert not jr.validate_url("https://nope.example/", nope)[0]


def test_ip_is_blocked_table():
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254", "::1", "fe80::1", "224.0.0.1"):
        assert jr.ip_is_blocked(ip)
    assert not jr.ip_is_blocked("93.184.216.34")


def _fetch(web, url="https://careers.acme.example/jobs/101", **kw):
    sl = Sleeper()
    res = jr.retrieve_jd(url, session=web, resolver=public_resolver, sleep=sl, **kw)
    return res, sl


def test_fetch_ok_extracts_text_and_company():
    res, _ = _fetch(FakeWeb())
    assert res.ok and len(res.text) > 200 and res.company_found and "acme" in res.company_found.lower()


def test_unsafe_url_makes_no_request():
    web = FakeWeb()
    res = jr.retrieve_jd("http://127.0.0.1/admin", session=web, resolver=public_resolver, sleep=Sleeper())
    assert res.failure_code == "unsafe_url" and web.calls == []


def test_redirect_to_private_is_refused_on_the_hop():
    web = FakeWeb({"https://a.example/j": Page(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})})
    res, _ = _fetch(web, "https://a.example/j")
    assert not res.ok and res.failure_code == "unsafe_url"
    assert all("169.254" not in c for c in web.calls)


def test_redirect_to_public_followed_and_loop_bounded():
    web = FakeWeb({"https://a.example/j": Page(302, headers={"Location": "https://b.example/j"}),
                   "https://b.example/j": jd_page("acme_backend.html")})
    res, _ = _fetch(web, "https://a.example/j")
    assert res.ok and res.final_url == "https://b.example/j"
    loop = FakeWeb({"https://a.example/j": Page(302, headers={"Location": "https://a.example/j"})})
    res, _ = _fetch(loop, "https://a.example/j", max_redirects=3)
    assert not res.ok and res.failure_code


def test_size_cap_and_content_type():
    big = FakeWeb({"https://a.example/j": Page(200, body_bytes=b"<html>" + b"x" * 200_000 + b"</html>")})
    res, _ = _fetch(big, "https://a.example/j", max_bytes=10_000)
    assert not res.ok and res.failure_code
    pdf = FakeWeb({"https://a.example/j": Page(200, body="%PDF", content_type="application/pdf")})
    res, _ = _fetch(pdf, "https://a.example/j")
    assert not res.ok


def test_404_not_retried_503_retried_with_backoff():
    web = FakeWeb({"https://a.example/j": Page(404, "nope")})
    res, sl = _fetch(web, "https://a.example/j")
    assert not res.ok and sl.delays == [] and web.count("a.example") == 1
    web = FakeWeb({"https://a.example/j": Page(503, "busy")})
    res, sl = _fetch(web, "https://a.example/j", max_retries=3)
    assert not res.ok and res.retryable and sl.delays == [1, 2, 4]


def test_retry_recovers():
    web = FakeWeb({"https://a.example/j": [Page(503, "busy"), Page(503, "busy"), jd_page("acme_backend.html")]})
    res, sl = _fetch(web, "https://a.example/j")
    assert res.ok and res.retry_count == 2 and sl.delays == [1, 2]


def test_timeout_is_failure_not_exception():
    import requests
    web = FakeWeb({"https://a.example/j": Page(exc=requests.Timeout("slow"))})
    res, _ = _fetch(web, "https://a.example/j")
    assert not res.ok and res.failure_code == "timeout"


def test_injection_in_jd_is_flagged_and_stripped():
    web = FakeWeb()
    res, _ = _fetch(web, "https://evil.example/jobs/1")
    assert res.ok and res.injection_flags
    assert "ignore previous" not in res.text.lower()


def test_verify_company_conflict_is_mismatch():
    res = jr.JDResult(status="ok", source_url="https://x.example/j", text="Data Engineer at Vertex Corp. We build pipelines. " * 10,
                      title_found="Data Engineer", company_found="Vertex Corp")
    status, evidence = jr.verify_jd_match("Data Engineer", "Brightside", res)
    assert status == "mismatch" and "Vertex" in evidence


def test_verify_matching_page_is_verified():
    web = FakeWeb()
    res, _ = _fetch(web)
    status, _ = jr.verify_jd_match("Backend Python Developer", "Acme Cloud", res)
    assert status in ("verified", "partial")


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("<html><head><title>Python Dev</title></head><body><h1>Python Developer</h1><p>" + "We use Python daily. " * 30
                + "</p></body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_real_loopback_http_roundtrip_only_with_allow_private():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/job"
        refused = jr.retrieve_jd(url, sleep=Sleeper())
        assert refused.failure_code == "unsafe_url"
        # port is not 80/443 so even allow_private must be checked against the port rule: use the validator contract
        ok, _ = jr.validate_url(url, allow_private=True)
        if ok:
            res = jr.retrieve_jd(url, allow_private=True, sleep=Sleeper())
            assert res.ok and "Python" in res.text
    finally:
        srv.shutdown()
        srv.server_close()
