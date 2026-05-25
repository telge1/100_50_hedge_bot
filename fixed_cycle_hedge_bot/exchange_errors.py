from __future__ import annotations

import http.client
import socket
from typing import Any

import requests
import urllib3


class ExchangeUnavailableError(Exception):
    def __init__(
        self,
        *,
        endpoint: str,
        original_exception: Exception,
    ) -> None:
        super().__init__(str(original_exception))
        self.endpoint = endpoint
        self.original_exception = original_exception
        self.error_class = type(original_exception).__name__
        self.error_message = str(original_exception)


def is_retryable_exchange_error(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, urllib3.exceptions.ProtocolError):
        return True
    if isinstance(exc, urllib3.exceptions.MaxRetryError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, urllib3.exceptions.NameResolutionError):
            return True
    if isinstance(exc, urllib3.exceptions.NameResolutionError):
        return True
    if isinstance(exc, http.client.RemoteDisconnected):
        return True
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def compact_exchange_error(exc: Exception) -> dict[str, Any]:
    return {
        "error_class": type(exc).__name__,
        "error_message": str(exc),
        "retryable": is_retryable_exchange_error(exc),
    }
