"""Offline protocol vectors and races; these tests never command a device."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ufeeder.transport import (
    CannotConnect,
    CommandError,
    CommandTimeout,
    Interaction,
    ProtocolError,
    UFeederTransport,
    bytes_field,
    check_response,
    decode_report,
    decode_scalar,
    encode_invoke,
    encode_path,
    encode_varint,
    encode_write,
    parse_fields,
    varint_field,
)


def envelope(opcode=5, payload=b"", exchange=42, source="device", destination="123"):
    return Interaction(
        25, 99, exchange, source, destination, 1770000000000, opcode, payload
    )


def test_independent_wire_vectors():
    # Literal protobuf bytes derived from the recovered BuilderInfo field tags.
    assert encode_path((10, 11, 4)).hex() == "080a100b1804"
    assert encode_invoke((10, 11, 4)).hex() == "0a06080a100b18041200"
    assert (
        encode_write((10, 11, 8), b"\x08\x01").hex() == "0a0c0a06080a100b180812020801"
    )
    assert encode_varint(-1).hex() == "ffffffffffffffffff01"
    assert (
        decode_scalar(b"\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01", signed=True)
        == -1
    )
    assert decode_scalar(b"", boolean=True) is False
    assert decode_scalar(b"\x08\x01", boolean=True) is True
    message = Interaction(25, 1, 2, "3", "4", 5, 4, b"\x12\x00")
    assert message.encode().hex() == "0819100118022201332a01343005380442021200"
    assert (
        Interaction.decode(bytes.fromhex("0819100118022201332a01343005380442021200"))
        == message
    )


@pytest.mark.parametrize(
    "raw", [b"\x00", b"\x80", b"\x0a\x04a", b"\x08" + b"\xff" * 10, b"\x0b"]
)
def test_rejects_malformed_protobuf(raw):
    with pytest.raises(ProtocolError):
        parse_fields(raw)


def test_duplicate_envelope_fields_not_silently_overwritten():
    with pytest.raises(ProtocolError):
        Interaction.decode(b"\x38\x05\x38\x00")


def test_reports_exclude_wifi_and_factory_secrets():
    def attribute(path, value):
        return bytes_field(1, bytes_field(1, encode_path(path)) + bytes_field(2, value))

    payload = (
        attribute((10, 11, 9), b"\x08\x01")
        + attribute((10, 10, 9), b"private wifi")
        + attribute((10, 13, 2), b"private factory secret")
        + bytes_field(
            2,
            bytes_field(1, encode_path((10, 11, 1)))
            + varint_field(2, 123)
            + bytes_field(3, b"event"),
        )
    )
    report = decode_report(envelope(opcode=1, payload=payload))
    assert report.attributes == {"10:11:9": b"\x08\x01"}
    assert report.events[0].path == "10:11:1"
    assert report.events[0].timestamp == 123
    assert "secret" not in repr(report)


def test_ack_status_and_path_validation():
    path = (10, 11, 4)
    check_response(envelope(), 5, path)  # Default-zero proto3 success.
    with pytest.raises(CommandError) as error:
        check_response(
            envelope(payload=varint_field(2, 12) + varint_field(3, 7)), 5, path
        )
    assert (error.value.status, error.value.cluster_status) == (12, 7)
    with pytest.raises(ProtocolError):
        check_response(
            envelope(payload=bytes_field(1, encode_path((10, 11, 2)))), 5, path
        )
    with pytest.raises(CommandError):
        check_response(envelope(opcode=0), 5, path)
    with pytest.raises(ProtocolError):
        check_response(envelope(opcode=3), 3, path)
    check_response(envelope(opcode=3, payload=bytes_field(1, b"")), 3, path)


@pytest.fixture
def transport():
    api = SimpleNamespace(user_id="123", client_id="abc", async_request=AsyncMock())
    transport = UFeederTransport(api, command_timeout=0.03)
    transport._devices = {"device"}
    transport._client = SimpleNamespace(publish=AsyncMock())
    transport._task = Mock()
    transport._ready.set()
    return transport


@pytest.mark.asyncio
async def test_fast_response_before_publish_returns(transport):
    async def publish(topic, payload, **kwargs):
        request = Interaction.decode(payload)
        assert request.message_id == 1
        assert topic == "/41d9698a795/device/123/thing/service"
        assert kwargs == {"qos": 1, "retain": False}
        transport._handle_message(
            "/41d9698a795/device/123/upload/reply",
            envelope(exchange=request.exchange_id).encode(),
        )
        await asyncio.sleep(0)

    transport._client.publish.side_effect = publish
    await transport.async_invoke("device", (10, 11, 4))
    assert not transport._pending
    assert transport._client.publish.await_count == 1


@pytest.mark.asyncio
async def test_ack_correlation_ignores_other_account_device_topic_and_exchange(
    transport,
):
    async def publish(topic, payload, **kwargs):
        request = Interaction.decode(payload)
        reply_topic = "/41d9698a795/device/123/upload/reply"
        for raw, bad_topic in (
            (envelope(exchange=(request.exchange_id + 1) % 65536), reply_topic),
            (
                envelope(exchange=request.exchange_id, source="another-device"),
                reply_topic,
            ),
            (
                envelope(exchange=request.exchange_id, destination="another-user"),
                reply_topic,
            ),
            (envelope(exchange=request.exchange_id), "/wrong/topic"),
        ):
            transport._handle_message(bad_topic, raw.encode())
        assert not transport._pending[request.exchange_id].future.done()
        transport._handle_message(
            reply_topic, envelope(exchange=request.exchange_id).encode()
        )

    transport._client.publish.side_effect = publish
    await transport.async_invoke("device", (10, 11, 4))


@pytest.mark.asyncio
async def test_timeout_does_not_retry_and_cleans_pending(transport):
    with pytest.raises(CommandTimeout):
        await transport.async_invoke("device", (10, 11, 2))
    assert transport._client.publish.await_count == 1
    assert not transport._pending


@pytest.mark.asyncio
async def test_disconnect_does_not_retry(transport):
    async def publish(*args, **kwargs):
        transport._fail_pending()

    transport._client.publish.side_effect = publish
    with pytest.raises(CannotConnect):
        await transport.async_invoke("device", (10, 11, 2))
    assert transport._client.publish.await_count == 1
    assert not transport._pending


@pytest.mark.asyncio
async def test_offline_transport_never_publishes(transport):
    transport._ready.clear()
    with pytest.raises(CannotConnect, match="not sent"):
        await transport.async_write("device", (10, 11, 8), b"\x08\x01")
    transport._client.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_cleans_pending(transport):
    task = asyncio.create_task(transport.async_invoke("device", (10, 11, 2)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not transport._pending
    assert transport._client.publish.await_count == 1


@pytest.mark.asyncio
async def test_rejected_device_response_propagates(transport):
    async def publish(topic, payload, **kwargs):
        request = Interaction.decode(payload)
        response = envelope(exchange=request.exchange_id, payload=varint_field(2, 5))
        transport._handle_message(
            "/41d9698a795/device/123/upload/reply", response.encode()
        )

    transport._client.publish.side_effect = publish
    with pytest.raises(CommandError, match="status=5"):
        await transport.async_invoke("device", (10, 11, 4))


def test_topic_injection_rejected():
    for value in ("", "#", "device/other", "a+b", "a\x00"):
        with pytest.raises(ProtocolError):
            UFeederTransport._segment(value)


@pytest.mark.asyncio
async def test_connection_lifecycle_subscribes_before_ready_and_stops(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.subscribe = AsyncMock(return_value=(1,))
            self.unsubscribe = AsyncMock()
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        @property
        def messages(self):
            async def incoming():
                await asyncio.Event().wait()
                yield  # pragma: no cover

            return incoming()

    clients = []
    monkeypatch.setattr(
        "custom_components.ufeeder.transport.aiomqtt.Client", FakeClient
    )
    api = SimpleNamespace(
        user_id="123",
        client_id="a" * 32,
        async_request=AsyncMock(
            return_value={
                "mqttUrl": "mqtts://ucsp-eu.ubtrobot.com:20000",
                "username": "private-user",
                "password": "private-password",
            }
        ),
    )
    changes = []
    transport = UFeederTransport(api, on_connection_change=changes.append)
    try:
        await transport.async_start(["device"])
        await asyncio.wait_for(transport._ready.wait(), timeout=2)
        client = clients[0]
        assert client.kwargs["identifier"] == "ha-" + "a" * 20
        assert client.kwargs["port"] == 20000
        assert client.kwargs["tls_context"].check_hostname is True
        assert client.kwargs["clean_session"] is True
        topics = {call.args[0] for call in client.subscribe.await_args_list}
        assert topics == {
            "/41d9698a795/device/123/upload/reply",
            "/41d9698a795/device/thing/property/post",
            "/41d9698a795/device/thing/event/post",
        }
        await transport.async_set_devices([])
        assert client.unsubscribe.await_count == 3
    finally:
        await transport.async_stop()
    assert clients[0].closed
    assert changes == [True, False]
    assert not transport.connected
    assert transport._task is None


@pytest.mark.asyncio
async def test_broker_rejecting_subscription_is_not_treated_as_connected(transport):
    transport._client.subscribe = AsyncMock(return_value=(128,))
    with pytest.raises(CannotConnect, match="subscription was rejected"):
        await transport._subscribe_devices()
    assert not transport._subscriptions
