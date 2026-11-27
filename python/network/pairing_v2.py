"""Security v2 3-flight ECDH + PIN/TOFU pairing handshake and gateway key distribution."""

import hmac
import hashlib
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from constants import (
    CRYPTO_MODE_V1,
    CRYPTO_MODE_V2,
    CRYPTO_MODE_V2_UNVERIFIED,
    PKT_KEY_DISTRIB,
    PKT_PAIR_ACK,
    PKT_PAIR_CONFIRM,
    PROTO_VERSION_V2,
    V2_MAX_FRAME_ESP8266,
    V2_PAIR_CONFIRM_LEN,
)
from network.packet_protocol import MACFormatter, PacketDecoderV2, PacketEncoderV2, V2_HEADER_STRUCT
from network.security_store import SecurityStore

logger = logging.getLogger(__name__)

BACKEND_MAC_STR = "00:00:00:00:00:00"
BACKEND_MAC_BYTES = b"\x00" * 6
HANDSHAKE_TIMEOUT_SECONDS = 30.0
MAX_FAILED_ATTEMPTS_PER_WINDOW = 3

KEY_DISTRIB_REMOVE = 0
KEY_DISTRIB_UPSERT = 1

CRYPTO_MODE_TO_CODE = {
    CRYPTO_MODE_V1: 0,
    CRYPTO_MODE_V2_UNVERIFIED: 1,
    CRYPTO_MODE_V2: 2,
}


@dataclass
class PendingHandshake:
    device_mac: str
    device_type: int
    max_frame: int
    has_pin: bool
    pin: Optional[str]
    epoch: int
    k_own: bytes
    k_ack: bytes
    k_session: bytes
    expected_proof_d: bytes
    sender_ip: str
    hello_data: dict
    created_at: float
    pub_d: bytes = b""
    nonce_d: bytes = b""
    confirm_frame: bytes = b""


