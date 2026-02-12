#!/usr/bin/env python3
import curses
import os
import re
import subprocess
import sys
import time
from collections import deque
from typing import Dict, Optional, Tuple


# --- Tuning ---
DEFAULT_REFRESH = 1.0
MIN_REFRESH = 0.2
MAX_REFRESH = 5.0

SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Konzervatív, workload-alapú becslések
ALGO_RATIOS = {
    "lz4": 2.0,
    "lzo": 2.2,
    "zstd": 3.0,
}


# ---------- low-level helpers ----------

def sh(cmd) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8", "replace")


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def b2mb(x: int) -> float:
    return x / (1024 * 1024)


def safe_addstr(stdscr, y: int, x: int, s: str, attr: int = 0):
    """Write clipped string safely (avoid curses error on small terminals)."""
    try:
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        s = s[: max(0, w - x - 1)]
        if s:
            stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass


def color_for_ratio(r: float) -> int:
    if r < 0.60:
        return 1  # green
    if r < 0.85:
        return 2  # yellow
    return 3      # red


def draw_bar(stdscr, y: int, x: int, width: int, ratio: float, label: str = ""):
    ratio = clamp(ratio, 0.0, 1.0)
    inner = max(0, width - 2)
    filled = int(inner * ratio)

    safe_addstr(stdscr, y, x, "[")
    for i in range(inner):
        ch = "█" if i < filled else " "
        attr = curses.color_pair(color_for_ratio(ratio)) if i < filled else 0
        safe_addstr(stdscr, y, x + 1 + i, ch, attr)
    safe_addstr(stdscr, y, x + 1 + inner, "]")

    if label:
        safe_addstr(stdscr, y, x + width + 1, label)


