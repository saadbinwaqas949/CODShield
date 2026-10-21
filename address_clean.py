"""Offline Pakistani address normalization. This module has no database access."""
import csv
import difflib
import hashlib
import os
import re
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
_AREA_FILE = Path(__file__).parent / "data" / "areas.csv"
_URDU_CHARS = "\u0600-\u06ff"
_FILLER = {"please", "plz", "kindly", "bhai", "sir", "madam", "address", "pata", "mera", "meri", "hai", "he", "ka", "ki", "ko", "mein", "me", "near", "at"}
_CITY_ALIASES = {
    "Lahore": ("lahore", "lhr", "لاہور"),
    "Karachi": ("karachi", "khi", "کراچی"),
    "Islamabad": ("islamabad", "isb", "isl", "اسلام آباد"),
    "Rawalpindi": ("rawalpindi", "rwp", "pindi", "راولپنڈی"),
}
_CITY_ALIASES["Lahore"] += ("\u0644\u0627\u06c1\u0648\u0631",)
_CITY_ALIASES["Karachi"] += ("\u06a9\u0631\u0627\u0686\u06cc",)
_CITY_ALIASES["Islamabad"] += ("\u0627\u0633\u0644\u0627\u0645 \u0622\u0628\u0627\u062f",)
_CITY_ALIASES["Rawalpindi"] += ("\u0631\u0627\u0648\u0644\u067e\u0646\u0688\u06cc",)
_LANDMARK_WORDS = (
    "chowk", "masjid", "mall", "school", "hospital", "market", "mosque",
    "samne", "qareeb", "near", "opposite", "landmark", "چوک", "مسجد", "قریب", "سامنے", "اسپتال",
)
_NOISE = re.compile(r"\b(?:please|plz|kindly|bhai|sir|madam|deliver|delivery|address|mera|meri)\b", re.I)


