"""The pool CA is private, disposable, and unique to a pool lifecycle."""

import stat

from dravenpdf.render._proxy_ca import ProxyCA


def test_private_unique_ca_and_cleanup() -> None:
    first = ProxyCA.create()
    second = ProxyCA.create()
    try:
        assert first.spki_hash != second.spki_hash
        assert stat.S_IMODE(first.confdir.stat().st_mode) == 0o700
        assert (first.confdir / "mitmproxy-ca.pem").exists()
        for path in first.confdir.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        first.close()
        second.close()
    assert not first.confdir.exists()
    first.close()
