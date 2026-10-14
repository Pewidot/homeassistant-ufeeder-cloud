"""UCSP MQTT transactions, reconstructed from U Feeder Android 1.1.4.

The broker's PUBACK is not a device acknowledgement. Commands complete only
after a correlated Interaction response. No command is replayed after timeout
or reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiomqtt

from .api import ALLOWED_PROPERTIES, CannotConnect, UFeederApi, UFeederError
from .const import ALLOWED_DOMAINS, PRODUCT_KEY

_LOGGER = logging.getLogger(__name__)
MAX_MESSAGE = 1024 * 1024
MODEL_VERSION = 25
COMMAND_TIMEOUT = 10.0
Path = tuple[int, int, int]


class ProtocolError(UFeederError):
    """Malformed or unsupported protobuf, without the raw message."""


class CommandError(UFeederError):
    """The device rejected a command."""

    def __init__(self, status: int, cluster_status: int = 0) -> None:
        self.status = status
        self.cluster_status = cluster_status
        super().__init__(
            f"Device rejected command (status={status}, cluster={cluster_status})"
        )


class CommandTimeout(CannotConnect):
    """The result is unknown; automatically repeating it could feed twice."""


def encode_varint(value: int) -> bytes:
    if type(value) is not int or not -(1 << 63) <= value < (1 << 64):
        raise ValueError("Integer outside protobuf range")
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def varint_field(number: int, value: int | bool) -> bytes:
    if not 0 < number < (1 << 29):
        raise ValueError("Invalid protobuf field")
    return encode_varint(number << 3) + encode_varint(int(value))


def bytes_field(number: int, value: bytes | str) -> bytes:
    if not 0 < number < (1 << 29):
        raise ValueError("Invalid protobuf field")
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) > MAX_MESSAGE:
        raise ValueError("Invalid protobuf bytes")
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def parse_fields(raw: bytes) -> list[tuple[int, int, int | bytes]]:
    """Bounded protobuf wire reader; preserves repeated fields and unknown tags."""
    if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE:
        raise ProtocolError("Invalid protobuf message size")
    offset = 0
    result: list[tuple[int, int, int | bytes]] = []

    def varint() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(raw):
                raise ProtocolError("Truncated protobuf integer")
            digit = raw[offset]
            offset += 1
            if shift == 63 and digit > 1:
                raise ProtocolError("Protobuf integer overflow")
            value |= (digit & 127) << shift
            if digit < 128:
                return value
        raise ProtocolError("Invalid protobuf integer")

    while offset < len(raw):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ProtocolError("Invalid protobuf field number")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(raw) - offset:
                raise ProtocolError("Truncated protobuf data")
            value = raw[offset : offset + size]
            offset += size
        else:
            raise ProtocolError("Unsupported protobuf wire type")
        result.append((number, wire, value))
        if len(result) > 4096:
            raise ProtocolError("Too many protobuf fields")
    return result


def _fields(raw: bytes, types: dict[int, int]) -> dict[int, int | bytes]:
    result = {}
    for number, wire, value in parse_fields(raw):
        if number not in types:
            continue
        if types[number] != wire or number in result:
            raise ProtocolError("Invalid singular protobuf field")
        result[number] = value
    return result


def decode_scalar(
    raw: bytes, *, signed: bool = False, boolean: bool = False
) -> int | bool:
    value = int(_fields(raw, {1: 0}).get(1, 0))
    if boolean:
        if value not in (0, 1):
            raise ProtocolError("Invalid boolean wrapper")
        return bool(value)
    if signed:
        value &= (1 << 32) - 1
        if value & (1 << 31):
            value -= 1 << 32
    return value


def encode_path(path: Path) -> bytes:
    if len(path) != 3 or any(
        type(n) is not int or not 0 <= n < (1 << 32) for n in path
    ):
        raise ValueError("Invalid interaction path")
    return b"".join(varint_field(i, n) for i, n in enumerate(path, 1))


def decode_path(raw: bytes) -> Path:
    fields = _fields(raw, {1: 0, 2: 0, 3: 0})
    return tuple(int(fields.get(i, 0)) for i in (1, 2, 3))


def encode_invoke(path: Path, argument: bytes = b"") -> bytes:
    # arg is repeated bytes: the app always adds one item, including no-arg calls.
    return bytes_field(1, encode_path(path)) + bytes_field(2, argument)


def encode_write(path: Path, value: bytes) -> bytes:
    attribute = bytes_field(1, encode_path(path)) + bytes_field(2, value)
    return bytes_field(1, attribute)


@dataclass(frozen=True)
class Interaction:
    model_version: int
    message_id: int
    exchange_id: int
    source: str
    destination: str
    timestamp: int
    opcode: int
    payload: bytes

    def encode(self) -> bytes:
        return b"".join(
            (
                varint_field(1, self.model_version),
                varint_field(2, self.message_id),
                varint_field(3, self.exchange_id),
                bytes_field(4, self.source),
                bytes_field(5, self.destination),
                varint_field(6, self.timestamp),
                varint_field(7, self.opcode),
                bytes_field(8, self.payload),
            )
        )

    @classmethod
    def decode(cls, raw: bytes) -> Interaction:
        data = _fields(raw, {1: 0, 2: 0, 3: 0, 4: 2, 5: 2, 6: 0, 7: 0, 8: 2})
        try:
            return cls(
                int(data.get(1, 0)),
                int(data.get(2, 0)),
                int(data.get(3, 0)),
                data.get(4, b"").decode("utf-8"),
                data.get(5, b"").decode("utf-8"),
                int(data.get(6, 0)),
                int(data.get(7, 0)),
                data.get(8, b""),
            )
        except (UnicodeError, AttributeError, TypeError, ValueError) as err:
            raise ProtocolError("Invalid Interaction envelope") from err


@dataclass(frozen=True)
class ReportEvent:
    path: str
    timestamp: int
    data: bytes


@dataclass(frozen=True)
class ReportData:
    attributes: dict[str, bytes]
    events: tuple[ReportEvent, ...]
    timestamp: int


def decode_report(message: Interaction) -> ReportData:
    attributes: dict[str, bytes] = {}
    events: list[ReportEvent] = []
    for number, wire, raw in parse_fields(message.payload):
        if number not in (1, 2):
            continue
        if wire != 2:
            raise ProtocolError("Invalid ReportData entry")
        entry = _fields(raw, {1: 2, 2: 2} if number == 1 else {1: 2, 2: 0, 3: 2})
        path = decode_path(entry.get(1, b""))
        key = ":".join(str(n) for n in path)
        if number == 1 and key in ALLOWED_PROPERTIES:
            attributes[key] = entry.get(2, b"")
        elif number == 2 and path[:2] in ((10, 10), (10, 11)):
            events.append(ReportEvent(key, int(entry.get(2, 0)), entry.get(3, b"")))
    return ReportData(attributes, tuple(events), message.timestamp)


def check_response(message: Interaction, opcode: int, path: Path) -> None:
    """Raise unless the expected device transaction explicitly succeeds."""
    if message.opcode == 0:
        status = int(_fields(message.payload, {1: 0}).get(1, 0))
        # The app treats generic StatusResponse as an exception, even status 0.
        raise CommandError(status)
    if message.opcode != opcode:
        raise ProtocolError("Unexpected interaction response")
    if opcode == 3:
        responses = [
            raw
            for field, wire, raw in parse_fields(message.payload)
            if field == 1 and wire == 2
        ]
        if not responses:
            raise ProtocolError("Write response has no status")
    else:
        responses = [message.payload]
    for raw in responses:
        fields = _fields(raw, {1: 2, 2: 0, 3: 0})
        # Some firmware omits the path; when supplied it must be our path.
        if 1 in fields and decode_path(fields[1]) != path:
            raise ProtocolError("Response path does not match request")
        status, cluster = int(fields.get(2, 0)), int(fields.get(3, 0))
        if status:
            raise CommandError(status, cluster)


@dataclass
class _Pending:
    device_id: str
    opcode: int
    path: Path
    future: asyncio.Future[None]


class UFeederTransport:
    """Dedicated MQTT connection for one account; isolated from HA's MQTT broker."""

    def __init__(
        self,
        api: UFeederApi,
        on_report: Callable[[str, ReportData], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        *,
        command_timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        self.api = api
        self._on_report = on_report
        self._on_connection_change = on_connection_change
        self._timeout = command_timeout
        self._devices: set[str] = set()
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._subscriptions: set[str] = set()
        self._subscription_lock = asyncio.Lock()
        self._pending: dict[int, _Pending] = {}
        self._retired: dict[int, float] = {}
        # The app starts this counter at zero. Although its protobuf field is
        # uint64, live firmware silently drops Unix-millisecond-sized IDs.
        self._message_id = 0
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    @staticmethod
    def _segment(value: str) -> str:
        if not value or any(ch in value for ch in "/+#\x00"):
            raise ProtocolError("Invalid MQTT topic identifier")
        return value

    def _topics(self, device_id: str) -> tuple[str, str, str]:
        prefix = f"/{PRODUCT_KEY}/{self._segment(device_id)}"
        source = self._segment(self.api.user_id)
        return (
            f"{prefix}/thing/property/post",
            f"{prefix}/thing/event/post",
            f"{prefix}/{source}/upload/reply",
        )

    async def async_start(self, device_ids: Iterable[str]) -> None:
        self._devices = {self._segment(device) for device in device_ids}
        if self._task is None:
            self._closed = False
            self._task = asyncio.create_task(self._run(), name="ufeeder_mqtt")

    async def async_set_devices(self, device_ids: Iterable[str]) -> None:
        self._devices = {self._segment(device) for device in device_ids}
        if self.connected:
            await self._subscribe_devices()

    async def async_stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._ready.clear()
        self._fail_pending()

    def _fail_pending(self) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(
                    CannotConnect("MQTT disconnected; command result is unknown")
                )

    async def _subscribe_devices(self) -> None:
        async with self._subscription_lock:
            client = self._client
            if client is None:
                return
            desired = {
                topic for device in self._devices for topic in self._topics(device)
            }
            for topic in sorted(desired - self._subscriptions):
                granted = await client.subscribe(topic, qos=1)
                if not granted or any(
                    getattr(code, "value", code) not in (0, 1, 2) for code in granted
                ):
                    raise CannotConnect("MQTT subscription was rejected")
                self._subscriptions.add(topic)
            for topic in sorted(self._subscriptions - desired):
                await client.unsubscribe(topic)
                self._subscriptions.discard(topic)

    async def _run(self) -> None:
        delay = 5
        while not self._closed:
            try:
                metadata = await self.api.async_request(
                    "GET", "device-transport/mqtt/v1/login"
                )
                uri = urlsplit(metadata["mqttUrl"])
                allowed_hosts = {
                    urlsplit(domain).hostname for domain in ALLOWED_DOMAINS
                }
                if (
                    uri.scheme != "mqtts"
                    or uri.hostname not in allowed_hosts
                    or uri.username
                    or uri.password
                ):
                    raise ProtocolError("Unrecognized UCSP MQTT server")
                # Loading the system trust store does file IO; keep it off HA's loop.
                context = await asyncio.to_thread(ssl.create_default_context)
                async with aiomqtt.Client(
                    hostname=uri.hostname,
                    port=uri.port or 8883,
                    username=metadata["username"],
                    password=metadata["password"],
                    identifier=f"ha-{self.api.client_id[:20]}",
                    tls_context=context,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    clean_session=True,
                    keepalive=60,
                    timeout=10,
                    max_queued_incoming_messages=256,
                    max_queued_outgoing_messages=16,
                    max_inflight_messages=8,
                ) as client:
                    self._client = client
                    self._subscriptions.clear()
                    await self._subscribe_devices()
                    self._ready.set()
                    delay = 5
                    if self._on_connection_change:
                        self._on_connection_change(True)
                    async for message in client.messages:
                        if not message.retain:
                            self._handle_message(
                                str(message.topic), bytes(message.payload)
                            )
            except asyncio.CancelledError:
                raise
            except (
                aiomqtt.MqttError,
                UFeederError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
            ):
                # Error strings from MQTT/aiohttp can contain credentials or URLs.
                _LOGGER.debug("U Feeder MQTT unavailable; reconnecting with backoff")
            finally:
                self._client = None
                was_connected = self._ready.is_set()
                self._ready.clear()
                self._fail_pending()
                if was_connected and self._on_connection_change:
                    self._on_connection_change(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    def _handle_message(self, topic: str, raw: bytes) -> None:
        try:
            message = Interaction.decode(raw)
            if message.source not in self._devices:
                return
            expected_topics = self._topics(message.source)
            if message.opcode == 1 and topic in expected_topics[:2]:
                report = decode_report(message)
                if self._on_report and (report.attributes or report.events):
                    self._on_report(message.source, report)
                return
            pending = self._pending.get(message.exchange_id)
            if (
                pending is None
                or pending.future.done()
                or message.source != pending.device_id
                or message.destination != self.api.user_id
                or topic != expected_topics[2]
            ):
                return
            try:
                check_response(message, pending.opcode, pending.path)
            except UFeederError as err:
                pending.future.set_exception(err)
            else:
                pending.future.set_result(None)
        except (ProtocolError, ValueError, TypeError):
            _LOGGER.debug("Ignored malformed U Feeder MQTT message")

    async def async_invoke(self, device_id: str, path: Path, arg: bytes = b"") -> None:
        await self._command(
            device_id, path, 4, encode_invoke(path, arg), "thing/service"
        )

    async def async_write(self, device_id: str, path: Path, value: bytes) -> None:
        await self._command(
            device_id, path, 2, encode_write(path, value), "thing/property/set"
        )

    async def _command(
        self, device_id: str, path: Path, opcode: int, payload: bytes, suffix: str
    ) -> None:
        if device_id not in self._devices:
            raise ProtocolError("Device is not registered with this account")
        if self._closed or self._task is None:
            raise CannotConnect("MQTT transport is stopped")
        try:
            async with asyncio.timeout(self._timeout):
                await self._ready.wait()
        except TimeoutError as err:
            raise CannotConnect("MQTT is not connected; command was not sent") from err
        # Reserve the response waiter before publishing. A fast device may reply
        # before the broker's PUBACK completes aiomqtt.publish.
        client = self._client
        if client is None:
            raise CannotConnect("MQTT is not connected; command was not sent")
        now = time.monotonic()
        self._retired = {
            key: expiry for key, expiry in self._retired.items() if expiry > now
        }
        exchange = secrets.randbelow(65536)
        while exchange in self._pending or exchange in self._retired:
            exchange = (exchange + 1) % 65536
        future = asyncio.get_running_loop().create_future()
        self._pending[exchange] = _Pending(device_id, opcode + 1, path, future)
        self._message_id += 1
        request = Interaction(
            MODEL_VERSION,
            self._message_id,
            exchange,
            self.api.user_id,
            device_id,
            int(time.time() * 1000),
            opcode,
            payload,
        )
        topic = f"/{PRODUCT_KEY}/{self._segment(device_id)}/{self._segment(self.api.user_id)}/{suffix}"
        try:
            async with asyncio.timeout(self._timeout):
                await client.publish(topic, request.encode(), qos=1, retain=False)
                await future
        except TimeoutError as err:
            raise CommandTimeout(
                "Device did not acknowledge; result is unknown, command was not repeated"
            ) from err
        except aiomqtt.MqttError as err:
            raise CannotConnect("MQTT command interrupted; result is unknown") from err
        finally:
            self._pending.pop(exchange, None)
            self._retired[exchange] = time.monotonic() + 60
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume disconnect failure if publish itself failed.
