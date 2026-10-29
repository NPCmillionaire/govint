"""Name normalization and fuzzy matching shared by every source."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

# Legal suffixes and filler that shouldn't affect whether two names match.
_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "l l c", "ltd",
    "limited", "lp", "llp", "plc", "sa", "ag", "gmbh", "bv", "nv", "pllc", "pc",
    "the", "of",
}


def normalize(name: str) -> str:
    """Uppercase-insensitive, accent-free, punctuation-free, suffix-free form of a name."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w not in _NOISE]
    return " ".join(words)


# Common words that make unrelated companies look alike ("X Technologies" vs "Y Technology").
_GENERIC = {
    "technology", "technologies", "tech", "systems", "system", "services", "service", "solutions",
    "trading", "industries", "industrial", "industry", "global", "enterprises", "enterprise",
    "partners", "capital", "management", "consulting", "investments", "investment", "energy",
    "resources", "logistics", "shipping", "electronics", "engineering", "manufacturing", "and",
    "national", "american", "usa", "us", "america", "worldwide", "network", "networks", "labs",
    "software", "development", "research", "associates", "ventures", "financial", "media",
}


def _core(norm: str) -> str:
    return " ".join(w for w in norm.split() if w not in _GENERIC)


def score(a: str, b: str) -> float:
    """0-100 similarity between two names after normalization.

    token_set_ratio alone over-matches short names inside long ones ("ACME" vs
    "ACME ROCKET FUEL OF IRAN"), so it is blended with a plain ratio.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    s = 0.6 * fuzz.token_set_ratio(na, nb) + 0.4 * fuzz.token_sort_ratio(na, nb)
    # "Oleg Deripaska" vs "DERIPASKA, Oleg Vladimirovich": every word present, one extra (a
    # middle name or patronymic) shouldn't sink the match.
    # Most query words must each have a close counterpart ("Karp" ≠ "Kovar"), and a one-word
    # alias like "Ali" can't match a multi-word query on its own.
    qa, qb = na.split(), nb.split()
    covered = sum(1 for w in qa if max(fuzz.ratio(w, x) for x in qb) >= 75) / len(qa)
    if covered < 0.67 or (len(qb) == 1 and len(qa) > 1):
        s = min(s, 75.0)
    short, long_ = sorted((qa, qb), key=len)
    if len(short) >= 2 and set(short) <= set(long_) and len(long_) - len(short) <= 1:
        s = max(s, 92.0)
    # The distinctive words must agree too, or cap below any match threshold.
    ca, cb = _core(na), _core(nb)
    if ca and cb and fuzz.token_set_ratio(ca, cb) < 85:
        s = min(s, 75.0)
    return s


_ADDR = {"boulevard": "blvd", "street": "st", "avenue": "ave", "drive": "dr", "road": "rd",
         "suite": "ste", "floor": "fl", "parkway": "pkwy", "highway": "hwy", "lane": "ln",
         "court": "ct", "place": "pl", "north": "n", "south": "s", "east": "e", "west": "w",
         "us": "", "usa": ""}


def address_key(text: str) -> str:
    """Stable id for an address so 'Suite 2350, Biscayne Boulevard' == 'STE 2350 BISCAYNE BLVD'."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\bus-([a-z]{2})\b", r"\1", s)  # GLEIF "US-CO" region codes
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [_ADDR.get(w, w) for w in s.split()]
    words = [w for w in words if w]
    if words and len(words[-1]) == 9 and words[-1].isdigit():  # ZIP+4 -> ZIP
        words[-1] = words[-1][:5]
    return " ".join(words)


def person_variants(name: str) -> list[str]:
    """'BURRITT DAVID B' <-> 'David B Burritt' style reorderings for person matching."""
    parts = normalize(name).split()
    if len(parts) < 2:
        return [name]
    return [" ".join(parts), " ".join(parts[1:] + parts[:1])]
