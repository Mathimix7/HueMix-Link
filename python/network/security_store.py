"""Security v2 key store, HKDF derivations, TX counter reservation, and replay protection."""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import struct
import threading
import zlib
from pathlib import Path
from typing import Dict, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from constants import (
    CRYPTO_MODE_V1,
    CRYPTO_MODE_V2,
    CRYPTO_MODE_V2_UNVERIFIED,
    FILE_SECURITY,
    FILE_SECURITY_COUNTERS,
    V2_COUNTER_BLOCK_SIZE_BACKEND,
    V2_MAX_FRAME_ESP8266,
)

logger = logging.getLogger(__name__)


def _normalize_mac(mac: str) -> str:
    clean = mac.replace(":", "").replace("-", "").upper()
    if len(clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    return ":".join(clean[i:i + 2] for i in range(0, 12, 2))


def _mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(_normalize_mac(mac).replace(":", ""))


def derive_hkdf_key(ikm: bytes, salt: bytes, info: bytes, length: int = 16) -> bytes:
    """Derive a key using HKDF-SHA256."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt if salt else None,
        info=info,
    ).derive(ikm)


class SecurityStore:
    """Manages House Master Key (HMK), per-device keys/modes, TX counter blocks, and replay state."""

    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is None:
            data_dir = str(Path(__file__).parent.parent / "data")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.security_file = self.data_dir / FILE_SECURITY
        self.counters_db = self.data_dir / FILE_SECURITY_COUNTERS

        self._lock = threading.RLock()
        self._state: Dict = {}

        # In-memory backend TX counter reservation window [next_tx, reserved_end)
        self._tx_next = 0
        self._tx_reserved_end = 0

        self._load_or_init_security_file()
        self._init_counters_db()

    # ------------------------------------------------------------------
    # Key store persistence (security.json, mode 0600 on POSIX)
    # ------------------------------------------------------------------

    def _load_or_init_security_file(self) -> None:
        with self._lock:
            if self.security_file.exists():
                with open(self.security_file, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
                if "hmk" not in self._state or len(bytes.fromhex(self._state["hmk"])) != 16:
                    raise ValueError(f"Corrupted HMK in {self.security_file}")
                self._state.setdefault("epoch", 1)
                self._state.setdefault("devices", {})
            else:
                self._state = {
                    "hmk": os.urandom(16).hex(),
                    "epoch": 1,
                    "devices": {},
                }
                self._save_security_file()
                logger.info(f"Initialized new Security v2 store at {self.security_file}")

    def _save_security_file(self) -> None:
        with self._lock:
            tmp_path = self.security_file.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.security_file)
            try:
                os.chmod(self.security_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows does not enforce POSIX 0600

    @property
    def hmk(self) -> bytes:
        with self._lock:
            return bytes.fromhex(self._state["hmk"])

    @property
    def epoch(self) -> int:
        with self._lock:
            return int(self._state.get("epoch", 1))

    # ------------------------------------------------------------------
    # HKDF Key Derivations (unicast-only: K_own[d] and K_ack[d])
    # ------------------------------------------------------------------

    def derive_k_own(self, mac: str, epoch: Optional[int] = None) -> bytes:
        """Derive per-device unicast key K_own[mac] = HKDF-SHA256(HMK, 'own-v2' || epoch || MAC)."""
        ep = self.epoch if epoch is None else int(epoch)
        info = b"own-v2" + struct.pack("<H", ep) + _mac_to_bytes(mac)
        return derive_hkdf_key(self.hmk, b"", info, 16)

    def derive_k_ack(self, mac: str, epoch: Optional[int] = None) -> bytes:
        """Derive gateway-local ACK key K_ack[mac] = HKDF-SHA256(HMK, 'ack-v2' || epoch || MAC)."""
        ep = self.epoch if epoch is None else int(epoch)
        info = b"ack-v2" + struct.pack("<H", ep) + _mac_to_bytes(mac)
        return derive_hkdf_key(self.hmk, b"", info, 16)

    @staticmethod
    def derive_pairing_session_key(
        ecdh_shared: bytes,
        secret: bytes,
        nonce_d: bytes,
        nonce_b: bytes,
        mac_d: bytes,
        mac_b: bytes,
        epoch: int,
    ) -> bytes:
        """Derive 16-byte pairing K_session from ECDH shared secret + PIN/TOFU secret."""
        ikm = ecdh_shared + secret
        salt = nonce_d + nonce_b
        info = b"HueMixLink-pair-v2" + mac_d + mac_b + struct.pack("<H", int(epoch))
        return derive_hkdf_key(ikm, salt, info, 16)

    # ------------------------------------------------------------------
    # Per-device security records
    # ------------------------------------------------------------------

    def get_device_record(self, mac: str) -> Optional[Dict]:
        norm = _normalize_mac(mac)
        with self._lock:
            rec = self._state.get("devices", {}).get(norm)
            return dict(rec) if rec else None

    def get_all_devices_snapshot(self) -> Dict[str, Dict]:
        """Return a thread-safe shallow copy of all device security records."""
        with self._lock:
            return {mac: dict(rec) for mac, rec in self._state.get("devices", {}).items()}

    def get_crypto_mode(self, mac: str) -> str:
        """Return device crypto_mode ('v1', 'v2-unverified', 'v2'); unknown defaults to 'v1'."""
        rec = self.get_device_record(mac)
        if not rec:
            return CRYPTO_MODE_V1
        return rec.get("crypto_mode", CRYPTO_MODE_V1)

    def is_v2_device(self, mac: str) -> bool:
        return self.get_crypto_mode(mac) in (CRYPTO_MODE_V2, CRYPTO_MODE_V2_UNVERIFIED)

    def get_max_frame(self, mac: str) -> int:
        rec = self.get_device_record(mac)
        if not rec:
            return V2_MAX_FRAME_ESP8266
        return int(rec.get("max_frame", V2_MAX_FRAME_ESP8266))

    def set_device_record(
        self,
        mac: str,
        crypto_mode: str,
        max_frame: int = V2_MAX_FRAME_ESP8266,
        pin: Optional[str] = None,
        key_epoch: Optional[int] = None,
    ) -> None:
        if crypto_mode not in (CRYPTO_MODE_V1, CRYPTO_MODE_V2_UNVERIFIED, CRYPTO_MODE_V2):
            raise ValueError(f"Invalid crypto_mode: {crypto_mode}")
        norm = _normalize_mac(mac)
        with self._lock:
            existing = self._state.setdefault("devices", {}).get(norm, {})
            record = {
                "crypto_mode": crypto_mode,
                "max_frame": int(max_frame),
                "pin": pin if pin is not None else existing.get("pin"),
                "key_epoch": int(key_epoch if key_epoch is not None else self.epoch),
            }
            if existing.get("pending_pin") and crypto_mode != CRYPTO_MODE_V2:
                record["pending_pin"] = existing["pending_pin"]
            if existing.get("pending_v2_upgrade") and crypto_mode == CRYPTO_MODE_V1:
                record["pending_v2_upgrade"] = True
            self._state["devices"][norm] = record
            self._save_security_file()

    def get_device_pin(self, mac: str) -> Optional[str]:
        rec = self.get_device_record(mac)
        if not rec:
            return None
        pin = rec.get("pin")
        return str(pin) if pin else None

    def set_pending_v2_upgrade(self, mac: str, pending: bool) -> None:
        norm = _normalize_mac(mac)
        with self._lock:
            devices = self._state.setdefault("devices", {})
            rec = devices.setdefault(norm, {
                "crypto_mode": CRYPTO_MODE_V1,
                "max_frame": V2_MAX_FRAME_ESP8266,
                "pin": None,
                "key_epoch": self.epoch,
            })
            if pending:
                rec["pending_v2_upgrade"] = True
            else:
                rec.pop("pending_v2_upgrade", None)
            self._save_security_file()

    def is_pending_v2_upgrade(self, mac: str) -> bool:
        rec = self.get_device_record(mac)
        return bool(rec and rec.get("pending_v2_upgrade"))

    def set_pending_pin(self, mac: str, pin: Optional[str]) -> None:
        norm = _normalize_mac(mac)
        with self._lock:
            devices = self._state.setdefault("devices", {})
            rec = devices.setdefault(norm, {
                "crypto_mode": CRYPTO_MODE_V1,
                "max_frame": V2_MAX_FRAME_ESP8266,
                "pin": None,
                "key_epoch": self.epoch,
            })
            if pin:
                rec["pending_pin"] = str(pin)
            else:
                rec.pop("pending_pin", None)
            self._save_security_file()

    def get_pending_pin(self, mac: str) -> Optional[str]:
        rec = self.get_device_record(mac)
        if not rec:
            return None
        pin = rec.get("pending_pin")
        return str(pin) if pin else None

    def commit_pin(self, mac: str, pin: str) -> None:
        norm = _normalize_mac(mac)
        with self._lock:
            devices = self._state.setdefault("devices", {})
            rec = devices.setdefault(norm, {
                "crypto_mode": CRYPTO_MODE_V2,
                "max_frame": V2_MAX_FRAME_ESP8266,
                "pin": None,
                "key_epoch": self.epoch,
            })
            rec["pin"] = str(pin)
            rec["crypto_mode"] = CRYPTO_MODE_V2
            rec.pop("pending_pin", None)
            rec.pop("pending_v2_upgrade", None)
            rec.pop("revoked", None)
            self._save_security_file()

    def provision_device_pin(
        self,
        mac: str,
        pin: Optional[str] = None,
        last_name: Optional[str] = None,
        reuse_existing: bool = True,
    ) -> str:
        """Provision and persist a 6-digit PIN for a device being serial-flashed.

        Keeps `crypto_mode='v1'` (not yet paired) so the device must still complete
        the v2 PIN handshake on first boot, while storing the PIN in `security.json`.
        """
        norm = _normalize_mac(mac)
        with self._lock:
            devices = self._state.setdefault("devices", {})
            existing = devices.get(norm)
            if pin is not None:
                final_pin = str(pin)
            elif reuse_existing and existing and existing.get("pin"):
                final_pin = str(existing["pin"])
            else:
                final_pin = f"{secrets.randbelow(1_000_000):06d}"

            if len(final_pin) != 6 or not final_pin.isdigit():
                raise ValueError("PIN must be exactly 6 digits")

            rec = dict(existing) if existing else {
                "crypto_mode": CRYPTO_MODE_V1,
                "max_frame": V2_MAX_FRAME_ESP8266,
                "key_epoch": self.epoch,
            }
            rec["crypto_mode"] = CRYPTO_MODE_V1
            rec["pin"] = final_pin
            rec["key_epoch"] = self.epoch
            rec.pop("revoked", None)
            rec.pop("pending_pin", None)
            rec.pop("pending_v2_upgrade", None)
            if last_name:
                rec["last_name"] = str(last_name)
            devices[norm] = rec
            self._save_security_file()
            return final_pin

    def is_revoked(self, mac: str) -> bool:
        rec = self.get_device_record(mac)
        return bool(rec and rec.get("revoked"))

    def revoke_device(self, mac: str, keep_pin: bool = True, last_name: Optional[str] = None) -> None:
        """Revoke active v2 trust for a device, optionally keeping its 6-digit PIN for future re-pairing."""
        norm = _normalize_mac(mac)
        with self._lock:
            devices = self._state.setdefault("devices", {})
            existing = devices.get(norm)
            saved_pin = existing.get("pin") if existing else None
            if keep_pin and saved_pin:
                devices[norm] = {
                    "crypto_mode": CRYPTO_MODE_V1,
                    "max_frame": int(existing.get("max_frame", V2_MAX_FRAME_ESP8266)),
                    "pin": str(saved_pin),
                    "key_epoch": int(existing.get("key_epoch", self.epoch)),
                    "revoked": True,
                    "last_name": last_name or existing.get("last_name") or norm,
                }
                self._save_security_file()
            elif norm in devices:
                del devices[norm]
                self._save_security_file()
        self.reset_replay(norm)

    def list_saved_pins(self) -> list:
        """Return all security records that have a saved 6-digit PIN."""
        with self._lock:
            result = []
            for mac, rec in self._state.get("devices", {}).items():
                pin = rec.get("pin")
                if not pin:
                    continue
                result.append({
                    "mac": mac,
                    "pin": str(pin),
                    "revoked": bool(rec.get("revoked", False)),
                    "crypto_mode": rec.get("crypto_mode", CRYPTO_MODE_V1),
                    "last_name": rec.get("last_name"),
                })
            result.sort(key=lambda item: (not item["revoked"], item["mac"]))
            return result

    def forget_device_pin(self, mac: str) -> bool:
        """Permanently delete a saved PIN (and remove the record if the device is revoked)."""
        norm = _normalize_mac(mac)
        removed = False
        was_revoked = False
        with self._lock:
            devices = self._state.get("devices", {})
            rec = devices.get(norm)
            if not rec:
                return False
            if rec.get("revoked"):
                del devices[norm]
                removed = True
                was_revoked = True
            elif rec.get("pin"):
                rec["pin"] = None
                removed = True
            if removed:
                self._save_security_file()
        # Only reset replay state when removing a revoked device — never wipe replay protection on an active peer
        if was_revoked:
            self.reset_replay(norm)
        return removed

    def remove_device(self, mac: str) -> None:
        norm = _normalize_mac(mac)
        with self._lock:
            if norm in self._state.get("devices", {}):
                del self._state["devices"][norm]
                self._save_security_file()
        self.reset_replay(norm)

    # ------------------------------------------------------------------
    # SQLite crash-safe TX counter & receive-side replay protection
    # ------------------------------------------------------------------

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.counters_db), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        return conn

    def _init_counters_db(self) -> None:
        with self._lock:
            conn = self._connect_db()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tx_counters (
                        key TEXT PRIMARY KEY,
                        next_value INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO tx_counters (key, next_value)
                    VALUES ('backend_tx', 1)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS replay_state (
                        mac TEXT PRIMARY KEY,
                        last_counter INTEGER NOT NULL,
                        last_ct_hash BLOB NOT NULL,
                        reject_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
            self._reserve_tx_block()

    def _reserve_tx_block(self) -> None:
        """Atomically reserve a fresh block of backend TX counters in SQLite."""
        with self._lock:
            conn = self._connect_db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_value FROM tx_counters WHERE key = 'backend_tx'"
                ).fetchone()
                start = int(row[0]) if row else 1
                end = start + V2_COUNTER_BLOCK_SIZE_BACKEND
                conn.execute(
                    "UPDATE tx_counters SET next_value = ? WHERE key = 'backend_tx'",
                    (end,),
                )
                conn.commit()
                self._tx_next = start
                self._tx_reserved_end = end
            finally:
                conn.close()

    def next_backend_tx_counter(self) -> int:
        """Return a strictly increasing, crash-safe uint32 TX counter for backend->device packets."""
        with self._lock:
            if self._tx_next >= self._tx_reserved_end:
                self._reserve_tx_block()
            val = self._tx_next
            self._tx_next += 1
            return val

    def check_and_update_replay(self, mac: str, counter: int, ciphertext: bytes) -> str:
        """Validate sender counter against persisted replay table.

        Returns:
            'accept'    - strictly newer counter; persisted as new high-water mark
            'duplicate' - same counter and byte-identical ciphertext (legitimate retransmit)
            'reject'    - older counter or same counter with different ciphertext
        """
        norm = _normalize_mac(mac)
        ct_hash = hashlib.sha256(ciphertext).digest()
        with self._lock:
            conn = self._connect_db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT last_counter, last_ct_hash, reject_count FROM replay_state WHERE mac = ?",
                    (norm,),
                ).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO replay_state (mac, last_counter, last_ct_hash, reject_count) "
                        "VALUES (?, ?, ?, 0)",
                        (norm, int(counter), ct_hash),
                    )
                    conn.commit()
                    return "accept"

                last_counter, last_ct_hash, reject_count = int(row[0]), bytes(row[1]), int(row[2])
                if counter > last_counter:
                    conn.execute(
                        "UPDATE replay_state SET last_counter = ?, last_ct_hash = ? WHERE mac = ?",
                        (int(counter), ct_hash, norm),
                    )
                    conn.commit()
                    return "accept"

                if counter == last_counter and ct_hash == last_ct_hash:
                    conn.commit()
                    return "duplicate"

                conn.execute(
                    "UPDATE replay_state SET reject_count = ? WHERE mac = ?",
                    (reject_count + 1, norm),
                )
                conn.commit()
                return "reject"
            finally:
                conn.close()

    def reset_replay(self, mac: str) -> None:
        """Reset replay baseline for a device (must be called on successful re-pair)."""
        norm = _normalize_mac(mac)
        with self._lock:
            conn = self._connect_db()
            try:
                conn.execute("DELETE FROM replay_state WHERE mac = ?", (norm,))
                conn.commit()
            finally:
                conn.close()

    def get_replay_stats(self, mac: str) -> Optional[Dict]:
        norm = _normalize_mac(mac)
        with self._lock:
            conn = self._connect_db()
            try:
                row = conn.execute(
                    "SELECT last_counter, reject_count FROM replay_state WHERE mac = ?",
                    (norm,),
                ).fetchone()
                if not row:
                    return None
                return {"last_counter": int(row[0]), "reject_count": int(row[1])}
            finally:
                conn.close()


def build_esp32_nvs_pin_image(pin: str, partition_size: int = 0x5000) -> bytes:
    """Build a minimal ESP-IDF NVS v2 partition image containing namespace 'sec', key 'pin'.

    Matches `esp_idf_nvs_partition_gen` (V2) byte-for-byte with zero external dependencies.
    """
    if not isinstance(pin, str) or len(pin) != 6 or not pin.isdigit():
        raise ValueError("PIN must be a 6-digit string")
    if partition_size < 0x3000 or (partition_size % 0x1000) != 0:
        raise ValueError("partition_size must be a multiple of 0x1000 (>= 0x3000)")

    def _crc32(data: bytes) -> int:
        return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF

    buf = bytearray(b"\xff" * partition_size)

    # Page 0 header (32 B): state=ACTIVE(0xFFFFFFFE), seq=0, version=0xFE (V2), unused=0xFF*19, crc32
    hdr_body = struct.pack("<IB", 0, 0xFE) + (b"\xff" * 19)
    hdr = struct.pack("<I", 0xFFFFFFFE) + hdr_body + struct.pack("<I", _crc32(hdr_body))
    buf[0x00:0x20] = hdr

    # Entry state bitmap (32 B): entries 0, 1, 2 = WRITTEN (0b10), remaining = EMPTY (0b11)
    buf[0x20] = 0b11_10_10_10  # 0xEA

    # Entry 0 (0x40..0x5F): namespace "sec" -> ns_index = 1 (ns=0, type=U8(0x01), span=1, chunk=0xFF)
    ns_key = b"sec" + (b"\x00" * 13)
    ns_data = bytes([1]) + (b"\xff" * 7)
    ns_head = struct.pack("<BBBB", 0, 0x01, 1, 0xFF)
    ns_crc = _crc32(ns_head + ns_key + ns_data)
    buf[0x40:0x60] = ns_head + struct.pack("<I", ns_crc) + ns_key + ns_data

    # Entry 1 (0x60..0x7F): string header "pin" in ns=1 (type=STR(0x21), span=2, chunk=0xFF)
    str_bytes = pin.encode("ascii") + b"\x00"  # 7 bytes
    str_data_crc = _crc32(str_bytes)
    pin_key = b"pin" + (b"\x00" * 13)
    pin_meta = struct.pack("<HHI", len(str_bytes), 0xFFFF, str_data_crc)
    pin_head = struct.pack("<BBBB", 1, 0x21, 2, 0xFF)
    pin_crc = _crc32(pin_head + pin_key + pin_meta)
    buf[0x60:0x80] = pin_head + struct.pack("<I", pin_crc) + pin_key + pin_meta

    # Entry 2 (0x80..0x9F): string payload padded with 0xFF to 32 bytes
    buf[0x80:0x80 + len(str_bytes)] = str_bytes

    return bytes(buf)


# Singleton instance
security_store = SecurityStore()
