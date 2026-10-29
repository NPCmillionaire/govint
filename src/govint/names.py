"""Name normalization and fuzzy matching shared by every source."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

# Legal suffixes and filler that shouldn't affect whether two names match.
_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "l l c", "ltd",
    "limited", "lp", "llp", "plc", "sa", "ag", "gmbh", "bv", "nv", "pllc", "pc",
    "the", "of", "joint", "stock", "pjsc", "ojsc", "jsc", "cjsc", "ao", "ooo", "zao", "pao", "tov",
    "too", "fzco", "fze", "fzc", "sarl", "srl", "spa", "kft", "doo", "sdn", "bhd", "pte", "pty",
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
    "national", "american", "usa", "us", "america", "worldwide", "network", "networks", "labs", "lab", "laboratory", "laboratories",
    "software", "development", "research", "associates", "ventures", "financial", "media",
    "aviation", "air", "aero", "airlines", "airways", "aircraft", "helicopter", "helicopters",
    "group", "holding", "holdings", "oil", "one", "trade", "company", "general", "public", "open",
    "state", "federal", "transport", "leasing", "engineering", "management", "repair", "repairs", "aviator", "aviators",
}


def _core(norm: str) -> str:
    return " ".join(w for w in norm.split() if w not in _GENERIC)


def _covered(w: str, others: list[str], person: bool) -> bool:
    if w in others:
        return True
    if len(w) >= 5 and w.rstrip("s") in {o.rstrip("s") for o in others if len(o) >= 5}:
        return True  # plurals: Labs / Lab
    if person:  # transliterated names vary: Mohammed / Muhammad
        return max(fuzz.ratio(w, x) for x in others) >= 80
    return False  # company words must match exactly: Paris != Pars, Airtrans != Birtrans


def score(a: str, b: str, person: bool = False) -> float:
    """0-100 similarity of a query name `a` to a candidate name `b`, after normalization.

    `person=True` when the candidate is an individual: word order, middle names and
    transliteration vary, so matching is looser.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    s = 0.6 * fuzz.token_set_ratio(na, nb) + 0.4 * fuzz.token_sort_ratio(na, nb)
    qa, qb = na.split(), nb.split()

    # A one-word alias like "Ali" can't match a multi-word query on its own.
    if len(qb) == 1 and len(qa) > 1:
        return min(s, 75.0)
    # Most query words need a close counterpart ("Karp" != "Kovar").
    covered = sum(1 for w in qa if max(fuzz.ratio(w, x) for x in qb) >= 75) / len(qa)
    if covered < 0.67:
        return min(s, 75.0)

    if person:
        if len(qa) == 1:  # one word can't identify a person ("Yang LLC" vs "Yang Qi")
            return min(s, 75.0)
        # "Oleg Deripaska" vs "DERIPASKA, Oleg Vladimirovich": one extra middle name is fine.
        short, long_ = sorted((qa, qb), key=len)
        if len(short) >= 2 and set(short) <= set(long_) and len(long_) - len(short) <= 1:
            s = max(s, 92.0)
        return s

    # Companies: compare the distinctive words (generic ones like "Aviation" don't count).
    ca, cb = _core(na).split(), _core(nb).split()
    if not ca or not cb:
        return min(s, 80.0)  # nothing distinctive to compare
    # Every distinctive query word must appear ("Sierra Alpha Group" != "Alpha Group"),
    # allowing one miss in long names.
    missing_q = [w for w in ca if not _covered(w, cb, False)]
    if len(missing_q) > (1 if len(ca) >= 4 else 0):
        return min(s, 75.0)
    # Extra distinctive words in the candidate make it a different company ("Golden Wings
    # Aviation" vs "Wings Aviation") unless the shared words are long and specific
    # ("Hikvision" vs "Hangzhou Hikvision Digital Technology").
    specific = all(len(w) >= 7 for w in ca)  # e.g. "Hikvision", "Rosneft"
    extra_c = [w for w in cb if not _covered(w, ca, False)]
    if not extra_c or (specific and len(extra_c) <= 2):
        # Same distinctive words, differing only in generic ones ("Rostec" vs "State
        # Corporation Rostec"), or every long distinctive query word is present.
        s = max(s, 92.0)
    elif extra_c:
        s = min(s, 85.0)
    # A distinctive word inserted between the query's words changes the name
    # ("United Airlines" vs "United African Airlines").
    pos = sorted(qb.index(w) for w in set(qa) if w in qb)
    if len(pos) >= 2 and any(w not in qa and w not in _GENERIC and len(w) > 2 for w in qb[pos[0] + 1:pos[-1]]):
        s = min(s, 80.0)
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
