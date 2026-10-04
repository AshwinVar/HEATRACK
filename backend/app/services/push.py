"""Push transports. Injected so tests and local development never need Firebase secrets.

`accepted` means Firebase accepted the message for delivery. It is NOT proof that the phone
received it or that a human saw it; acknowledgement happens only in the app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.config import Settings

log = logging.getLogger(__name__)

Status = Literal["accepted", "invalid_token", "retry"]


@dataclass
class SendResult:
    status: Status
    message_id: str | None = None
    error: str | None = None


@dataclass
class PushMessage:
    token: str
    platform: str
    title: str
    body: str
    data: dict[str, str]


class PushTransport(Protocol):
    name: str

    def send(self, msg: PushMessage) -> SendResult: ...


@dataclass
class FakeTransport:
    """Development/test transport. Records messages; never contacts a real device.
    Visibly labelled in /v1/diagnostics and in the app."""

    name: str = "fake"
    sent: list[PushMessage] = field(default_factory=list)
    script: list[Status] = field(default_factory=list)  # queued outcomes for tests

    def send(self, msg: PushMessage) -> SendResult:
        outcome: Status = self.script.pop(0) if self.script else "accepted"
        if outcome == "accepted":
            self.sent.append(msg)
            return SendResult("accepted", message_id=f"fake-{len(self.sent)}")
        return SendResult(outcome, error=f"fake_{outcome}")


class FcmTransport:
    name = "fcm"

    def __init__(self, settings: Settings) -> None:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            if settings.firebase_credentials_json:
                import json

                cred = credentials.Certificate(json.loads(settings.firebase_credentials_json))
            elif settings.firebase_credentials_file:
                cred = credentials.Certificate(settings.firebase_credentials_file)
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)

    def send(self, msg: PushMessage) -> SendResult:
        from firebase_admin import exceptions, messaging

        message = messaging.Message(
            token=msg.token,
            notification=messaging.Notification(title=msg.title, body=msg.body),
            data=msg.data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="familypulse_alerts"),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10", "apns-push-type": "alert"},
                payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
            ),
        )
        try:
            return SendResult("accepted", message_id=messaging.send(message))
        except (messaging.UnregisteredError, messaging.SenderIdMismatchError) as exc:
            return SendResult("invalid_token", error=type(exc).__name__)
        except exceptions.InvalidArgumentError:
            # The payload is fixed and generic, so an invalid argument is the token.
            return SendResult("invalid_token", error="InvalidArgumentError")
        except exceptions.FirebaseError as exc:
            return SendResult("retry", error=f"{type(exc).__name__}:{exc.code}")
        except Exception as exc:  # noqa: BLE001 - network errors etc. are retried
            log.warning("fcm send failed: %s", type(exc).__name__)
            return SendResult("retry", error=type(exc).__name__)


_transport: PushTransport | None = None


def get_transport(settings: Settings) -> PushTransport | None:
    global _transport
    if _transport is None:
        if settings.push_transport == "fake":
            _transport = FakeTransport()
        elif settings.push_transport == "fcm":
            _transport = FcmTransport(settings)
    return _transport


def set_transport(t: PushTransport | None) -> None:
    global _transport
    _transport = t
