import pytest


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Never let a test push a real Home Assistant alert.

    login_notify defaults to True, so any test that logs in through the
    TestClient (or trips the login rate limit) would otherwise send a live
    "new login" / critical "login blocked" push from host "testclient".
    """
    import web_server
    sent = []
    monkeypatch.setattr(web_server, "_notify", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(web_server, "_geo", lambda ip: "test")
    return sent
