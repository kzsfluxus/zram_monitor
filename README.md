# ZRAM Monitor

Linuxos eszközkészlet a ZRAM és a rendszer memóriaállapotának elemzésére.

A projekt két Python fájlt tartalmaz:

- `zram_free.py` – egyszeri, konzolos lekérdezés részletes számításokkal
- `zram_monitor.py` – színes, curses-alapú TUI monitor history grafikonokkal

Cél: pontosabb képet adni arról, hogy mennyi adatállapot férhet el a rendszerben  
**RAM + ZRAM + esetleges swap partíció / swap fájl** kombinációval.

---

# Funkciók

## zram_free.py

Egyszeri riport, amely:

- Felismeri a ZRAM algoritmust (`zramctl`)
- Kiszámolja:
  - Konzervatív (algoritmus alapú) elméleti kapacitást
  - Optimista (aktuális DATA/COMPR arány alapú) kapacitást
- Összegez több ZRAM eszközt

### Konzervatív arányok

| Algoritmus | Becsült arány |
|------------|--------------|
| lz4        | 2.0x |
| lzo        | 2.2x |
| zstd       | 3.0x |

Ez stabil, tervezésre alkalmas becslés.

---

## zram_monitor.py (TUI)

Színes terminálos dashboard (pure curses, nincs külső dependency).

### Megjelenített adatok

- RAM használat
- ZRAM fizikai kihasználtság (COMPR / DISKSIZE)
- Nem-ZRAM swap használat
- Aktuális tömörítési arány
- Konzervatív és optimista effektív memória
- ZRAM hatékonyság (DATA - COMPR)
- OOM kockázat jelzés
- Automatikusan méreteződő history grafikon

### History grafikonok

- RAM%
- ZRAM fizikai%
- Nem-ZRAM swap%
- Effektív free% (konzervatív és optimista)

A history hossza automatikusan igazodik a terminál szélességéhez.

### Billentyűk

| Billentyű | Funkció |
|-----------|----------|
| q         | Kilépés |
| +         | Gyorsabb frissítés |
| -         | Lassabb frissítés |

---

# Követelmények

- Linux
- Python 3
- util-linux (zramctl miatt)
- curses támogatás (Linuxon a Python része)

Debian/Ubuntu:

```bash
sudo apt install util-linux
```

---

# Telepítés

```bash
git clone <repo-url>
cd <repo-folder>
chmod +x zram_free.py zram_monitor.py
```

---

# Használat

## Egyszeri riport

```bash
sudo ./zram_free.py
```

## TUI monitor

```bash
sudo ./zram_monitor.py
```

---

# Modellek magyarázata

## Konzervatív modell

Az algoritmushoz rendelt reális tömörítési aránnyal számol.

Példa:

8 GB RAM  
2 GB ZRAM (zstd → 3.0x)

ZRAM logikai kapacitás ≈ 2 × 3 = 6 GB  
Teljes elméleti eltárolható memória ≈ 8 + 6 = 14 GB

Ez tervezésre alkalmas érték.

---

## Optimista modell

A pillanatnyi:

DATA / COMPR

arányt használja.

Ez gyakran túl optimista, mert:

- kezdetben a memória jól tömöríthető
- telítettségnél az arány romlik

Ez inkább tájékoztató jellegű.

---

# OOM kockázat számítás

Figyelembe veszi:

- RAM available arány
- ZRAM fizikai kihasználtság
- Nem-ZRAM swap szabad kapacitás

A jelzés:

- SAFE
- OK
- WARN
- HIGH

Ez heurisztikus jelző, nem kernel-szintű garancia.

---

# Fontos megjegyzések

- A nagy elméleti memória nem egyenlő a fizikai RAM sebességével.
- ZRAM CPU terhelést okoz.
- A valós viselkedés workload-függő.
- A számítások becslések, nem kernel belső algoritmusok.