def sparkline(values, width: int) -> str:
    """values: list of floats in [0..1]. returns string length == width."""
    if width <= 0:
        return ""
    if not values:
        return " " * width

    vals = values[-width:]
    out = []
    for v in vals:
        v = clamp(v, 0.0, 1.0)
        idx = int(round(v * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[idx])

    if len(out) < width:
        out = [" "] * (width - len(out)) + out
    return "".join(out)


def trim_deque(dq: deque, maxlen: int):
    """Manually trim deque to maxlen (we keep maxlen dynamic)."""
    while len(dq) > maxlen:
        dq.popleft()


# ---------- data collection ----------

def get_mem_free_b() -> Tuple[int, int]:
    """Returns (total_ram_bytes, available_ram_bytes)."""
    out = sh(["free", "-b"])
    lines = out.strip().split("\n")
    if len(lines) < 2:
        return 0, 0
    parts = re.split(r"\s+", lines[1].strip())
    if len(parts) < 7:
        return 0, 0
    total = int(parts[1])
    avail = int(parts[6])
    return total, avail


def get_swaps_from_proc() -> Tuple[int, int, int, int]:
    """
    /proc/swaps:
      zram_swap_total, zram_swap_used, other_swap_total, other_swap_used (bytes)
    Size/Used are KiB.
    """
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
    except Exception:
        return 0, 0, 0, 0

    if len(lines) < 2:
        return 0, 0, 0, 0

    z_total = z_used = o_total = o_used = 0
    for line in lines[1:]:
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 5:
            continue
        name = parts[0]
        size_kib = int(parts[2])
        used_kib = int(parts[3])
        size_b = size_kib * 1024
        used_b = used_kib * 1024
        if "/dev/zram" in name:
            z_total += size_b
            z_used += used_b
        else:
            o_total += size_b
            o_used += used_b
    return z_total, z_used, o_total, o_used


def get_zramctl_bytes() -> Optional[Dict]:
    """Sum across zram devices: disksize, data, compr (bytes) + algos + ratios."""
    out = sh(["zramctl", "--bytes"])
    if "no devices found" in out.lower():
        return None
    lines = out.strip().split("\n")
    if len(lines) < 2:
        return None

    header = re.split(r"\s+", lines[0].strip())
    try:
        idx_algo = header.index("ALGORITHM")
        idx_disksize = header.index("DISKSIZE")
        idx_data = header.index("DATA")
        idx_compr = header.index("COMPR")
    except ValueError:
        return None

    disksize = data = compr = 0
    algos = set()

    for line in lines[1:]:
        parts = re.split(r"\s+", line.strip())
        if len(parts) <= max(idx_algo, idx_disksize, idx_data, idx_compr):
            continue
        algos.add(parts[idx_algo].lower())
        disksize += int(parts[idx_disksize])
        data += int(parts[idx_data])
        compr += int(parts[idx_compr])

    real_ratio = (data / compr) if compr > 0 else 1.0
    cons_ratio = min(ALGO_RATIOS.get(a, 2.0) for a in algos) if algos else 2.0

    return {
        "algos": algos,
        "disksize": disksize,
        "data": data,
        "compr": compr,
        "real_ratio": real_ratio,
        "cons_ratio": cons_ratio,
    }


# ---------- risk model ----------

def oom_risk(total_ram: int, avail_ram: int,
             zram_phys_used: int, zram_phys_limit: int,
             other_swap_total: int, other_swap_used: int) -> Tuple[str, int, str]:
    """
    Returns (label, color_pair, explanation)
    """
    if total_ram <= 0:
        return ("UNKNOWN", 2, "no RAM data")

    ram_free_ratio = avail_ram / total_ram
    zram_util = (zram_phys_used / zram_phys_limit) if zram_phys_limit > 0 else 0.0

    other_free = other_swap_total - other_swap_used
    other_free_ratio = (other_free / other_swap_total) if other_swap_total > 0 else 1.0

    if ram_free_ratio < 0.06 and zram_util > 0.90 and (other_swap_total == 0 or other_free_ratio < 0.15):
        return ("HIGH", 3, "low RAM, zram near full, swap low")
    if ram_free_ratio < 0.10 and (zram_util > 0.88 or (other_swap_total > 0 and other_free_ratio < 0.12)):
        return ("WARN", 2, "ram getting low; swap/zram pressure")
    if ram_free_ratio < 0.15 and (zram_util > 0.85 or (other_swap_total > 0 and other_free_ratio < 0.20)):
        return ("OK", 2, "watching (pressure rising)")
    return ("SAFE", 1, "healthy headroom")


# ---------- TUI ----------

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    init_colors()

    refresh = DEFAULT_REFRESH

    # history buffers (dynamic maxlen handled manually)
    hist_ram = deque()
    hist_zram = deque()
    hist_other_swap = deque()
    hist_free_cons = deque()
    hist_free_opt = deque()

    last_tick = time.time()

    while True:
        now = time.time()
        if now - last_tick < refresh:
            key = stdscr.getch()
            if key == ord('q'):
                break
            elif key in (ord('+'), ord('=')):
                refresh = max(MIN_REFRESH, refresh - 0.1)
            elif key in (ord('-'), ord('_')):
                refresh = min(MAX_REFRESH, refresh + 0.1)
            time.sleep(0.02)
            continue
        last_tick = now

        stdscr.erase()
        h, w = stdscr.getmaxyx()

        # calculate graph width NOW (depends on terminal width)
        graph_w = max(10, w - 18)
        # dynamic history length: ~6 screens worth, min 60
        dyn_hist_len = max(60, graph_w * 6)

        try:
            total_ram, avail_ram = get_mem_free_b()
            zram = get_zramctl_bytes()
            _zswap_total, _zswap_used, oswap_total, oswap_used = get_swaps_from_proc()
        except Exception:
            safe_addstr(stdscr, 0, 0, "Adatlekérési hiba.", curses.color_pair(3))
            stdscr.refresh()
            continue

        if not zram:
            safe_addstr(stdscr, 0, 2, "ZRAM Monitor", curses.A_BOLD | curses.color_pair(4))
            safe_addstr(stdscr, 2, 2, "Nincs aktív ZRAM eszköz.", curses.color_pair(2))
            safe_addstr(stdscr, h - 1, 2, "Kilépés: q", curses.color_pair(4))
            stdscr.refresh()
            continue

        used_ram = max(0, total_ram - avail_ram)
        ram_used_ratio = (used_ram / total_ram) if total_ram > 0 else 0.0

        zram_phys_limit = zram["disksize"]
        zram_phys_used = zram["compr"]
        zram_phys_ratio = (zram_phys_used / zram_phys_limit) if zram_phys_limit > 0 else 0.0

        other_swap_ratio = (oswap_used / oswap_total) if oswap_total > 0 else 0.0
        oswap_free = max(0, oswap_total - oswap_used)

        cons_ratio = float(zram["cons_ratio"])
        real_ratio = float(zram["real_ratio"])

        cons_zram_capacity = zram_phys_limit * cons_ratio
        opt_zram_capacity = zram_phys_limit * real_ratio

        free_cons_bytes = avail_ram + cons_zram_capacity + oswap_free
        free_opt_bytes = avail_ram + opt_zram_capacity + oswap_free

        total_cons_bytes = total_ram + cons_zram_capacity + oswap_total
        total_opt_bytes = total_ram + opt_zram_capacity + oswap_total

        free_cons_ratio = (free_cons_bytes / total_cons_bytes) if total_cons_bytes > 0 else 0.0
        free_opt_ratio = (free_opt_bytes / total_opt_bytes) if total_opt_bytes > 0 else 0.0

        gain_bytes = max(0, zram["data"] - zram["compr"])
        efficiency = (zram["data"] / zram_phys_limit) if zram_phys_limit > 0 else 0.0

        risk_label, risk_color, risk_expl = oom_risk(
            total_ram, avail_ram,
            zram_phys_used, zram_phys_limit,
            oswap_total, oswap_used
        )

        # Append history (then trim dynamically for resize)
        hist_ram.append(clamp(ram_used_ratio, 0.0, 1.0))
        hist_zram.append(clamp(zram_phys_ratio, 0.0, 1.0))
        hist_other_swap.append(clamp(other_swap_ratio, 0.0, 1.0))
        hist_free_cons.append(clamp(free_cons_ratio, 0.0, 1.0))
        hist_free_opt.append(clamp(free_opt_ratio, 0.0, 1.0))

        trim_deque(hist_ram, dyn_hist_len)
        trim_deque(hist_zram, dyn_hist_len)
        trim_deque(hist_other_swap, dyn_hist_len)
        trim_deque(hist_free_cons, dyn_hist_len)
        trim_deque(hist_free_opt, dyn_hist_len)

        # ---------- layout ----------
        safe_addstr(stdscr, 0, 2, "ZRAM Monitor", curses.A_BOLD | curses.color_pair(4))
        safe_addstr(stdscr, 0, w - 26, f"refresh: {refresh:.1f}s  (+/-)  q", curses.color_pair(4))

        row = 2
        bar_w = max(10, w - 22)
        mb = 1024 * 1024

        safe_addstr(stdscr, row, 2, "RAM used", curses.A_BOLD)
        draw_bar(stdscr, row + 1, 2, bar_w, ram_used_ratio,
                 f"{b2mb(used_ram):.0f}/{b2mb(total_ram):.0f} MB  avail {b2mb(avail_ram):.0f} MB")
        row += 3

        algos = ", ".join(sorted(zram["algos"])) if zram["algos"] else "?"
        safe_addstr(stdscr, row, 2, f"ZRAM phys ({algos})", curses.A_BOLD)
        draw_bar(stdscr, row + 1, 2, bar_w, zram_phys_ratio,
                 f"COMPR {b2mb(zram['compr']):.0f}/{b2mb(zram_phys_limit):.0f} MB  DATA {b2mb(zram['data']):.0f} MB")
        row += 3

        safe_addstr(stdscr, row, 2, "Other swap (non-zram)", curses.A_BOLD)
        if oswap_total > 0:
            draw_bar(stdscr, row + 1, 2, bar_w, other_swap_ratio,
                     f"used {b2mb(oswap_used):.0f}/{b2mb(oswap_total):.0f} MB  free {b2mb(oswap_free):.0f} MB")
        else:
            draw_bar(stdscr, row + 1, 2, bar_w, 0.0, "none")
        row += 3

        safe_addstr(stdscr, row, 2, "Compression", curses.A_BOLD)
        safe_addstr(stdscr, row + 1, 4,
                    f"real ratio: {real_ratio:.2f}    conservative: {cons_ratio:.2f}",
                    curses.color_pair(5))
        safe_addstr(stdscr, row + 2, 4,
                    f"gain: {b2mb(gain_bytes):.0f} MB    efficiency(DATA/DISKSIZE): {efficiency:.2f}x",
                    curses.color_pair(5))
        row += 4

        safe_addstr(stdscr, row, 2, "OOM risk", curses.A_BOLD)
        safe_addstr(stdscr, row + 1, 4, f"{risk_label}", curses.A_BOLD | curses.color_pair(risk_color))
        safe_addstr(stdscr, row + 1, 12, f"({risk_expl})", curses.color_pair(risk_color))
        row += 3

        safe_addstr(stdscr, row, 2, "Effective capacity (free)", curses.A_BOLD)
        safe_addstr(stdscr, row + 1, 4,
                    f"Conservative free: {b2mb(int(free_cons_bytes)):.0f} MB  (of {b2mb(int(total_cons_bytes)):.0f} MB)",
                    curses.color_pair(1))
        safe_addstr(stdscr, row + 2, 4,
                    f"Optimistic  free: {b2mb(int(free_opt_bytes)):.0f} MB  (of {b2mb(int(total_opt_bytes)):.0f} MB)",
                    curses.color_pair(2))
        row += 4

        safe_addstr(stdscr, row, 2, f"History (auto length: {dyn_hist_len} samples)", curses.A_BOLD)

        # RAM used%
        s = sparkline(list(hist_ram), graph_w)
        safe_addstr(stdscr, row + 1, 2, "RAM%     ")
        safe_addstr(stdscr, row + 1, 11, s, curses.color_pair(color_for_ratio(ram_used_ratio)))

        # ZRAM phys%
        s = sparkline(list(hist_zram), graph_w)
        safe_addstr(stdscr, row + 2, 2, "ZRAM%phys")
        safe_addstr(stdscr, row + 2, 11, s, curses.color_pair(color_for_ratio(zram_phys_ratio)))

        # Other swap%
        s = sparkline(list(hist_other_swap), graph_w)
        safe_addstr(stdscr, row + 3, 2, "SWAP%oth ")
        safe_addstr(stdscr, row + 3, 11, s, curses.color_pair(color_for_ratio(other_swap_ratio)))

        # Free% (cons/opt) — invert for color (low free = red)
        s1 = sparkline(list(hist_free_cons), graph_w)
        s2 = sparkline(list(hist_free_opt), graph_w)
        safe_addstr(stdscr, row + 4, 2, "FREE%cons")
        safe_addstr(stdscr, row + 4, 11, s1, curses.color_pair(color_for_ratio(1.0 - free_cons_ratio)))
        safe_addstr(stdscr, row + 5, 2, "FREE%opt ")
        safe_addstr(stdscr, row + 5, 11, s2, curses.color_pair(color_for_ratio(1.0 - free_opt_ratio)))

        safe_addstr(stdscr, h - 1, 2, "q=quit  +=faster  -=slower", curses.color_pair(4))

        stdscr.refresh()


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Futtasd sudo-val:")
        print(f"  sudo {sys.argv[0]}")
        sys.exit(1)

    curses.wrapper(main)

