from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodySizeLimit:
    """Count real ASGI body bytes, including requests without Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        multipart = headers.get(b'content-type', b'').startswith(b'multipart/form-data')
        limit = 101 * 1024 * 1024 if multipart else 1024 * 1024
        declared = headers.get(b'content-length')
        if declared is not None:
            try:
                valid = 0 <= int(declared) <= limit
            except ValueError:
                valid = False
            if not valid:
                return await JSONResponse({'detail': 'Превышен или некорректен размер запроса'},
                                          status_code=413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > limit:
                    raise HTTPException(413, 'Превышен размер запроса')
            return message

        await self.app(scope, limited_receive, send)
