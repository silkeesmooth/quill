"""Your saved details, and matching them to a form's fields.

The profile is a small JSON file in your user folder (plain text, readable by
your Windows account only). Fields that ask about someone else (mother,
spouse, emergency contact...) or a different value (name at birth, previous
address...) are left alone.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass

import quill_sign as ps

# key, question, example
FIELDS = [
    ("first_name", "First name", ""),
    ("middle_name", "Middle name", ""),
    ("last_name", "Last name", ""),
    ("suffix", "Suffix (Jr., III)", ""),
    ("email", "Personal email", ""),
    ("work_email", "Work email", ""),
    ("phone", "Phone", "555-123-4567"),
    ("street", "Street address", "123 Main St"),
    ("address2", "Apartment / unit", "Apt 4"),
    ("city", "City", ""),
    ("state", "State", "TX or Texas"),
    ("zip", "ZIP code", ""),
    ("country", "Country", "United States"),
    ("birth_date", "Date of birth", "MM/DD/YYYY"),
    ("birth_city", "City of birth", ""),
    ("birth_state", "State or country of birth", ""),
    ("drivers_license", "Driver's license number", ""),
    ("employer", "Employer", ""),
    ("job_title", "Job title", ""),
]
KEYS = [k for k, _, _ in FIELDS]
SENSITIVE = re.compile(r"ssn|social|passport|license|account|routing|card|pin|password", re.I)

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico",
}
BY_NAME = {v.lower(): k for k, v in STATES.items()}


# ---------------------------------------------------------------- storage


def path():
    return ps.home() / "profile.json"


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if str(v).strip()}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    ps.home().mkdir(parents=True, exist_ok=True)
    clean = {k: v.strip() for k, v in data.items() if v and v.strip()}
    path().write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_date(text: str) -> datetime.date | None:
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def check_value(key: str, value: str) -> str:
    """Normalize a profile answer, or raise ValueError with a reason."""
    value = value.strip()
    if key == "birth_date" and value:
        d = parse_date(value)
        if not d:
            raise ValueError("Use MM/DD/YYYY, for example 04/23/1990.")
        return d.strftime("%m/%d/%Y")
    if key == "phone" and value and len(re.sub(r"\D", "", value)) < 7:
        raise ValueError("That doesn't look like a phone number.")
    if key in ("email", "work_email") and value and "@" not in value:
        raise ValueError("That doesn't look like an email address.")
    return value


# ---------------------------------------------------------------- field text


VOCAB = sorted("""first last middle full name names zip code area phone number street address
city state birth date email mailing of at on mothers fathers mother father home work cell
mobile postal country today todays signed suffix prefix emergency contact title employer
company diff different recent card other used day month year dob mail apt unit line
drivers driver license licence dl no num
""".split(), key=len, reverse=True)


def split_glued(word: str) -> str:
    """'cityofbirth' -> 'city of birth', using a small vocabulary."""
    n = len(word)
    best: list = [None] * (n + 1)
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        for v in VOCAB:
            if word.startswith(v, i) and best[i + len(v)] is None:
                best[i + len(v)] = best[i] + [v]
    return " ".join(best[n]) if best[n] else word


def field_text(name: str, label: str) -> str:
    """Lower-case words describing a field, from its name and label."""
    last = name.rsplit(".", 1)[-1]
    last = re.sub(r"\[\d+\]", " ", last)
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    words = re.split(r"[^A-Za-z0-9']+", last)
    words = [split_glued(w.lower()) if len(w) > 6 else w.lower() for w in words if w]
    lab = re.sub(r"[^a-z0-9'/]+", " ", (label or "").lower())
    return re.sub(r"\s+", " ", " ".join(words) + " | " + lab).strip()


SOMEONE_ELSE = re.compile(
    r"\b(mother'?s?|father'?s?|parents?'?|spouse'?s?|husband'?s?|wife'?s?|partner'?s?|emergency|"
    r"contact|child(ren)?'?s?|dependents?|beneficiar\w*|guardian'?s?|witness|supervisor|manager|"
    r"reference|physician|doctor|preparer|notary|agent|previous|prior|former|maiden|alias|"
    r"other|different|diff|recent|landlord|owner|relationship|relative|co ?applicant|"
    r"office use|for office|official)\b")
BIRTH = re.compile(r"\b(birth|dob|born)\b")
DATE_NOT_TODAY = re.compile(
    r"\b(birth|dob|born|expir\w*|issue[ds]?|hire[ds]?|start|end|effective|due|arriv\w*|"
    r"depart\w*|from|through|period|admission|discharge|incident|loss|injury|purchase|"
    r"marriage|death|employment|graduat\w*|move[ds]?|office)\b")
WORKISH = r"\b(work|business|office|company|employer|corporate|job)\b"


@dataclass
class Rule:
    key: str
    pattern: re.Pattern
    birth_ok: bool = False            # may the field mention birth?
    unless: re.Pattern | None = None  # skip this rule (try the next) if this matches


def R(key, pattern, birth_ok=False, unless=None):
    return Rule(key, re.compile(pattern), birth_ok, re.compile(unless) if unless else None)


# Most specific first; the first rule that matches wins.
RULES = [
    # a work email box gets only the work email (empty if none is saved)
    R("work_email", rf"(?=.*{WORKISH}).*\be ?mail\b"),
    R("email", r"\be ?mail\b"),
    R("birth_date", r"\b(date of birth|birth ?date|dob|born on)\b", True),
    R("birth_city", r"\b(city|place|town) (of|at) birth\b|\bbirth ?(city|place)\b|place of birth",
      True),
    R("birth_state", r"\b(state|country)\b.*\b(of|at) birth\b|\bbirth ?(state|country)\b", True),
    # license state / class / dates aren't the number: no saved value, so left empty
    R("license_detail", r"\blicen[cs]e\b.*\b(state|class|type|expir\w*|issued?|date|restrictions?|"
      r"endorsements?|country)\b|\b(state|class|expir\w*|issued?)\b.*\blicen[cs]e\b"),
    # only a *driver's* license: a bare "license number" may be a professional one
    R("drivers_license", r"\b(drivers?|driver's|driving|operator'?s?)\b.*\blicen[cs]e\b|"
      r"\blicen[cs]e\b.*\bdriv\w*|\bdl\b|\bd ?l ?(#|no|num|number)\b"),
    R("age", r"\bage\b"),
    R("city_state_zip", r"\bcity\b.*\bstate\b.*\b(zip|postal)\b"),
    R("area_code", r"\barea code\b"),
    R("phone", r"\b(phone|telephone|tel|mobile|cell)\b"),
    R("zip", r"\b(zip|postal ?code|post ?code)\b"),
    # city/state/country before street: "Mailing address - City" is a city
    R("city", r"\b(city|town)\b"),
    R("state", r"\bstate\b"),
    R("country", r"\bcountry\b"),
    R("address2", r"\b(address (line )?2|line 2|apt|apartment|suite|unit)\b", unless=r"\bstreet\b"),
    R("street", r"\b(street|address( line)?( 1)?|addr|residence)\b"),
    R("first_name", r"\b(first|given) name\b|\bfname\b|\| first$|: first$|^first \||\bfirst$"),
    R("middle_initial", r"\b(mi|m i|middle initial|initial)\b"),
    R("middle_name", r"\bmiddle( name)?\b"),
    R("last_name", r"\b(last|family|sur) ?name\b|\blname\b|\| last$|: last$|^last \||\blast$"),
    R("suffix", r"\bsuffix\b"),
    R("prefix", r"\b(prefix|salutation|honorific)\b"),
    R("employer", r"\b(employer|company|organization|business)( name)?\b"),
    R("job_title", r"\b(job title|occupation|position)\b"),
    R("full_name", r"\b(full name|printed name|print name|your name|name|applicant)\b"),
    R("today", r"\b(today'?s?( date)?|date signed|signature date|date)\b"),
]
NOT_A_NAME = re.compile(r"\b(company|business|organization|user|file|account|bank|street|"
                        r"school|employer|product|event|project|plan|pet)\b")
WORK = re.compile(r"\b(work|office|business|fax|daytime work)\b")


# ---------------------------------------------------------------- values


def date_format(text: str) -> str:
    # field text has dashes turned to spaces, so compare with separators removed
    t = re.sub(r"[\s/.-]", "", text.lower())
    if "yyyymmdd" in t:
        return "%Y-%m-%d"
    if "ddmmyyyy" in t:
        return "%d/%m/%Y"
    if re.search(r"mmddyy(?!yy)", t):
        return "%m/%d/%y"
    return "%m/%d/%Y"


def state_value(stored: str, text: str, maxlen: int) -> str:
    s = stored.strip()
    abbr = s.upper() if s.upper() in STATES else BY_NAME.get(s.lower())
    if not abbr:
        return s  # a foreign country or region: use as given
    if maxlen and maxlen <= 3:
        return abbr
    if "abbreviat" in text:  # "do not abbreviate"
        return STATES[abbr]
    return s


def phone_parts(phone: str) -> tuple[str, str, str]:
    d = re.sub(r"\D", "", phone)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return d[:3], f"{d[3:6]}-{d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return "", phone, phone


def value_for(key: str, prof: dict, text: str, maxlen: int, form_keys: set) -> str:
    today = datetime.date.today()
    if key == "today":
        return today.strftime(date_format(text))
    if key == "birth_date":
        d = parse_date(prof.get("birth_date", ""))
        return d.strftime(date_format(text)) if d else ""
    if key == "age":
        d = parse_date(prof.get("birth_date", ""))
        if not d:
            return ""
        return str(today.year - d.year - ((today.month, today.day) < (d.month, d.day)))
    if key == "full_name":
        parts = [prof.get("first_name"), prof.get("middle_name"), prof.get("last_name"),
                 prof.get("suffix")]
        return " ".join(p for p in parts if p)
    if key == "middle_initial":
        m = prof.get("middle_name", "")
        return m[:1].upper() if m else ""
    if key in ("state", "birth_state"):
        return state_value(prof.get(key, ""), text, maxlen) if prof.get(key) else ""
    if key == "city_state_zip":
        st = state_value(prof.get("state", ""), "", 2) if prof.get("state") else ""
        left = ", ".join(p for p in (prof.get("city"), st) if p)
        return f"{left} {prof.get('zip', '')}".strip()
    if key in ("phone", "area_code"):
        if not prof.get("phone"):
            return ""
        area, rest, full = phone_parts(prof["phone"])
        if key == "area_code":
            return area
        return rest if "area_code" in form_keys and area else full
    if key == "street":
        street = prof.get("street", "")
        if street and prof.get("address2") and "address2" not in form_keys:
            street = f"{street}, {prof['address2']}"
        return street
    return prof.get(key, "")


def rule_for(name: str, label: str, ftype: str) -> str | None:
    text = field_text(name, label)
    if SOMEONE_ELSE.search(text):
        return None
    for rule in RULES:
        if not rule.pattern.search(text) or (rule.unless and rule.unless.search(text)):
            continue
        if not rule.birth_ok and BIRTH.search(text):
            return None  # e.g. "Name at birth": not the current value
        if rule.key == "today" and DATE_NOT_TODAY.search(text):
            return None
        if rule.key in ("phone", "area_code") and WORK.search(text):
            return None
        if rule.key == "full_name" and NOT_A_NAME.search(text):
            return None
        if rule.key in NAME_PARTS and _names_mentioned(text) > 1:
            return None  # "First Full Middle Name Last": a whole row, not one box
        return rule.key
    return None


NAME_PARTS = ("first_name", "middle_name", "last_name")


def _names_mentioned(text: str) -> int:
    lab = text.split("|", 1)[-1]
    return sum(1 for w in ("first", "middle", "last") if re.search(rf"\b{w}\b", lab))


def custom_rule(name: str, label: str, prof: dict) -> str | None:
    """Keys you added yourself match fields that mention all their words."""
    text = field_text(name, label)
    if SOMEONE_ELSE.search(text):
        return None
    for key in prof:
        if key in KEYS or key.startswith("_"):  # "_" keys are quill's own notes
            continue
        words = re.findall(r"[a-z0-9]+", key.lower().replace("_", " "))
        if words and all(re.search(rf"\b{re.escape(w)}", text) for w in words):
            return key
    return None


def plan(fields: list[dict], prof: dict, maxlens: dict[str, int] | None = None) -> list[tuple]:
    """(field, key, value) for every empty field we can fill."""
    maxlens = maxlens or {}
    keyed = []
    for f in fields:
        if f["type"] not in ("text", "dropdown", "list") or f["value"]:
            continue
        key = custom_rule(f["name"], f["label"], prof) or rule_for(f["name"], f["label"], f["type"])
        if key:
            keyed.append((f, key))
    # Two fields on one page with the same label and the same match can't be
    # told apart (two "Street" lines, area code and number both "Phone"): skip.
    def same(f, k):
        return (f.get("pages", [0])[0], k, field_text("", f["label"]))
    seen: dict[tuple, int] = {}
    for f, k in keyed:
        seen[same(f, k)] = seen.get(same(f, k), 0) + 1
    keyed = [(f, k) for f, k in keyed if not f["label"] or seen[same(f, k)] == 1]
    form_keys = {k for _, k in keyed}
    out = []
    for f, key in keyed:
        text = field_text(f["name"], f["label"])
        maxlen = maxlens.get(f["name"], 0)
        value = value_for(key, prof, text, maxlen, form_keys)
        if not value:
            continue
        if f["type"] in ("dropdown", "list"):
            value = pick_option(value, f["options"])
            if value is None:
                continue
        if maxlen and len(value) > maxlen:
            continue
        out.append((f, key, value))
    return out


def pick_option(value: str, options: list[str]) -> str | None:
    v = value.strip().lower()
    alts = {v}
    if v.upper() in STATES:
        alts.add(STATES[v.upper()].lower())
    if v in BY_NAME:
        alts.add(BY_NAME[v].lower())
    for o in options:
        if o.strip().lower() in alts:
            return o
    return None
