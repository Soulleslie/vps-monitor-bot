import pytest

from bot.fetcher import UnsafeURLError, assert_public_host, normalize_url


def test_normalize_url():
    assert normalize_url(" novixlink.com/store/nttispipvps#x ") == "https://novixlink.com/store/nttispipvps"
    assert normalize_url("https://a.com/store/x?language=chinese") == "https://a.com/store/x?language=chinese"
    with pytest.raises(UnsafeURLError):
        normalize_url("ftp://a.com/x")
    with pytest.raises(UnsafeURLError):
        normalize_url("https://user:pw@a.com/x")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.5/", "http://[::1]/", "http://169.254.169.254/latest/meta-data",
    "http://192.168.1.1/", "http://localhost/",
])
async def test_private_hosts_rejected(url):
    with pytest.raises(UnsafeURLError):
        await assert_public_host(url)


async def test_public_ip_ok():
    await assert_public_host("http://1.1.1.1/")