# Normalize text for case-insensitive address matching.
def _fold(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[\u064b-\u065f\u0670]", "", value)
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ی")
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


# Load known city and area names from the project data file.
def _load_areas():
    entries = []
    with _AREA_FILE.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            aliases = [row["area"], *(row.get("aliases") or "").split("|")]
            entries.append((row["city"], row["area"], tuple(dict.fromkeys(_fold(a) for a in aliases if a))))
    return entries


_AREAS = _load_areas()


# Match a whole address phrase without partial-word matches.
def _contains(text, phrase):
    return f" {phrase} " in f" {text} "


# Find a recognized city in the supplied address text.
def _find_city(text):
    for city, aliases in _CITY_ALIASES.items():
        if any(_contains(text, _fold(alias)) for alias in aliases):
            return city
    return None


# Find a recognized area, preferring the selected city.
def _find_area(text, city):
    candidates = [entry for entry in _AREAS if city is None or entry[0] == city]
    exact = []
    for entry in candidates:
        for alias in entry[2]:
            if _contains(text, alias):
                exact.append((len(alias), len(alias.split()), entry[1]))
    # Prefer the longest exact alias so a specific area beats a shorter name inside it.
    if exact:
        return max(exact, key=lambda item: (item[0], item[1], -len(item[2])))[2]

    # Fuzzy matching uses longer alias words to reduce accidental matches.
    words = text.split()
    fuzzy = []
    for _city, area, aliases in candidates:
        for alias in aliases:
            for alias_word in alias.split():
                if len(alias_word) < 5:
                    continue
                for word in words:
                    ratio = difflib.SequenceMatcher(None, word, alias_word).ratio()
                    if ratio >= 0.84:
                        fuzzy.append((ratio, area))
    return max(fuzzy)[1] if fuzzy else None


# Normalize Roman Urdu words and numeric address parts.
def _roman_or_digits(value):
    value = value.lower()
    if value.isdigit():
        return str(int(value))
    roman = {"i": 1, "v": 5, "x": 10}
    total = 0
    for i, ch in enumerate(value):
        amount = roman.get(ch, 0)
        total += -amount if i + 1 < len(value) and amount < roman.get(value[i + 1], 0) else amount
    return str(total) if total else value.upper()


# Extract structured address parts and quality flags.
def _extract(raw, text):
    house = None
    match = re.search(r"(?:house|h\s*#?|h#|plot|flat|apt|apartment|ghar|makan|\u06af\u06be\u0631|\u0645\u06a9\u0627\u0646)\s*[-:#]?\s*([0-9]+[a-z]?)", text, re.I)
    if match:
        house = match.group(1).upper()
    else:
        # Compact local form: "341 A1 DHA Lahore" means house 341, block A1.
        match = re.match(r"^\s*(\d+[a-z]?)\s+(?:[a-z]?\d+[a-z]?|\d+[a-z])\b", text, re.I)
        if match:
            house = match.group(1).upper()

    # Parse block, phase, or sector labels separately from the house number.
    block = None
    match = re.search(r"\b(phase|ph|block|blk|sector|sec)\s*[-:#]?\s*([0-9]+|[ivx]+|[a-z])\b", text, re.I)
    if match:
        label = "Phase" if match.group(1).lower() in ("phase", "ph") else "Block" if match.group(1).lower() in ("block", "blk") else "Sector"
        block = f"{label} {_roman_or_digits(match.group(2))}"
    else:
        match = re.search(r"(\u0641\u06cc\u0632|\u0628\u0644\u0627\u06a9|\u0633\u06cc\u06a9\u0679\u0631)\s*([0-9]+)", text)
        if match:
            label = {"\u0641\u06cc\u0632": "Phase", "\u0628\u0644\u0627\u06a9": "Block", "\u0633\u06cc\u06a9\u0679\u0631": "Sector"}[match.group(1)]
            block = f"{label} {match.group(2)}"

    if not block:
        match = re.match(r"^\s*\d+[a-z]?\s+([a-z]?\d+[a-z]?|\d+[a-z])\b", text, re.I)
        if match:
            block = f"Block {match.group(1).upper()}"

    # Stop street extraction when a recognized area or city name begins.
    street = None
    match = re.search(r"\b(street|st|road|rd|gali|lane|mohalla)\s*[-:#]?\s*([\w -]+)", text, re.I)
    if match:
        part = match.group(2).strip()
        tokens = part.split()
        stops = [alias.split() for _city, _area, aliases in _AREAS for alias in aliases]
        stops.extend([_fold(alias).split() for aliases in _CITY_ALIASES.values() for alias in aliases])
        for index in range(len(tokens)):
            if any(tokens[index:index + len(stop)] == stop for stop in stops if stop):
                tokens = tokens[:index]
                break
        part = " ".join(tokens).strip(" -")
        street = (match.group(1).title() + (" " + part if part else "")).strip()
    elif re.search(r"[\u06af\u0645]\u0644\u06cc", text):
        street = "Gali"

    landmark = None
    if any(word in text for word in _LANDMARK_WORDS):
        landmark = " ".join(raw.split())[:120] or None
    return house, block, street, landmark


def clean_address(raw):
    """Return canonical address fields, transparent quality flags, and salted hash."""
    original = "" if raw is None else str(raw)
    text = _fold(original)
    text = re.sub(r"\b(?:lhr|khi|isb|rwp)\b", lambda m: {"lhr": "lahore", "khi": "karachi", "isb": "islamabad", "rwp": "rawalpindi"}[m.group(0)], text)
    text = re.sub(r"\b(?:ph)\b", "phase", text)
    text = re.sub(r"\b(?:blk)\b", "block", text)
    text = re.sub(r"\b(?:st)\b", "street", text)
    text = re.sub(r"\bh\s*#?\s*(?=\d)", "house ", text)
    city = _find_city(text)
    area = _find_area(text, city)
    area = {"DHA Lahore": "DHA", "Gulberg III": "Gulberg",
            "Allama Iqbal Town": "Iqbal Town", "Lahore Cantt": "Cantt"}.get(area, area)
    if area == "WAPDA Town":
        area = "Wapda Town"
    house, block, street, landmark = _extract(original, text)
    # Flags describe missing or suspicious parts; the score below uses the same flags.
    flags = []
    if not house:
        flags.append("missing_house_number")
    if not city:
        flags.append("missing_city")
    if landmark and not any((house, street, block)):
        flags.append("landmark_only")

    words = re.findall(rf"[a-z0-9]+|[{_URDU_CHARS}]+", text)
    meaningful = [word for word in words if word not in _FILLER]
    if len(meaningful) < 3:
        flags.append("too_short")
    letters = re.sub(r"[^a-z\u0600-\u06ff]", "", text)
    if text and not letters and any(ch.isdigit() for ch in text):
        flags.append("digits_only")
    known = bool(city or area)
    alpha_words = re.findall(r"[a-z]+", text)
    if not known and len("".join(alpha_words)) >= 4 and alpha_words and all(
        len(word) >= 4 and sum(ch in "aeiou" for ch in word) / len(word) <= 0.30 for word in alpha_words
    ):
        flags.append("gibberish")

    # Quality points: city +0.25, recognized area +0.25, house number +0.20,
    # street or block/phase/sector +0.20. Subtract 0.10 for fewer than 3 words,
    # 0.25 for gibberish, and 0.25 for digits only. Clamp the result to [0, 1].
    quality = (0.25 * bool(city) + 0.25 * bool(area) + 0.20 * bool(house)
               + 0.20 * bool(street or block) - 0.10 * ("too_short" in flags)
               - 0.25 * ("gibberish" in flags) - 0.25 * ("digits_only" in flags))
    quality = round(max(0.0, min(1.0, quality)), 2)

    # Construct fields in delivery-friendly order, preserving raw text only as a fallback.
    normalized_parts = []
    if house:
        normalized_parts.append(f"House {house}")
    if block:
        normalized_parts.append(block)
    if street:
        normalized_parts.append(street)
    if area:
        normalized_parts.append(area)
    if city:
        normalized_parts.append(city)
    if landmark:
        normalized_parts.append(landmark)
    if not normalized_parts:
        cleaned = _NOISE.sub(" ", original)
        normalized_parts = [" ".join(cleaned.split())] if cleaned.strip() else []
    normalized_text = ", ".join(normalized_parts)

    # Hash only stable location fields; the salt prevents direct dictionary matching.
    canonical = "|".join(((house or "").casefold(), (block or "").casefold(),
                          (area or "").casefold(), (city or "").casefold()))
    salt = os.getenv("HASH_SALT") or "codshield-salt-"
    addr_hash = hashlib.sha256((salt + canonical).encode("utf-8")).hexdigest()
    return {"city": city, "area": area, "street": street, "house_no": house,
            "block_phase_sector": block, "landmark": landmark, "quality_score": quality,
            "flags": flags, "normalized_text": normalized_text, "addr_hash": addr_hash}
