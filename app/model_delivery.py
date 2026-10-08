"""Public delivery metadata. No provider routes, credentials or response bodies."""

ERROR_STAGES = {
    "UPSTREAM_SCHEMA_INVALID": "request_schema",
    "UPSTREAM_SCHEMA_UNSUPPORTED": "request_capability",
    "UPSTREAM_HTTP_ERROR": "upstream_http",
    "UPSTREAM_CONNECT_FAILED": "upstream_connect",
    "UPSTREAM_CONNECT_TIMEOUT": "upstream_connect",
    "UPSTREAM_WRITE_FAILED": "upstream_write",
    "UPSTREAM_TIMEOUT": "upstream_timeout",
    "UPSTREAM_READ_FAILED": "response_read",
    "UPSTREAM_RESPONSE_INVALID": "response_envelope",
    "GATEWAY_BUSY": "gateway_admission",
}
NOT_DISPATCHED = {"GATEWAY_BUSY", "UPSTREAM_CONNECT_FAILED", "UPSTREAM_CONNECT_TIMEOUT"}
PROVIDER_CODES = {
    "invalid_json_schema", "invalid_schema", "invalid_request_error", "unsupported_value",
    "unsupported_parameter", "invalid_value", "invalid_api_key", "authentication_error",
    "rate_limit_exceeded", "insufficient_quota", "model_not_found", "context_length_exceeded",
    "server_error", "internal_server_error", "service_unavailable", "permission_denied",
}


def safe_provider_code(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in PROVIDER_CODES else None


def delivery_metadata(call, request_key, *, gateway_status=None, certainty=None, provider_status=None):
    code = call.error_code
    if certainty is None:
        certainty = ("not_dispatched" if code in NOT_DISPATCHED else
                     "response_received" if call.http_status is not None else "may_have_dispatched")
    if provider_status is None and certainty != "not_dispatched":
        provider_status = call.http_status
    if gateway_status is None:
        if call.status == "succeeded":
            gateway_status = provider_status
        elif call.status == "failed" and code in ERROR_STAGES:
            gateway_status = provider_status if provider_status in {400, 422, 429} else 502
        # Running, cancelled and legacy unknown outcomes have no persisted
        # proof of an HTTP response to Core. Do not invent a gateway status.
    return {
        "origin": "upstream" if certainty == "response_received" else "gateway",
        "stage": ERROR_STAGES.get(code, "response_status" if call.status == "succeeded" else "delivery_status"),
        "dispatchCertainty": certainty,
        "callId": call.id,
        **({"requestKey": request_key} if request_key else {}),
        **({"gatewayHttpStatus": gateway_status} if gateway_status is not None else {}),
        **({"providerHttpStatus": provider_status} if provider_status is not None else {}),
    }


def request_status(reservation, call):
    # released is persisted proof that this call did not reach the provider.
    certainty = "not_dispatched" if reservation.state == "released" else None
    metadata = delivery_metadata(call, reservation.idempotency_key, certainty=certainty,
                                 gateway_status=call.http_status if certainty == "not_dispatched" else None)
    return {
        "call_id": call.id, "status": call.status, "error_code": call.error_code,
        "credit_state": reservation.state, "reserved": reservation.amount, "actual": reservation.actual,
        **metadata, "responseAvailable": False,
    }
