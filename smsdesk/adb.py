"""Safe ADB transport. Commands use argument arrays, bounded timeouts and no shell."""

from __future__ import annotations

import subprocess
import shlex
from dataclasses import dataclass

from .validation import validate_ip_port


class AdbError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, detail: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.detail = detail[:500]


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    detail: str = ""
    brand: str = ""
    model: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def friendly_name(self) -> str:
        """Readable hardware identity, with ADB's model/product as a fallback."""
        brand = _humanize_brand(self.brand)
        model = _humanize_model(self.model)
        if brand and model and not model.casefold().startswith(brand.casefold()):
            return f"{brand} {model}"
        if brand or model:
            return brand or model
        properties = dict(
            part.split(":", 1) for part in self.detail.split()
            if ":" in part
        )
        return (_humanize_model(properties.get("model", "")) or
                _humanize_model(properties.get("product", "")) or "Android device")


def _humanize_model(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).strip()


def _humanize_brand(value: str) -> str:
    brand = _humanize_model(value)
    known = {
        "google": "Google", "samsung": "Samsung", "oneplus": "OnePlus",
        "motorola": "Motorola", "xiaomi": "Xiaomi", "oppo": "OPPO",
        "vivo": "vivo", "huawei": "HUAWEI", "honor": "HONOR",
        "sony": "Sony", "asus": "ASUS", "lenovo": "Lenovo",
    }
    return known.get(brand.casefold(), brand.capitalize())


class AdbClient:
    def __init__(self, executable: str = "adb", timeout: float = 15.0):
        self.executable = executable
        self.timeout = timeout

    def run(self, args: list[str], *, serial: str | None = None, timeout: float | None = None) -> str:
        command = [self.executable]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout or self.timeout,
                                    check=False, shell=False, encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise AdbError("ADB executable was not found. Configure its path or install Android platform-tools.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {timeout or self.timeout:g} seconds.") from exc
        except OSError as exc:
            raise AdbError(f"Could not start ADB: {exc}") from exc
        output = (result.stdout or "").strip()
        if result.returncode:
            # ADB diagnostics sometimes include user data; caller logs only the short error.
            detail = (result.stderr or output).strip()
            raise AdbError(f"ADB command failed (exit {result.returncode}).", returncode=result.returncode, detail=detail)
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

    def connect(self, endpoint: str) -> str:
        return self.run(["connect", validate_ip_port(endpoint)], timeout=max(20.0, self.timeout))

    def verify_sms_activity(self, serial: str) -> str:
        devices = {device.serial: device for device in self.devices()}
        device = devices.get(serial)
        if device is None:
            raise AdbError("Selected device is no longer listed by ADB.")
        if not device.usable:
            raise AdbError(f"Device is {device.state}; unlock it and authorize this computer in Android.")
        resolved = self.run(["shell", "cmd", "package", "resolve-activity", "--brief",
                             "-a", "android.intent.action.SENDTO", "-d", "smsto:15555550100"], serial=serial)
        lines = [line.strip() for line in resolved.splitlines() if line.strip()]
        if not lines or any("No activity found" in line for line in lines) or not any("/" in line for line in lines):
            raise AdbError("No messaging activity can accept an SMS compose intent on this device.")
        return lines[-1]

    def stage_sms(self, serial: str, phone: str, message: str) -> None:
        # The remote Android shell parses a command string even though the host
        # subprocess uses argv. Quote every remote token to prevent shell
        # metacharacters in a consented user's message from becoming commands.
        remote = shlex.join(["am", "start", "-a", "android.intent.action.SENDTO",
                             "-d", f"smsto:{phone}", "--es", "sms_body", message])
        output = self.run(["shell", remote], serial=serial)
        if "Error:" in output or "Exception" in output:
            raise AdbError("Android rejected the SMS compose intent.", detail=output)
