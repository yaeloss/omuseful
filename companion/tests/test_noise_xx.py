# SPDX-License-Identifier: Apache-2.0
"""Handshake regression tests for the Noise_XX_25519_AESGCM_SHA256 stack.

Fixed keys make every run byte-identical: message 1 must be exactly the
initiator's ephemeral public key, both sides must agree on the handshake
hash and transport keys, and tampered or replayed material must fail.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from musegadget.noise import noise_xx
from musegadget.noise.noise_xx import (
    NoiseProtocolError,
    NoiseXXInitiator,
    NoiseXXResponder,
    _X25519KeyPair,
)

# Distinct fixed private keys for initiator/responder ephemeral and static.
_PRIVATE = [bytes([value] * 32) for value in (0x11, 0x22, 0x33, 0x44)]


def _keypair(private_bytes: bytes) -> _X25519KeyPair:
    private = x25519.X25519PrivateKey.from_private_bytes(private_bytes)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _X25519KeyPair(private_key=private, public_key_bytes=public)


def _fixed_keys(monkeypatch):
    keys = iter(_keypair(raw) for raw in _PRIVATE)
    monkeypatch.setattr(noise_xx, "_generate_x25519_key_pair", lambda: next(keys))


def _handshake(monkeypatch):
    _fixed_keys(monkeypatch)
    initiator, responder = NoiseXXInitiator(), NoiseXXResponder()
    initiator.initialize()
    responder.initialize()
    msg1 = initiator.write_message1()
    msg2 = responder.read_message1_and_write_message2(msg1)
    assert initiator.read_message2(msg2) == b""
    msg3 = initiator.write_message3()
    responder.read_message3(msg3)
    return initiator, responder, msg1


# Golden wire bytes and handshake hash for the fixed keys above, frozen so a
# change to the shared symmetric-state code (which both sides use, and which
# a round-trip alone cannot catch drifting from the Noise_XX spec) fails
# here instead of against a real Muse VM.
_MSG2_HEX = (
    "0faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20"
    "83be4718bc8739c3ea26ed6075c008d2f47a152fcbe9837641dc39e08b096889"
    "b7b97092f86c82d33ab49d34de944bd6a1d11d13912035c9c069d16f946e55fd")
_MSG3_HEX = (
    "fd1098a9346acf3d47fa7ad40180f1554b9d148a5cec2c67d775374d444c62c8"
    "c0f355af97d6d5449d918ecf367b6c0d46e39fcccb00a21770fa0d6a84f68a7e")
_HANDSHAKE_HASH_HEX = "5038a31c611d8d2ac0e82a941fc4373f2f3c25d189536cf60183fe2010c5eaeb"


def test_message1_is_exactly_the_ephemeral_public_key(monkeypatch):
    _fixed_keys(monkeypatch)
    initiator = NoiseXXInitiator()
    initiator.initialize()
    assert initiator.write_message1() == _keypair(_PRIVATE[0]).public_key_bytes


def test_golden_message2_message3_and_handshake_hash(monkeypatch):
    _fixed_keys(monkeypatch)
    initiator, responder = NoiseXXInitiator(), NoiseXXResponder()
    initiator.initialize()
    responder.initialize()
    msg2 = responder.read_message1_and_write_message2(initiator.write_message1())
    assert msg2.hex() == _MSG2_HEX
    assert initiator.read_message2(msg2) == b""
    msg3 = initiator.write_message3()
    assert msg3.hex() == _MSG3_HEX
    responder.read_message3(msg3)
    assert initiator.handshake_hash().hex() == _HANDSHAKE_HASH_HEX
    assert responder.handshake_hash().hex() == _HANDSHAKE_HASH_HEX


def test_both_sides_agree_and_transport_round_trips(monkeypatch):
    initiator, responder, _ = _handshake(monkeypatch)
    assert initiator.handshake_hash() == responder.handshake_hash()
    assert len(initiator.handshake_hash()) == 32
    send_i, recv_i = initiator.split()
    send_r, recv_r = responder.split()
    wire = send_i.encrypt_with_ad(b"", b"ping")
    assert recv_r.decrypt_with_ad(b"", wire) == b"ping"
    wire = send_r.encrypt_with_ad(b"", b"pong")
    assert recv_i.decrypt_with_ad(b"", wire) == b"pong"


def test_a_replayed_transport_frame_is_rejected(monkeypatch):
    initiator, responder, _ = _handshake(monkeypatch)
    send_i, _ = initiator.split()
    _, recv_r = responder.split()
    wire = send_i.encrypt_with_ad(b"", b"ping")
    assert recv_r.decrypt_with_ad(b"", wire) == b"ping"
    with pytest.raises(NoiseProtocolError):
        recv_r.decrypt_with_ad(b"", wire)  # nonce has moved on


def test_a_tampered_message2_kills_the_handshake(monkeypatch):
    _fixed_keys(monkeypatch)
    initiator, responder = NoiseXXInitiator(), NoiseXXResponder()
    initiator.initialize()
    responder.initialize()
    msg2 = bytearray(responder.read_message1_and_write_message2(initiator.write_message1()))
    msg2[-1] ^= 0xFF
    with pytest.raises(NoiseProtocolError):
        initiator.read_message2(bytes(msg2))
    with pytest.raises(NoiseProtocolError):
        initiator.write_message3()  # a failed handshake stays dead


def test_a_low_order_ephemeral_is_rejected(monkeypatch):
    _fixed_keys(monkeypatch)
    responder = NoiseXXResponder()
    responder.initialize()
    with pytest.raises(NoiseProtocolError):
        responder.read_message1_and_write_message2(bytes(32))
