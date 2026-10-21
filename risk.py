"""Transparent rule-based return-risk score (v1)."""
from dataclasses import dataclass, field
import re


@dataclass
class PhoneHistory:
    delivered: int = 0
    refused: int = 0
    returned: int = 0
    days_since_last_refusal: int = 3650


@dataclass
class Order:
    amount: float
    address: str
    order_hour: int = 12
    phone: str | None = None
    history: PhoneHistory = field(default_factory=PhoneHistory)
    linked_identities: int = 0
    recent_orders: int = 0
    blocklisted: bool = False
    is_cod: bool = True
    name: str | None = None
    is_first_time: bool = True
    address_quality: float | None = None
    address_flags: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    score: float
    level: str
    action: str
    reasons: list[str]
    credits: float


# Normalize phone digits for risk-feature comparisons.
def normalize_pk_phone(raw):
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits


# Detect address text with implausible Latin-letter patterns.
def _looks_gibberish(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    return bool(words) and all(len(w) >= 4 and sum(ch in "aeiou" for ch in w) / len(w) <= 0.30 for w in words)


# Combine risk signals into a bounded score, level, and reasons.
def _score(total_amount, is_first_time, order_hour, recent_orders, phone_flagged,
           address_quality, address_flags, refused=0, returned=0,
           linked_identities=0, gibberish=False, history_weight=1.0):
    score = 0.04
    reasons = []

    # Add risk points and the reason for each addition.
    def add(points, reason):
        nonlocal score
        score += points
        reasons.append(reason)

    amount_points = min(0.12, max(0.0, (float(total_amount) - 5000) * 0.12 / 25000))
    if amount_points:
        add(amount_points, "Order value adds return risk.")
    if is_first_time:
        add(0.06, "No previous orders from this customer.")
    if order_hour < 8 or order_hour >= 22:
        add(0.04, "Order was placed outside usual daytime hours.")
    if recent_orders >= 2:
        add(min(0.14, 0.05 + 0.03 * (recent_orders - 1)), "Several recent orders from this customer.")
    if phone_flagged:
        add(0.24, "Phone is flagged in the shared return history.")
    if refused or returned:
        add(min(0.20, 0.06 * (refused + returned)) * history_weight,
            "Past orders were refused or returned.")
    if linked_identities >= 2:
        add(min(0.18, 0.04 * linked_identities), "Several linked customer identities were seen.")

    if address_quality is not None:
        q = max(0.0, min(1.0, float(address_quality)))
        if q < 0.25:
            add(0.12, f"Address quality is very low ({q:.2f}).")
        elif q < 0.5:
            add(0.08, f"Address quality is low ({q:.2f}).")
        elif q < 0.7:
            add(0.04, f"Address quality needs review ({q:.2f}).")
    penalties = {
        "missing_house_number": (0.13, "Address flag: missing_house_number."),
        "missing_city": (0.10, "Address flag: missing_city."),
        "landmark_only": (0.10, "Address flag: landmark_only."),
        "too_short": (0.08, "Address flag: too_short."),
        "digits_only": (0.15, "Address flag: digits_only."),
        "gibberish": (0.18, "Address flag: gibberish."),
    }
    for flag in sorted(set(address_flags or [])):
        if flag in penalties:
            add(*penalties[flag])
    if gibberish and "gibberish" not in (address_flags or []):
        add(0.18, "Address flag: gibberish.")

    score = round(min(0.999, max(0.0, score)), 3)
    level = "high" if score >= 0.75 else "medium" if score >= 0.35 else "low"
    if not reasons:
        reasons.append("No strong return-risk signals found.")
    return score, level, reasons


# Actuarial credibility: treat the transparent rules as a prior estimate, then
# give a customer's completed COD outcomes increasing weight as experience grows.
# Five prior orders is a conservative credibility constant, not a fitted value.
CUSTOMER_CREDIBILITY_ORDERS = 5


def apply_customer_credibility(rule_score, completed=0, failed=0):
    """Blend rule score with customer outcomes using a bounded credibility weight.

    Refused/returned are failures; delivered orders are successes. Unresolved
    orders must not be included. With no history this returns the rule score.
    """
    # Bound inputs before blending so malformed counts cannot create an invalid rate.
    completed = max(0, int(completed))
    failed = max(0, min(completed, int(failed)))
    prior = max(0.0, min(1.0, float(rule_score)))
    # Each completed outcome adds weight; the prior still matters for sparse histories.
    credibility = completed / (completed + CUSTOMER_CREDIBILITY_ORDERS)
    observed_rate = failed / completed if completed else prior
    adjusted = (1.0 - credibility) * prior + credibility * observed_rate
    return round(max(0.0, min(0.999, adjusted)), 3), credibility


def score_order(total_amount, address, is_first_time=False, order_hour=12,
                recent_orders=0, phone_flagged=False, address_quality=None,
                address_flags=None):
    """Legacy scoring signature plus optional address-cleaner features."""
    return _score(total_amount, is_first_time, order_hour, recent_orders,
                  phone_flagged, address_quality, address_flags,
                  gibberish=_looks_gibberish(address))


def assess_order(order):
    """Compatibility wrapper for the original standalone Order assessment API."""
    # Prepaid orders bypass COD return-risk rules because there is no refusal at delivery.
    if not order.is_cod:
        return Assessment(0.0, "low", "approve", ["Prepaid order."], 1.0)
    if order.blocklisted:
        return Assessment(0.999, "high", "reject", ["Customer is blocklisted."], 0.0)
    from address_clean import clean_address
    # Explicit caller-supplied quality and flags take precedence over inferred address data.
    cleaned = clean_address(order.address)
    quality = order.address_quality if order.address_quality is not None else cleaned["quality_score"]
    flags = list(order.address_flags or cleaned["flags"])
    name_gibberish = bool(order.name and _looks_gibberish(order.name))
    score, level, reasons = _score(
        order.amount, order.is_first_time, order.order_hour,
        order.recent_orders, False, quality, flags,
        refused=order.history.refused, returned=order.history.returned,
        linked_identities=order.linked_identities,
        gibberish=name_gibberish,
        history_weight=(1.0 if order.history.days_since_last_refusal <= 30 else
                        0.6 if order.history.days_since_last_refusal <= 180 else 0.25),
    )
    if order.phone and len(normalize_pk_phone(order.phone)) < 9:
        score = min(0.999, round(score + 0.10, 3))
        reasons.append("Customer phone number looks incomplete.")
        level = "high" if score >= 0.75 else "medium" if score >= 0.35 else "low"
    return Assessment(score, level, "approve" if level == "low" else "manual_review",
                      reasons, round(1.0 - score, 3))
