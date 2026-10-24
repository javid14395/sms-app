"""Safe ADB transport. Commands use argument arrays, bounded timeouts, and no host shell."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from typing import Callable

from .validation import validate_ip_port, validate_pairing_code, validate_port


class AdbError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, detail: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.detail = detail[:500]


@dataclass(frozen=True)
class MessagingAppInfo:
    resolved_activity: str
    default_sms_package: str = ""
    chooser_required: bool = False
    sim_count: int = 1
    transport_mode: str = "manual_composer_stage"
    limitations: str = (
        "ADB opens the Android SMS compose activity via SENDTO intent. "
        "ADB exit code 0 only confirms intent dispatch; a human operator must tap Send on the device "
        "and confirm in the desktop app. Delivery is not verified via ADB."
    )


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    detail: str = ""
    brand: str = ""
    model: str = ""
    alias: str = ""
    last_seen: str = ""
    last_checked: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def status_category(self) -> str:
        """Normalized device health category: usable, offline, unauthorized, reconnecting, or missing."""
        st = (self.state or "").lower()
        if st == "device":
            return "usable"
        if st == "offline":
            return "offline"
        if st in ("unauthorized", "authorizing"):
            return "unauthorized"
        if st in ("connecting", "reconnecting", "recovery", "sideload"):
            return "reconnecting"
        return "missing"

    @property
    def transport_type(self) -> str:
        if ":" in self.serial or "._adb-tls-" in self.serial:
            return "wifi"
        return "usb"

    @property
    def friendly_name(self) -> str:
        """Readable hardware identity, with ADB's model/product as a fallback.

        Fixed edge case: when both brand and model exist and model already starts
        with brand (e.g. brand='Samsung', model='Samsung SM-S918B'), return the
        full model rather than only the brand.
        """
        brand = _humanize_brand(self.brand)
        model = _humanize_model(self.model)
        if brand and model:
            if model.casefold().startswith(brand.casefold()):
                return model
            return f"{brand} {model}"
        if brand or model:
            return model or brand
        properties = dict(
            part.split(":", 1) for part in self.detail.split()
            if ":" in part
        )
        return (
            _humanize_model(properties.get("model", ""))
            or _humanize_model(properties.get("product", ""))
            or "Android device"
        )

    def display_label(self, *, include_serial: bool = True, disambiguate: bool = False) -> str:
        base = self.alias.strip() if self.alias and self.alias.strip() else self.friendly_name
        if include_serial:
            return f"{base} ({self.serial})"
        if disambiguate:
            suffix = self.serial[-4:] if len(self.serial) >= 4 else self.serial
            return f"{base} [{suffix}]"
        return base


def format_device_labels(devices: list[Device], *, aliases: dict[str, str] | None = None) -> dict[str, str]:
    """Build unambiguous display labels mapped from device serial -> display string."""
    aliases = aliases or {}
    enriched = [replace(d, alias=aliases.get(d.serial, d.alias)) for d in devices]
    counts: dict[str, int] = {}
    for d in enriched:
        base = d.alias.strip() if d.alias and d.alias.strip() else d.friendly_name
        counts[base.casefold()] = counts.get(base.casefold(), 0) + 1
    result: dict[str, str] = {}
    for d in enriched:
        base = d.alias.strip() if d.alias and d.alias.strip() else d.friendly_name
        if counts.get(base.casefold(), 0) > 1:
            result[d.serial] = f"{base} ({d.serial}) — {d.state}"
        else:
            result[d.serial] = f"{base} ({d.serial}) — {d.state}"
    return result


def _humanize_model(value: str) -> str:
    return " ".join((value or "").replace("_", " ").split()).strip()


def _humanize_brand(value: str) -> str:
    brand = _humanize_model(value)
    if not brand:
        return ""
    known = {
        "google": "Google",
        "samsung": "Samsung",
        "oneplus": "OnePlus",
        "motorola": "Motorola",
        "xiaomi": "Xiaomi",
        "oppo": "OPPO",
        "vivo": "vivo",
        "huawei": "HUAWEI",
        "honor": "HONOR",
        "sony": "Sony",
        "asus": "ASUS",
        "lenovo": "Lenovo",
        "realme": "realme",
        "nothing": "Nothing",
    }
    return known.get(brand.casefold(), brand.capitalize())


class AdbClient:
    def __init__(self, executable: str = "adb", timeout: float = 15.0):
        self.executable = executable
        self.timeout = timeout

    def check_availability(self) -> dict[str, str | bool]:
        """Verify whether the configured ADB binary exists and responds to `version`."""
        resolved_path = shutil.which(self.executable) or self.executable
        try:
            version_out = self.run(["version"], timeout=5.0)
            first_line = version_out.splitlines()[0] if version_out else "ADB available"
            return {
                "available": True,
                "executable": self.executable,
                "resolved_path": str(resolved_path),
                "version": first_line,
                "error": "",
            }
        except AdbError as exc:
            return {
                "available": False,
                "executable": self.executable,
                "resolved_path": str(resolved_path),
                "version": "",
                "error": str(exc),
            }

    def run(self, args: list[str], *, serial: str | None = None, timeout: float | None = None) -> str:
        command = [self.executable]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
                shell=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise AdbError(
                "ADB executable was not found. Configure its path or install Android platform-tools."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {effective_timeout:g} seconds.") from exc
        except OSError as exc:
            raise AdbError(f"Could not start ADB: {exc}") from exc
        output = (result.stdout or "").strip()
        if result.returncode:
            detail = (result.stderr or output).strip()
            raise AdbError(
                f"ADB command failed (exit {result.returncode}).",
                returncode=result.returncode,
                detail=detail,
            )
        return output

    def devices(self) -> list[Device]:
        output = self.run(["devices", "-l"])
        parsed = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                parsed.append(Device(parts[0], parts[1], " ".join(parts[2:])))
        return parsed

    def device_identity(self, serial: str) -> tuple[str, str]:
        """Read non-sensitive Android brand/model properties for display only."""
        brand = self.run(["shell", "getprop", "ro.product.brand"], serial=serial)
        model = self.run(["shell", "getprop", "ro.product.model"], serial=serial)
        return _humanize_model(brand), _humanize_model(model)

    def pair(self, endpoint: str, pairing_code: str) -> str:
        """Pair with an Android 11+ Wireless Debugging endpoint using a 6-digit pairing code.

        Pairing code is passed directly as a command argument and never persisted.
        """
        valid_endpoint = validate_ip_port(endpoint)
        valid_code = validate_pairing_code(pairing_code)
        output = self.run(["pair", valid_endpoint, valid_code], timeout=max(20.0, self.timeout))
        if "Failed" in output or "error:" in output.lower():
            raise AdbError("Wireless ADB pairing failed.", detail=output)
        return output or f"Paired with {valid_endpoint}"

    def connect(self, endpoint: str) -> str:
        """Connect to an already paired or TCP/IP-enabled wireless ADB endpoint."""
        valid_endpoint = validate_ip_port(endpoint)
        output = self.run(["connect", valid_endpoint], timeout=max(20.0, self.timeout))
        if "cannot connect" in output.lower() or "failed to connect" in output.lower() or "unable to connect" in output.lower():
            raise AdbError(f"Could not connect to {valid_endpoint}: {output}", detail=output)
        return output

    def disconnect(self, endpoint: str | None = None) -> str:
        args = ["disconnect"]
        if endpoint:
            args.append(validate_ip_port(endpoint))
        return self.run(args, timeout=10.0)

    def enable_tcpip(self, serial: str, port: int = 5555) -> str:
        """Legacy USB-initialized TCP/IP mode (`adb -s <serial> tcpip <port>`)."""
        if not serial or not serial.strip():
            raise AdbError("Select a USB-connected device serial before enabling TCP/IP mode.")
        valid_port = validate_port(port, label="TCP/IP port")
        return self.run(["tcpip", str(valid_port)], serial=serial, timeout=15.0)

    def reconnect_with_backoff(
        self,
        endpoint: str,
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """Bounded reconnect helper with exponential backoff and cancellation."""
        valid_endpoint = validate_ip_port(endpoint)
        attempts = max(1, min(int(max_attempts), 5))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if cancel_check and cancel_check():
                raise AdbError("Reconnect cancelled by operator.")
            try:
                return self.connect(valid_endpoint)
            except AdbError as exc:
                last_error = exc
                if attempt < attempts:
                    wait_s = max(0.0, base_delay * (2 ** (attempt - 1)))
                    deadline = time.monotonic() + wait_s
                    while time.monotonic() < deadline:
                        if cancel_check and cancel_check():
                            raise AdbError("Reconnect cancelled by operator.")
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise AdbError(f"Reconnect failed after {attempts} attempt(s): {last_error}")

    def inspect_messaging_app(self, serial: str) -> MessagingAppInfo:
        """Resolve the SENDTO SMS handler and inspect default SMS role / chooser state."""
        activity = self.verify_sms_activity(serial)
        chooser = "ResolverActivity" in activity or "ChooserActivity" in activity
        default_pkg = ""
        try:
            raw_pkg = self.run(["shell", "settings", "get", "secure", "sms_default_application"], serial=serial, timeout=5.0)
            if raw_pkg and raw_pkg.strip() != "null":
                default_pkg = raw_pkg.strip()
        except AdbError:
            pass
        sim_count = 1
        try:
            raw_sims = self.run(["shell", "getprop", "gsm.sim.state"], serial=serial, timeout=5.0)
            if raw_sims:
                parts = [p.strip() for p in raw_sims.split(",") if p.strip()]
                if len(parts) > 1:
                    sim_count = len(parts)
        except AdbError:
            pass
        return MessagingAppInfo(
            resolved_activity=activity,
            default_sms_package=default_pkg,
            chooser_required=chooser,
            sim_count=sim_count,
        )

    def verify_sms_activity(self, serial: str) -> str:
        devices = {device.serial: device for device in self.devices()}
        device = devices.get(serial)
        if device is None:
            raise AdbError("Selected device is no longer listed by ADB.")
        if not device.usable:
            raise AdbError(f"Device is {device.state}; unlock it and authorize this computer in Android.")
        resolved = self.run(
            [
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.SENDTO",
                "-d",
                "smsto:15555550100",
            ],
            serial=serial,
        )
        lines = [line.strip() for line in resolved.splitlines() if line.strip()]
        if not lines or any("No activity found" in line for line in lines) or not any("/" in line for line in lines):
            raise AdbError("No messaging activity can accept an SMS compose intent on this device.")
        return lines[-1]

    def stage_sms(self, serial: str, phone: str, message: str) -> None:
        # The remote Android shell parses a command string even though the host
        # subprocess uses argv. Quote every remote token to prevent shell
        # metacharacters in a consented user's message from becoming commands.
        remote = shlex.join(
            [
                "am",
                "start",
                "-a",
                "android.intent.action.SENDTO",
                "-d",
                f"smsto:{phone}",
                "--es",
                "sms_body",
                message,
            ]
        )
        output = self.run(["shell", remote], serial=serial)
        if "Error:" in output or "Exception" in output:
            raise AdbError("Android rejected the SMS compose intent.", detail=output)


class MockAdbClient(AdbClient):
    """Dry-run mocked ADB transport that simulates composer staging without contacting any device."""

    def __init__(self):
        super().__init__(executable="mock-adb", timeout=5.0)
        self.staged_history: list[tuple[str, str, str]] = []

    def check_availability(self) -> dict[str, str | bool]:
        return {
            "available": True,
            "executable": "mock-adb (Dry-Run Transport)",
            "resolved_path": "internal://mock-adb",
            "version": "MockAdbClient 2.0 (Dry-Run Mode - No external device traffic)",
            "error": "",
        }

    def devices(self) -> list[Device]:
        return [
            Device(
                serial="DRYRUN-PIXEL-01",
                state="device",
                detail="product:pixel model:Pixel_8",
                brand="Google",
                model="Pixel 8 (Dry-Run)",
            )
        ]

    def device_identity(self, serial: str) -> tuple[str, str]:
        return "Google", "Pixel 8 (Dry-Run)"

    def verify_sms_activity(self, serial: str) -> str:
        return "com.google.android.apps.messaging/.ui.conversation.LaunchConversationActivity"

    def stage_sms(self, serial: str, phone: str, message: str) -> None:
        self.staged_history.append((serial, phone, message))
