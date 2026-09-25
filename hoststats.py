# SPDX-License-Identifier: GPL-3.0-or-later
"""
Host statistics for an AMD BC-250 (Cyan Skillfish APU) running SteamOS.

Everything is read straight from sysfs / procfs, so no root and no vendor tools
are needed. Each reader degrades to None (or an empty list) when the kernel
does not expose a value, and the display shows "--" for it.

Notes specific to the BC-250 kernel:
  * amdgpu's `gpu_busy_percent` returns "Operation not supported", so GPU
    utilisation is derived from DRM fdinfo engine-time counters instead (the
    same source nvtop uses).
  * amdgpu's sclk reading is unreliable on this platform, so it is not shown.
  * The compute-unit count comes from the KFD topology, which reports the
    number of CUs that are actually enabled.
"""

import glob
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import psutil

SYS_CPU = "/sys/devices/system/cpu"
HWMON = "/sys/class/hwmon"
KFD_NODES = "/sys/class/kfd/kfd/topology/nodes"

# Friendly names for hwmon drivers; anything else is shown under its driver name.
HWMON_NAMES = {
    "k10temp": "CPU",
    "amdgpu": "GPU",
    "nvme": "NVMe",
    "mt7921_phy0": "WiFi",
    "drivetemp": "SATA",
}

# Filesystems that count as "disks" for the usage panel.
DISK_FSTYPES = {"ext4", "ext3", "btrfs", "xfs", "f2fs", "ntfs", "ntfs3", "exfat", "vfat"}
DISK_MIN_BYTES = 2 * 1024 ** 3   # hides the small /var, /boot and EFI partitions
# SteamOS bind-mounts several of these from the home partition; showing them would
# hide the real mount (/home) behind a duplicate of the same device.
DISK_SKIP_PREFIXES = ("/usr", "/opt", "/var", "/boot", "/etc", "/srv", "/root", "/nix", "/efi", "/esp")


# ── Small readers ──────────────────────────────────────────────────────────────

