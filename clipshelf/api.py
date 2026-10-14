"""Shared JSON API envelope.

Error responses, the request-body size cap, and the one-line ValidationError
text used by JSON error payloads. Authentication and the /api guard
(api_auth, api_csrf, api_guard) stay in clipshelf.accounts.
"""

import json

from django.http import JsonResponse


def json_error(status, detail=None, errors=None):
    payload = {}
    if detail:
        payload["detail"] = detail
    if errors:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


class ApiError(Exception):
    """Carries a ready-to-send JSON error response through admin views."""

    def __init__(self, status, detail=None, errors=None):
        super().__init__(detail)
        self.response = json_error(status, detail=detail, errors=errors)


MAX_BODY_BYTES = 64 * 1024


def json_body(request) -> dict:
    """Parse the request body as a JSON object; oversize bodies answer 413."""
    body = request.body or b""
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, detail="request body too large")
    try:
        data = json.loads(body) if body else {}
        # "\ud800" escapes survive loads but no later layer can store or send
        # them; the round-trip encode raises UnicodeEncodeError (a ValueError),
        # so lone surrogates answer 400 here like any other malformed body.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise ApiError(400, detail="malformed JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, detail="JSON object expected")
    return data


def vmessage(exc) -> str:
    """One-line text form of a ValidationError for JSON error payloads."""
    msg = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
    if isinstance(msg, dict):
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in msg.items())
    return "; ".join(str(m) for m in msg)
