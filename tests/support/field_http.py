"""Small ASGI POST client without optional HTTP dependencies."""

import json


async def post(
    app, value=None, *, raw=None, path="/api/field/single-microgrid/initialize"
):
    body = raw if raw is not None else json.dumps(value).encode()
    sent = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "http_version": "1.1",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    status = next(x["status"] for x in sent if x["type"] == "http.response.start")
    content = json.loads(
        b"".join(x.get("body", b"") for x in sent if x["type"] == "http.response.body")
    )
    return status, content