class PairingV2SessionManager:
    """Manages active v2 ECDH+PIN/TOFU pairing handshakes and per-window failure lockouts."""

    def __init__(self):
        self._lock = threading.RLock()
        self._pending: Dict[str, PendingHandshake] = {}
        self._fail_counts: Dict[str, int] = {}
        self._recently_completed: Dict[str, float] = {}

    def reset_window_failures(self) -> None:
        """Clear per-device failure counters when a new pairing window opens."""
        with self._lock:
            self._fail_counts.clear()
            self._prune_expired_locked()

    def _prune_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [
            mac for mac, hs in self._pending.items()
            if (now - hs.created_at) > HANDSHAKE_TIMEOUT_SECONDS
        ]
        for mac in expired:
            del self._pending[mac]
        expired_done = [
            mac for mac, ts in self._recently_completed.items()
            if (now - ts) > 5.0
        ]
        for mac in expired_done:
            del self._recently_completed[mac]

    def has_pending(self, device_mac: str) -> bool:
        norm = MACFormatter.format_with_colons(device_mac)
        with self._lock:
            self._prune_expired_locked()
            return norm in self._pending

    def cancel_pending(self, device_mac: str) -> None:
        """Clear any in-flight v2 pairing handshake for a revoked/deleted MAC."""
        norm = MACFormatter.format_with_colons(device_mac)
        with self._lock:
            self._pending.pop(norm, None)

    def is_locked_out(self, device_mac: str) -> bool:
        norm = MACFormatter.format_with_colons(device_mac)
        with self._lock:
            return self._fail_counts.get(norm, 0) >= MAX_FAILED_ATTEMPTS_PER_WINDOW

    def _record_failure(self, device_mac: str) -> int:
        norm = MACFormatter.format_with_colons(device_mac)
        with self._lock:
            count = self._fail_counts.get(norm, 0) + 1
            self._fail_counts[norm] = count
            if count >= MAX_FAILED_ATTEMPTS_PER_WINDOW:
                logger.warning(
                    f"SECURITY: Pairing lockout triggered for {norm} after {count} failed handshakes"
                )
            return count

    @staticmethod
    def build_transcript(
        pub_d: bytes,
        pub_b: bytes,
        nonce_d: bytes,
        nonce_b: bytes,
        mac_d: bytes,
        mac_b: bytes,
        epoch: int,
    ) -> bytes:
        return pub_d + pub_b + nonce_d + nonce_b + mac_d + mac_b + struct.pack("<H", int(epoch))

    def handle_hello_v2(
        self,
        device_mac: str,
        device_type: int,
        hello_data: dict,
        home_id: int,
        sender_ip: str,
        store: SecurityStore,
        active_pin: Optional[str] = None,
        assigned_device_id: int = 0,
    ) -> Optional[bytes]:
        """Process Flight 1 (v1-framed HELLO with v2 cap TLV + ECDH pub_D/nonce_D) and build Flight 2.

        Returns 151-byte PKT_PAIR_CONFIRM v2 handshake frame, or None if rejected.
        """
        norm = MACFormatter.format_with_colons(device_mac)
        if self.is_locked_out(norm):
            logger.warning(f"SECURITY: Ignoring v2 pairing HELLO from locked-out device {norm}")
            return None

        pub_d = hello_data.get("ecdh_pub")
        nonce_d = hello_data.get("nonce_d")
        if not pub_d or len(pub_d) != 32 or not nonce_d or len(nonce_d) != 8:
            logger.warning(f"SECURITY: v2 HELLO from {norm} missing ECDH public key or nonce")
            return None

        # Hold self._lock across check + keygen + store so concurrent worker threads
        # forwarding the same broadcast HELLO from multiple gateways never race and
        # overwrite self._pending[norm] with a second ephemeral PAIR_CONFIRM.
        with self._lock:
            self._prune_expired_locked()
            existing = self._pending.get(norm)
            if (
                existing is not None
                and existing.pub_d == pub_d
                and existing.nonce_d == nonce_d
                and existing.confirm_frame
            ):
                new_rssi = hello_data.get("rssi", -100)
                old_rssi = existing.hello_data.get("rssi", -100)
                if new_rssi > old_rssi:
                    existing.sender_ip = sender_ip
                    existing.hello_data = dict(hello_data)
                logger.debug(
                    f"🔐 Reusing in-flight v2 PAIR_CONFIRM for duplicate HELLO from {norm} via {sender_ip}"
                )
                return existing.confirm_frame

            has_pin = bool(hello_data.get("has_pin", False))
            max_frame = int(hello_data.get("max_frame", V2_MAX_FRAME_ESP8266))
            if max_frame < 250 or max_frame > 1470:
                max_frame = V2_MAX_FRAME_ESP8266

            rec = store.get_device_record(norm)
            stored_pin = (rec.get("pin") if rec else None) or None

            # Prevent PIN-stripping downgrade: if the user entered a PIN for this pairing
            # window OR this MAC already has a stored PIN, refuse TOFU even if the
            # unauthenticated HELLO cleared V2_HELLO_FLAG_HAS_PIN.
            require_pin = has_pin or bool(active_pin) or bool(stored_pin)
            pin_used: Optional[str] = None
            if require_pin:
                if not has_pin:
                    logger.warning(
                        f"SECURITY: Rejected TOFU downgrade attempt for {norm} "
                        f"(active_pin={bool(active_pin)}, stored_pin={bool(stored_pin)})"
                    )
                    return None
                # Prefer PIN entered in current pairing window; fall back to pre-registered device PIN
                pin_used = active_pin or stored_pin
                if not pin_used or len(pin_used) != 6 or not pin_used.isdigit():
                    logger.warning(
                        f"SECURITY: Device {norm} requires a 6-digit PIN, but none was entered for this pairing window"
                    )
                    return None
                secret = b"HML2-PIN:" + pin_used.encode("ascii")
            else:
                secret = b"HML2-TOFU"

            # Generate ephemeral X25519 keypair + 8-byte nonce
            priv_b = X25519PrivateKey.generate()
            pub_b = priv_b.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            nonce_b = os.urandom(8)

            try:
                shared = priv_b.exchange(X25519PublicKey.from_public_bytes(pub_d))
            except Exception as exc:
                logger.warning(f"SECURITY: Invalid X25519 public key from {norm}: {exc}")
                self._record_failure(norm)
                return None

            if not any(shared):
                logger.warning(f"SECURITY: Rejected all-zero X25519 shared secret from {norm}")
                self._record_failure(norm)
                return None

            epoch = store.epoch
            mac_d_bytes = MACFormatter.to_bytes(norm)
            k_session = SecurityStore.derive_pairing_session_key(
                shared, secret, nonce_d, nonce_b, mac_d_bytes, BACKEND_MAC_BYTES, epoch
            )
            transcript = self.build_transcript(
                pub_d, pub_b, nonce_d, nonce_b, mac_d_bytes, BACKEND_MAC_BYTES, epoch
            )

            proof_b = hmac.new(k_session, b"backend-ok" + transcript, hashlib.sha256).digest()
            expected_proof_d = hmac.new(k_session, b"device-ok" + transcript, hashlib.sha256).digest()

            k_own = store.derive_k_own(norm, epoch=epoch)
            k_ack = store.derive_k_ack(norm, epoch=epoch)
            key_bundle = k_own + k_ack + struct.pack("<H", epoch)  # 34 bytes
            wrap = AESGCM(k_session).encrypt(b"\x00" * 12, key_bundle, transcript)  # 50 bytes

            counter = store.next_backend_tx_counter()
            header = V2_HEADER_STRUCT.pack(
                PKT_PAIR_CONFIRM,
                PROTO_VERSION_V2,
                BACKEND_MAC_BYTES,
                mac_d_bytes,
                0,
                counter & 0xFFFFFFFF,
                epoch & 0xFFFF,
                0,
            )
            body = (
                pub_b
                + nonce_b
                + struct.pack("<IBH", home_id & 0xFFFFFFFF, assigned_device_id & 0xFF, epoch & 0xFFFF)
                + proof_b
                + wrap
            )
            frame = header + body
            assert len(frame) == V2_PAIR_CONFIRM_LEN, f"Unexpected PAIR_CONFIRM len {len(frame)}"

            self._pending[norm] = PendingHandshake(
                device_mac=norm,
                device_type=device_type,
                max_frame=max_frame,
                has_pin=has_pin,
                pin=pin_used,
                epoch=epoch,
                k_own=k_own,
                k_ack=k_ack,
                k_session=k_session,
                expected_proof_d=expected_proof_d,
                sender_ip=sender_ip,
                hello_data=dict(hello_data),
                created_at=time.monotonic(),
                pub_d=bytes(pub_d),
                nonce_d=bytes(nonce_d),
                confirm_frame=frame,
            )

        logger.info(
            f"🔐 Sent v2 PAIR_CONFIRM to {norm} (pin={'yes' if has_pin else 'TOFU'}, max_frame={max_frame})"
        )
        return frame

    def handle_pair_ack(self, frame: bytes, store: SecurityStore) -> Optional[dict]:
        """Process Flight 3 (PKT_PAIR_ACK encrypted under K_own[d]) and commit pairing.

        Returns metadata dict on success, or None if verification fails.
        """
        hdr = PacketDecoderV2.parse_header(frame)
        if not hdr or hdr["type"] != PKT_PAIR_ACK or hdr["version"] != PROTO_VERSION_V2:
            return None

        norm = hdr["source_mac"]
        with self._lock:
            self._prune_expired_locked()
            hs = self._pending.get(norm)
            recently_done = norm in self._recently_completed

        if hs is None:
            if recently_done:
                logger.debug(f"Ignoring duplicate PKT_PAIR_ACK from recently paired device {norm}")
            else:
                logger.warning(f"SECURITY: Unexpected PKT_PAIR_ACK from {norm} (no pending handshake)")
            return None

        decoded = PacketDecoderV2.decode_with_key(frame, hs.k_own)
        if decoded is None:
            logger.warning(f"SECURITY: PKT_PAIR_ACK from {norm} failed AES-GCM verification (wrong PIN/MITM?)")
            self._record_failure(norm)
            return None

        proof_d = decoded["payload"]
        if len(proof_d) != 32 or not hmac.compare_digest(proof_d, hs.expected_proof_d):
            logger.warning(f"SECURITY: PKT_PAIR_ACK from {norm} had invalid proof_D")
            self._record_failure(norm)
            return None

        crypto_mode = CRYPTO_MODE_V2 if hs.has_pin else CRYPTO_MODE_V2_UNVERIFIED
        store.set_device_record(
            norm,
            crypto_mode=crypto_mode,
            max_frame=hs.max_frame,
            pin=hs.pin,
            key_epoch=hs.epoch,
        )
        # Reset replay baseline so a reflashed/re-paired device starts fresh,
        # then record this PAIR_ACK's counter as the initial high-water mark.
        store.reset_replay(norm)
        store.check_and_update_replay(norm, decoded["counter"], decoded["ciphertext"])

        with self._lock:
            self._pending.pop(norm, None)
            self._fail_counts.pop(norm, None)
            self._recently_completed[norm] = time.monotonic()

        logger.info(f"✅ Completed v2 pairing for {norm} (mode={crypto_mode}, max_frame={hs.max_frame})")
        return {
            "device_mac": norm,
            "device_type": hs.device_type,
            "crypto_mode": crypto_mode,
            "max_frame": hs.max_frame,
            "sender_ip": hs.sender_ip,
            "hello_data": hs.hello_data,
            "k_ack": hs.k_ack,
        }


