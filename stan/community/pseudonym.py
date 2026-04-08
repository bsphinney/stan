"""Generate memorable anonymous lab names for community submissions.

Each STAN installation gets a pseudonym on first setup, stored in
~/.stan/community.yml as `display_name`. The name is deterministic per
installation (once generated it never changes) but not traceable back
to the lab. Users can override with their real name if they prefer.

Format: "Adjective Scientist" — proteomics-themed so the names are
fun and on-brand. ~2500 unique combinations (50 × 50).

Examples: Nimble Edman, Bold Tanaka, Keen Aebersold, Swift Sanger
"""

from __future__ import annotations

import hashlib
import random


# ── Word lists ──────────────────────────────────────────────────────

ADJECTIVES = [
    # Heroic / adventurous
    "Nimble", "Bold", "Daring", "Fearless", "Intrepid",
    "Mighty", "Valiant", "Fierce", "Relentless", "Unstoppable",
    # Sciencey / precise
    "Keen", "Sharp", "Precise", "Calibrated", "Resolute",
    "Focused", "Meticulous", "Diligent", "Tireless", "Methodical",
    # Funny / playful
    "Caffeinated", "Turbo", "Hyperfocused", "Overloaded", "Unfiltered",
    "Ionized", "Fragmented", "Eluted", "Supersonic", "Pressurized",
    "Clogged", "Leaky", "Sputtering", "Misaligned", "Degassed",
    # Colors / vibes
    "Golden", "Crimson", "Azure", "Emerald", "Cosmic",
    "Stellar", "Radiant", "Invisible", "Phantom", "Stealth",
    # MS-themed
    "Charged", "Neutral", "Depleted", "Enriched", "Digested",
    "Alkylated", "Oxidized", "Deamidated", "Truncated", "Concatenated",
]

# Mass spec / proteomics / chemistry pioneers + a few funny ones
SCIENTISTS = [
    # Real legends
    "Edman", "Sanger", "Fenn", "Tanaka", "Aebersold",
    "Mann", "Coon", "Cooks", "McLafferty", "Biemann",
    "Hunt", "Yates", "Gygi", "Olsen", "Cox",
    "Lamond", "Domon", "Hillenkamp", "Karas", "Thomson",
    "Aston", "Burlingame", "Eng", "Fenselau", "Kelleher",
    "MacCoss", "Nesvizhskii", "Rappsilber", "Washburn", "Zubarev",
    "Cottrell", "Demichev", "Elias", "Bantscheff", "Steen",
    # Instrument / lab life themed
    "Quadrupole", "Hexapole", "Orbitrap", "Reflectron", "Emitter",
    "Gradient", "Baseline", "Eluent", "Peptide", "Precursor",
    "Contaminant", "Keratin", "Trypsin", "Autolysis", "HeLa",
    # Pure comedy
    "McSpecface", "ColumnGhost", "DeadVolume", "PeakTail", "Carryover",
    "BlankRun", "GhostPeak", "FalsePositive", "MissedCleavage", "LostIon",
]


def generate_pseudonym(seed: str | None = None) -> str:
    """Generate a random lab pseudonym.

    Args:
        seed: Optional seed string for deterministic generation.
              If None, uses system randomness.

    Returns:
        A name like "Nimble Edman" or "Bold Tanaka".
    """
    if seed is not None:
        # Deterministic from seed
        h = hashlib.sha256(seed.encode()).digest()
        adj_idx = h[0] % len(ADJECTIVES)
        sci_idx = h[1] % len(SCIENTISTS)
    else:
        adj_idx = random.randrange(len(ADJECTIVES))
        sci_idx = random.randrange(len(SCIENTISTS))

    return f"{ADJECTIVES[adj_idx]} {SCIENTISTS[sci_idx]}"


def is_pseudonym(name: str) -> bool:
    """Check if a display_name looks like a generated pseudonym."""
    parts = name.split()
    if len(parts) != 2:
        return False
    return parts[0] in ADJECTIVES and parts[1] in SCIENTISTS


def generate_unique_pseudonym(relay_url: str = "https://brettsp-stan.hf.space") -> str:
    """Generate a pseudonym that isn't already taken on the community site.

    Queries the relay's /api/leaderboard to get existing display_names,
    then generates candidates until one is unique. Falls back to adding
    a numeric suffix after 20 attempts (extremely unlikely with ~3000
    combinations and <100 labs, but handles it gracefully).

    If the relay is unreachable (offline, no internet), just returns
    a random name without dedup — better to risk a collision than to
    block setup because of a network issue.
    """
    existing: set[str] = set()
    try:
        import urllib.request
        import json
        req = urllib.request.Request(f"{relay_url}/api/leaderboard", headers={"User-Agent": "STAN"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            for sub in data.get("submissions", []):
                name = sub.get("display_name", "")
                if name:
                    existing.add(name)
    except Exception:
        # Offline or relay down — generate without dedup check
        return generate_pseudonym()

    # Try up to 20 random names
    for _ in range(20):
        candidate = generate_pseudonym()
        if candidate not in existing:
            return candidate

    # Extremely unlikely fallback: add a numeric suffix
    import random
    base = generate_pseudonym()
    suffix = random.randint(100, 999)
    return f"{base} {suffix}"
