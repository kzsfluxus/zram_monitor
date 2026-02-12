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
    try:
        output = subprocess.check_output(
            ["zramctl", "--bytes"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8")

        if "no devices found" in output.lower():
            return None

        lines = output.strip().split("\n")
        header = re.split(r"\s+", lines[0].strip())

        idx_algo = header.index("ALGORITHM")
        idx_disksize = header.index("DISKSIZE")
        idx_data = header.index("DATA")
        idx_compr = header.index("COMPR")

        total_disksize = 0
        total_data = 0
        total_compr = 0
        algorithms = set()

        for line in lines[1:]:
            parts = re.split(r"\s+", line.strip())
            if len(parts) <= max(idx_algo, idx_disksize, idx_data, idx_compr):
                continue

            algorithms.add(parts[idx_algo].lower())
            total_disksize += int(parts[idx_disksize])
            total_data += int(parts[idx_data])
            total_compr += int(parts[idx_compr])

        real_ratio = (total_data / total_compr) if total_compr > 0 else 1.0

        return {
            "algorithms": algorithms,
            "disksize": total_disksize,
            "data": total_data,
            "compr": total_compr,
            "real_ratio": real_ratio
        }

    except Exception:
        return None


def get_memory_stats():
    try:
        output = subprocess.check_output(
            ["free", "-b"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8")

        lines = output.strip().split("\n")
        mem_line = lines[1]
        parts = re.split(r"\s+", mem_line.strip())

        total = int(parts[1])
        available = int(parts[6])

        return total, available

    except Exception:
        return 0, 0


def get_conservative_ratio(algorithms):
    ratios = []
    for algo in algorithms:
        ratios.append(ALGO_RATIOS.get(algo, 2.0))

    return min(ratios) if ratios else 2.0


def main():
    if os.geteuid() != 0:
        print("Futtasd sudo-val:")
        print(f"  sudo {sys.argv[0]}")
        sys.exit(1)

    zram = get_zram_stats()
    total_ram, available_ram = get_memory_stats()

    if not zram:
        print("Nincs aktív ZRAM eszköz.")
        sys.exit(0)

    conservative_ratio = get_conservative_ratio(zram["algorithms"])
    real_ratio = zram["real_ratio"]

    # --- Konzervatív számítás ---
    conservative_zram_capacity = zram["disksize"] * conservative_ratio
    conservative_total = total_ram + conservative_zram_capacity
    conservative_free = available_ram + conservative_zram_capacity

    # --- Aktuális arány alapú (optimista) ---
    optimistic_zram_capacity = zram["disksize"] * real_ratio
    optimistic_total = total_ram + optimistic_zram_capacity
    optimistic_free = available_ram + optimistic_zram_capacity

    mb = 1024 * 1024

    print("\n==============================")
    print("      ZRAM MEMÓRIA ANALÍZIS")
    print("==============================\n")

    print(f"Fizikai RAM: {total_ram / mb:.2f} MB")
    print(f"Rendszer által elérhető (available): {available_ram / mb:.2f} MB\n")

    print("ZRAM állapot:")
    print(f"  Algoritmus(ok): {', '.join(zram['algorithms'])}")
    print(f"  Limit (fizikai): {zram['disksize'] / mb:.2f} MB")
    print(f"  DATA (logikai adat): {zram['data'] / mb:.2f} MB")
    print(f"  COMPR (valós memória): {zram['compr'] / mb:.2f} MB")
    print(f"  Pillanatnyi valós arány: {real_ratio:.2f}")
    print(f"  Konzervatív becsült arány: {conservative_ratio:.2f}\n")

    print("----- Konzervatív elméleti modell -----")
    print(f"Teljes elméleti memória: {conservative_total / mb:.2f} MB")
    print(f"Még eltárolható (szabad kapacitás): {conservative_free / mb:.2f} MB\n")

    print("----- Aktuális arány alapú (optimista) -----")
    print(f"Teljes elméleti memória: {optimistic_total / mb:.2f} MB")
    print(f"Még eltárolható (szabad kapacitás): {optimistic_free / mb:.2f} MB\n")

    print("Megjegyzés:")
    print("- A konzervatív modell algoritmus-alapú, stabil becslés.")
    print("- Az aktuális modell a jelenlegi tömörítési arányból számol.")
    print("- Telítettségnél a valós arány jellemzően romlik.")
    print("- A valós rendszer OOM viselkedése ettől eltérhet.\n")


if __name__ == "__main__":
    main()