def encode_key_distrib(
    gw_radio_mac: str,
    target_mac: str,
    crypto_mode: str,
    max_frame: int,
    k_ack: bytes,
    action: int,
    store: SecurityStore,
    msg_id: int = 0,
) -> bytes:
    """Encode PKT_KEY_DISTRIB (0x07) encrypted under K_own[gw_radio_mac].

    Plaintext (26 B): action(1) || target_mac(6) || mode_code(1) || max_frame(2 LE) || k_ack(16)
    """
    if len(k_ack) != 16:
        raise ValueError("k_ack must be 16 bytes")
    mode_code = CRYPTO_MODE_TO_CODE.get(crypto_mode, 0)
    plaintext = (
        struct.pack("<B", action & 0xFF)
        + MACFormatter.to_bytes(target_mac)
        + struct.pack("<BH", mode_code, int(max_frame) & 0xFFFF)
        + k_ack
    )
    gw_key = store.derive_k_own(gw_radio_mac)
    counter = store.next_backend_tx_counter()
    return PacketEncoderV2.encode(
        pkt_type=PKT_KEY_DISTRIB,
        src_mac=BACKEND_MAC_STR,
        dst_mac=gw_radio_mac,
        msg_id=msg_id,
        counter=counter,
        key_epoch=store.epoch,
        plaintext=plaintext,
        key=gw_key,
    )


# Global singleton
pairing_v2_manager = PairingV2SessionManager()