def _read(path: str) -> Optional[str]:
    """Return the stripped text of a sysfs/procfs file, or None if unreadable."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _parse_cpulist(text: str) -> List[int]:
    """Expand a kernel CPU list such as '0-3,8' into [0, 1, 2, 3, 8]."""
    cpus: List[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        elif part:
            cpus.append(int(part))
    return cpus


# ── CPU ────────────────────────────────────────────────────────────────────────

def cpu_topology() -> Tuple[int, int]:
    """Return (physical_cores, threads) counting only CPUs that are online."""
    online = _read(f"{SYS_CPU}/online")
    if online is None:
        return psutil.cpu_count(logical=False) or 0, psutil.cpu_count(logical=True) or 0
    cpus = _parse_cpulist(online)
    cores = {
        (_read(f"{SYS_CPU}/cpu{n}/topology/physical_package_id"),
         _read(f"{SYS_CPU}/cpu{n}/topology/core_id"))
        for n in cpus
    }
    return len(cores), len(cpus)


# ── GPU ────────────────────────────────────────────────────────────────────────

def find_amdgpu() -> Optional[str]:
    """Return the sysfs device directory of the first amdgpu card, or None."""
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        driver = os.path.basename(os.path.realpath(f"{dev}/driver"))
        if driver == "amdgpu":
            return dev
    return None


def cu_count() -> Optional[int]:
    """Number of enabled compute units, from the KFD topology (simd_count / simd_per_cu)."""
    for node in sorted(glob.glob(f"{KFD_NODES}/*")):
        props = {}
        for line in (_read(f"{node}/properties") or "").splitlines():
            key, _, val = line.partition(" ")
            props[key] = val
        try:
            simds, per_cu = int(props.get("simd_count", 0)), int(props.get("simd_per_cu", 0))
        except ValueError:
            continue
        if simds and per_cu:          # node 0 is the CPU and reports zeros
            return simds // per_cu
    return None


class GpuBusy:
    """
    GPU utilisation from DRM fdinfo, for kernels where gpu_busy_percent is unsupported.

    Every process with the GPU open exposes cumulative per-engine busy time
    (`drm-engine-gfx: <ns>`) in /proc/<pid>/fdinfo/<fd>. Summing the growth of
    those counters across clients and dividing by wall time gives utilisation.

    Only processes this user can read are counted: games and the compositor run
    as this user on SteamOS, but a root-owned GPU client would be invisible.
    """

    ENGINES = ("drm-engine-gfx", "drm-engine-compute")
    RESCAN_SECS = 10   # how often to look for new GPU clients (a full /proc walk)

    def __init__(self, pdev: str):
        self.pdev = pdev                        # PCI address, e.g. 0000:01:00.0
        self.paths: List[str] = []
        self.last_scan = 0.0
        self.prev: Dict[str, Dict[str, int]] = {}   # client id -> engine -> ns
        self.prev_t: Optional[float] = None

    def _parse(self, path: str) -> Optional[Tuple[str, Dict[str, int]]]:
        """Return (client_id, {engine: ns}) for an amdgpu fdinfo file on our device."""
        try:
            with open(path) as f:
                txt = f.read()
        except OSError:
            return None
        if not re.search(r"^drm-driver:\s*amdgpu", txt, re.M):
            return None
        pdev = re.search(r"^drm-pdev:\s*(\S+)", txt, re.M)
        if pdev and pdev.group(1) != self.pdev:
            return None
        cid = re.search(r"^drm-client-id:\s*(\d+)", txt, re.M)
        engines = {k: int(v) for k, v in re.findall(r"^(drm-engine-[a-z_0-9]+):\s*(\d+) ns", txt, re.M)}
        return (cid.group(1) if cid else path), engines

    def _scan(self) -> None:
        self.paths = [p for p in glob.glob("/proc/[0-9]*/fdinfo/*") if self._parse(p)]
        self.last_scan = time.monotonic()

    def sample(self) -> Optional[float]:
        """Return utilisation 0-100 since the previous call, or None if unknown."""
        now = time.monotonic()
        if now - self.last_scan > self.RESCAN_SECS or not self.paths:
            self._scan()

        cur: Dict[str, Dict[str, int]] = {}
        for path in self.paths:
            parsed = self._parse(path)
            if parsed is None:
                self.last_scan = 0.0            # a client went away: rescan next time
                continue
            cur[parsed[0]] = parsed[1]          # one entry per client, even if it has several fds

        prev, prev_t = self.prev, self.prev_t
        self.prev, self.prev_t = cur, now
        if prev_t is None or not cur:
            return None

        elapsed_ns = max(now - prev_t, 0.001) * 1e9
        busy = {e: 0 for e in self.ENGINES}
        for cid, engines in cur.items():
            if cid not in prev:                 # first sight of a client: no baseline yet
                continue
            for e in self.ENGINES:
                busy[e] += max(0, engines.get(e, 0) - prev[cid].get(e, 0))
        return min(100.0, max(busy.values()) / elapsed_ns * 100)


# ── Temperatures ───────────────────────────────────────────────────────────────

@dataclass
class Temp:
    name: str        # short display name, e.g. "CPU"
    celsius: float


def read_temps() -> List[Temp]:
    """
    Every temperature the kernel exposes through hwmon, with friendly names.

    A driver with several sensors gets its sensor label appended, e.g. "NVMe Sensor 1".
    """
    temps: List[Temp] = []
    for hw in sorted(glob.glob(f"{HWMON}/hwmon*")):
        driver = _read(f"{hw}/name") or os.path.basename(hw)
        inputs = sorted(glob.glob(f"{hw}/temp*_input"), key=lambda p: int(re.search(r"temp(\d+)_", p).group(1)))
        for path in inputs:
            raw = _read_int(path)
            if raw is None:
                continue
            name = HWMON_NAMES.get(driver, driver)
            if len(inputs) > 1:
                idx = re.search(r"temp(\d+)_", path).group(1)
                name += " " + (_read(f"{hw}/temp{idx}_label") or f"#{idx}")
            temps.append(Temp(name, raw / 1000.0))
    return temps


# ── Storage ────────────────────────────────────────────────────────────────────

@dataclass
class Disk:
    label: str
    mount: str
    percent: float
    used: int
    total: int


def _disk_label(mount: str) -> str:
    if mount == "/":
        return "ROOT"
    return os.path.basename(mount.rstrip("/")).upper()[:8] or mount


def find_mounts(limit: int = 3) -> List[str]:
    """Auto-detect mount points worth showing: one per physical filesystem, big enough to matter."""
    seen, mounts = set(), []
    for part in sorted(psutil.disk_partitions(all=False), key=lambda p: len(p.mountpoint)):
        if part.fstype not in DISK_FSTYPES or part.device in seen:
            continue                            # skip pseudo fs and btrfs subvolume duplicates
        if any(part.mountpoint == p or part.mountpoint.startswith(p + "/") for p in DISK_SKIP_PREFIXES):
            continue
        try:
            if psutil.disk_usage(part.mountpoint).total < DISK_MIN_BYTES:
                continue
        except OSError:
            continue
        seen.add(part.device)
        mounts.append(part.mountpoint)
    return mounts[:limit]


def read_disks(mounts: List[str]) -> List[Disk]:
    disks = []
    for m in mounts:
        try:
            u = psutil.disk_usage(m)
        except OSError:
            continue
        disks.append(Disk(_disk_label(m), m, u.percent, u.used, u.total))
    return disks


# ── Network ────────────────────────────────────────────────────────────────────

@dataclass
class Wifi:
    iface: Optional[str] = None      # None: no wireless adapter present
    connected: bool = False
    ssid: Optional[str] = None
    signal_dbm: Optional[int] = None
    ip: Optional[str] = None         # this host's IPv4 address (the one used for the default route)


def find_wifi_iface() -> Optional[str]:
    """First wireless network interface, e.g. wlan0."""
    for path in sorted(glob.glob("/sys/class/net/*/wireless")):
        return os.path.basename(os.path.dirname(path))
    return None


def iw_link(iface: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """Return (connected, ssid, signal_dbm) from `iw dev <iface> link`."""
    try:
        out = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False, None, None
    if not out.strip() or "Not connected" in out:
        return False, None, None
    ssid = re.search(r"^\s*SSID:\s*(.*)$", out, re.M)
    sig = re.search(r"^\s*signal:\s*(-?\d+)", out, re.M)
    return True, (ssid.group(1) if ssid else None), (int(sig.group(1)) if sig else None)


def host_ip() -> Optional[str]:
    """
    This host's IPv4 address. Asks the kernel which address it would use to reach
    an outside host (a UDP connect sends nothing), then falls back to the first
    non-loopback IPv4 on any interface.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))     # TEST-NET-1: never routed, only used for the lookup
            return s.getsockname()[0]
    except OSError:
        pass
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                return a.address
    return None


