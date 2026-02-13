#!/usr/bin/env python3
import subprocess
import re
import os
import sys

# Konzervatív, valós workload alapú becsült arányok
ALGO_RATIOS = {
    "lz4": 2.0,
    "lzo": 2.2,
    "zstd": 3.0,
}


def get_zram_stats():
    """
    Returns:
      algorithms (set[str])
      zram_limit_bytes (int)   -- DISKSIZE összesen
      zram_data_bytes (int)    -- DATA összesen
      zram_compr_bytes (int)   -- COMPR összesen
      real_ratio (float)       -- DATA/COMPR (ha COMPR>0)
      cons_ratio (float)       -- algoritmus alapú konzervatív
    """
    try:
        output = subprocess.check_output(
            ["zramctl", "--bytes"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8", "replace")

        if "no devices found" in output.lower():
            return None

        lines = output.strip().split("\n")
        if len(lines) < 2:
            return None

        header = re.split(r"\s+", lines[0].strip())
        idx_algo = header.index("ALGORITHM")
        idx_disksize = header.index("DISKSIZE")
        idx_data = header.index("DATA")
        idx_compr = header.index("COMPR")

        zram_limit = 0
        zram_data = 0
        zram_compr = 0
        algos = set()

        for line in lines[1:]:
            parts = re.split(r"\s+", line.strip())
            if len(parts) <= max(idx_algo, idx_disksize, idx_data, idx_compr):
                continue
            algos.add(parts[idx_algo].lower())
            zram_limit += int(parts[idx_disksize])
            zram_data += int(parts[idx_data])
            zram_compr += int(parts[idx_compr])

        real_ratio = (zram_data / zram_compr) if zram_compr > 0 else 1.0
        cons_ratio = min(ALGO_RATIOS.get(a, 2.0) for a in algos) if algos else 2.0

        return algos, zram_limit, zram_data, zram_compr, real_ratio, cons_ratio

    except Exception:
        return None


def get_memory_stats():
    """Returns (total_ram_bytes, available_ram_bytes)."""
    try:
        out = subprocess.check_output(["free", "-b"], stderr=subprocess.DEVNULL).decode("utf-8", "replace")
        lines = out.strip().split("\n")
        if len(lines) < 2:
            return 0, 0
        parts = re.split(r"\s+", lines[1].strip())
        if len(parts) < 7:
            return 0, 0
        total = int(parts[1])
        available = int(parts[6])
        return total, available
    except Exception:
        return 0, 0


def fmt_mb(x_bytes: float) -> str:
    return f"{x_bytes / (1024*1024):.2f} MB"


def main():
    if os.geteuid() != 0:
        print("Futtasd sudo-val:")
        print(f"  sudo {sys.argv[0]}")
        sys.exit(1)

    mem_total, mem_avail = get_memory_stats()
    z = get_zram_stats()

    if not z:
        print("Nincs aktív ZRAM eszköz.")
        sys.exit(0)

    algos, zram_limit, zram_data, zram_compr, real_ratio, cons_ratio = z

    # HELYES képlet: extra = ZRAM_limit * (ratio - 1)
    extra_cons = zram_limit * max(0.0, (cons_ratio - 1.0))
    extra_opt = zram_limit * max(0.0, (real_ratio - 1.0))

    total_cons = mem_total + extra_cons
    total_opt = mem_total + extra_opt

    free_cons = mem_avail + extra_cons
    free_opt = mem_avail + extra_opt

    print("\n=== ZRAM Memória Elemzés ===\n")
    print(f"Fizikai RAM: {fmt_mb(mem_total)}")
    print(f"Rendszer által elérhető (available): {fmt_mb(mem_avail)}\n")

    print("ZRAM állapot:")
    print(f"  Algoritmus(ok): {', '.join(sorted(algos))}")
    print(f"  Limit (fizikai): {fmt_mb(zram_limit)}")
    print(f"  DATA (logikai adat): {fmt_mb(zram_data)}")
    print(f"  COMPR (valós memória): {fmt_mb(zram_compr)}")
    print(f"  Pillanatnyi valós arány: {real_ratio:.2f}")
    print(f"  Konzervatív becsült arány: {cons_ratio:.2f}\n")

    print("----- Konzervatív elméleti modell (RAM + ZRAM) -----")
    print(f"Teljes elméleti memória: {fmt_mb(total_cons)}")
    print(f"Még eltárolható (szabad kapacitás): {fmt_mb(free_cons)}\n")

    print("----- Optimista elméleti modell (RAM + ZRAM) -----")
    print(f"Teljes elméleti memória: {fmt_mb(total_opt)}")
    print(f"Még eltárolható (szabad kapacitás): {fmt_mb(free_opt)}\n")

    print("Megjegyzés:")
    print("- A képlet: RAM + ZRAM_limit × (ratio − 1), mert a ZRAM fizikai része a RAM-ból jön.")
    print("- Az optimista modell a pillanatnyi arányból számol, telítettségnél jellemzően romlik az arány.")


if __name__ == "__main__":
    main()
