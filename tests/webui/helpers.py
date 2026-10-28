"""Browser setup for exercising administration handlers behind their guards."""

from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient


def authorize(client: TestClient) -> None:
    client.session.headers.update(
        {
            "X-Qobuz-CSRF": client.app["csrf_token"],
            "Content-Type": "application/json",
        }
    )


async def oauth_callback(client: TestClient, ingress: str = "") -> str:
    origin = str(client.make_url("/")).rstrip("/") + ingress
    headers = {"X-Ingress-Path": ingress} if ingress else {}
    response = await client.get(
        "/auth/login", params={"origin": origin}, headers=headers, allow_redirects=False
    )
    assert response.status == 302
    redirect = parse_qs(urlsplit(response.headers["Location"]).query)["redirect_url"][0]
    return urlsplit(redirect).path.removeprefix(ingress) if ingress else urlsplit(redirect).path