# ── Snapshot ───────────────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    cpu_pct: float = 0.0
    cores: int = 0
    threads: int = 0
    mem_pct: float = 0.0
    mem_used: int = 0
    mem_total: int = 0
    gpu_pct: Optional[float] = None
    cu: Optional[int] = None
    gpu_power_w: Optional[float] = None
    vram_used: Optional[int] = None
    vram_total: Optional[int] = None
    gtt_used: Optional[int] = None
    gtt_total: Optional[int] = None
    temps: List[Temp] = field(default_factory=list)
    disks: List[Disk] = field(default_factory=list)
    wifi: Wifi = field(default_factory=Wifi)


class HostStats:
    """Owns the stateful readers (CPU delta, GPU fdinfo delta) and produces Snapshots."""

    def __init__(self, disk_mounts: Optional[List[str]] = None):
        self.gpu_dev = find_amdgpu()
        self.gpu_busy = GpuBusy(os.path.basename(os.path.realpath(self.gpu_dev))) if self.gpu_dev else None
        self.cu = cu_count()
        self.mounts = disk_mounts or find_mounts()
        self.wifi_iface = find_wifi_iface()
        self._link = (False, None, None)        # cached iw result: it runs a subprocess, so poll it slowly
        self._link_t = 0.0
        psutil.cpu_percent(interval=None)       # prime: the first call has no baseline

    def _gpu_power(self) -> Optional[float]:
        """Package power (PPT) in watts from the amdgpu hwmon, if present."""
        for hw in glob.glob(f"{self.gpu_dev}/hwmon/hwmon*"):
            for name in ("power1_average", "power1_input"):
                uw = _read_int(f"{hw}/{name}")
                if uw:
                    return uw / 1e6
        return None

    LINK_POLL_SECS = 5

    def _wifi(self) -> Wifi:
        w = Wifi(iface=self.wifi_iface, ip=host_ip())
        if self.wifi_iface:
            now = time.monotonic()
            if now - self._link_t > self.LINK_POLL_SECS:
                self._link, self._link_t = iw_link(self.wifi_iface), now
            w.connected, w.ssid, w.signal_dbm = self._link
        return w

    def collect(self) -> Snapshot:
        s = Snapshot()
        s.cpu_pct = psutil.cpu_percent(interval=None)
        s.cores, s.threads = cpu_topology()

        vm = psutil.virtual_memory()
        s.mem_pct, s.mem_used, s.mem_total = vm.percent, vm.total - vm.available, vm.total

        if self.gpu_dev:
            s.gpu_pct = self.gpu_busy.sample()
            s.cu = self.cu
            s.gpu_power_w = self._gpu_power()
            s.vram_used = _read_int(f"{self.gpu_dev}/mem_info_vram_used")
            s.vram_total = _read_int(f"{self.gpu_dev}/mem_info_vram_total")
            s.gtt_used = _read_int(f"{self.gpu_dev}/mem_info_gtt_used")
            s.gtt_total = _read_int(f"{self.gpu_dev}/mem_info_gtt_total")

        s.temps = read_temps()
        s.disks = read_disks(self.mounts)
        s.wifi = self._wifi()
        return s
