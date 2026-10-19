"""
The small, boring functions that decide whether reconciliation works.

Nothing here is clever. All of it is load-bearing: AP breaks on invoice numbers
that are written four ways and dates exported in six formats, not on algorithms.
"""

import re
import difflib
from datetime import datetime, timedelta, date, timezone


# ── invoice numbers ──────────────────────────────────────────────────────────

_INV_PREFIX = re.compile(r"^(INV|INVOICE|BILL|DOC|NO|NUM|REF)[-_ ]?", re.I)


def norm_invoice(n) -> str:
    """The key two systems can agree on.

    Bill.com writes 'INV-0012345', the PDF says '12345', the statement says
    '0012345 ', and a human typed 'inv 12345' into QBO. Strip everything that
    is not a letter or digit, drop a leading INV/BILL/REF, then drop leading
    zeros. Compare on that or nothing ever matches.

    The zeros are stripped last and only if something remains, so an invoice
    genuinely numbered '000' does not normalise to the empty string and collide
    with every other unnumbered row.
    """
    t = re.sub(r"[^A-Za-z0-9]", "", str(n or "")).upper()
    if not t:
        return ""
    t = _INV_PREFIX.sub("", t)
    stripped = t.lstrip("0")
    return stripped or t


def invoice_numbers_equal(a, b) -> bool:
    na, nb = norm_invoice(a), norm_invoice(b)
    return bool(na) and na == nb


_PREFIX_SPLIT = re.compile(r"^([A-Z]*)(\d+)$")


def split_invoice_key(n):
    """('ABC', '1') for a normalised key, or (None, None) if it is not a
    simple letters-then-digits shape.

    This exists for one very common situation: the supplier prints
    'ABC-000001' on the invoice PDF and plain '1' on the statement, and
    your own books have whichever one somebody typed. They are the same
    invoice. Nothing in a pure string comparison can see that, which is why
    matching does a second pass using this.

    Deliberately strict — letters then digits and nothing else. 'ABC-12-345'
    returns nothing rather than a guess, because splitting it would be
    inventing structure the vendor did not put there.
    """
    m = _PREFIX_SPLIT.match(norm_invoice(n) or "")
    if not m:
        return None, None
    prefix, digits = m.group(1), m.group(2).lstrip("0") or m.group(2)
    return prefix, digits


# ── money ────────────────────────────────────────────────────────────────────

def num(v):
    """A number out of whatever the exporting system produced.

    Handles '$1,234.56', '1 234,56', '(450.00)' for negatives, and the trailing
    'CR' some statements use for a credit. Returns None when there is no number
    at all, which the callers treat as 'absent' rather than zero — the
    difference between a line that says nothing and a line that says zero.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)

    s = str(v).strip()
    if not s:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if re.search(r"\bCR\b|\bCREDIT\b", s, re.I):
        negative = True
    s = re.sub(r"\b(CR|DR|CREDIT|DEBIT)\b", "", s, flags=re.I)

    # European format: 1.234,56 — a comma as the decimal separator with dots
    # as thousands. Detect it before stripping, or the value becomes 123456.
    if re.match(r"^-?[\d.]+,\d{1,2}$", s.strip()):
        s = s.replace(".", "").replace(",", ".")

    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if negative:
        val = -abs(val)
    return round(val, 2)


def money(v) -> str:
    n = num(v)
    if n is None:
        return "—"
    return f"({abs(n):,.2f})" if n < 0 else f"{n:,.2f}"


def amounts_agree(a, b, tolerance: float = 0.02) -> bool:
    """Two amounts are the same money. A two-cent default absorbs rounding
    between a system that stores cents and one that stores a rounded total."""
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tolerance


# ── dates ────────────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y",
    "%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%d-%b-%Y", "%b %d %Y",
    "%m-%d-%Y", "%d.%m.%Y", "%Y%m%d", "%B %d, %Y", "%d %B %Y",
)


def parse_date(s):
    """A date object from almost anything, or None.

    Never guesses between d/m and m/d — the format list puts US order first
    because that is what the source systems here emit. An ambiguous
    '03/04/2026' therefore reads as 4 March, consistently, which is better
    than reading it differently on different rows.
    """
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s

    t = str(s).strip()
    if not t:
        return None
    t = t.split("T")[0]
    if " " in t and ":" in t:
        t = t.split(" ")[0]

    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


def iso(d) -> str:
    d = parse_date(d)
    return d.isoformat() if d else ""


def period_of(d) -> str:
    """The YYYY-MM a date belongs to."""
    d = parse_date(d)
    return d.strftime("%Y-%m") if d else ""


def current_period() -> str:
    """The period an AP run settles: last month, because you pay in arrears."""
    today = datetime.now(timezone.utc).date()
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def period_bounds(period: str):
    """(first_day, last_day) for 'YYYY-MM'."""
    y, m = (int(x) for x in period.split("-")[:2])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def months_back_for_terms(terms_days) -> int:
    """How far back a run reaches, given the vendor's terms.

    Payment goes out in the first week of the month. On net 30 an invoice dated
    the 28th of last month is not strictly due yet, but it belongs in this run —
    the cut is the END of last month, because that is how the vendor draws the
    statement and when they expect the money. Net 45 behaves the same. Net 60
    reaches a month further back.
    """
    try:
        t = int(terms_days or 30)
    except (TypeError, ValueError):
        t = 30
    if t <= 45:
        return 1
    if t <= 75:
        return 2
    if t <= 105:
        return 3
    return 4


def cutoff_for_terms(terms_days, today=None) -> date:
    """The last invoice date eligible for this run."""
    d = today or datetime.now(timezone.utc).date()
    first = d.replace(day=1)
    for _ in range(months_back_for_terms(terms_days)):
        first = (first - timedelta(days=1)).replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def due_date_for(invoice_date, terms_days):
    d = parse_date(invoice_date)
    if not d:
        return None
    try:
        return d + timedelta(days=int(terms_days or 30))
    except (TypeError, ValueError):
        return d + timedelta(days=30)


def age_bucket(due, today=None) -> str:
    d = parse_date(due)
    if not d:
        return "unknown"
    t = today or datetime.now(timezone.utc).date()
    days = (t - d).days
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


# ── vendor name matching ─────────────────────────────────────────────────────

_SUFFIXES = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"gmbh|plc|lp|llp|pllc|holdings|group|intl|international)\b\.?", re.I)


def norm_vendor(name: str) -> str:
    """Vendor names for comparison: lowercase, no punctuation, no corporate
    suffix. 'Company ABC Supply Co., Inc.' and 'Company ABC' become the
    same string."""
    t = (name or "").lower()
    t = re.sub(r"[&/]", " and ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = _SUFFIXES.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def match_vendor_name(raw: str, vendors, cutoff: float = 0.86):
    """Find our vendor record for someone else's spelling.

    Exact, then normalised-exact, then containment, then fuzzy. The cutoff is
    high on purpose: filing a document against the wrong vendor is far more
    expensive to unpick than leaving it unmatched for a human to place.
    """
    raw_s = (raw or "").strip()
    if not raw_s:
        return None
    raw_l = raw_s.lower()
    raw_n = norm_vendor(raw_s)

    for v in vendors:
        if (v.get("name") or "").strip().lower() == raw_l:
            return v
    if raw_n:
        for v in vendors:
            if norm_vendor(v.get("name")) == raw_n:
                return v
            if v.get("qbo_vendor_name") and norm_vendor(v["qbo_vendor_name"]) == raw_n:
                return v
        for v in vendors:
            n = norm_vendor(v.get("name"))
            if n and len(n) > 3 and (n in raw_n or raw_n in n):
                return v

    names = {norm_vendor(v.get("name")): v for v in vendors if norm_vendor(v.get("name"))}
    hit = difflib.get_close_matches(raw_n, list(names), n=1, cutoff=cutoff)
    return names[hit[0]] if hit else None


def match_vendor_by_email(from_email: str, subject: str, vendors):
    """Which vendor sent this.

    Address first — exact, then the sending domain — because an address is a
    fact and a subject line is a habit. Only then the vendor name appearing in
    the subject, and only for names long enough not to collide by accident.
    """
    frm = (from_email or "").strip().lower()
    dom = frm.split("@")[-1] if "@" in frm else ""
    sub = (subject or "").lower()

    for key in ("invoice_email", "statement_email", "request_email"):
        for v in vendors:
            a = (v.get(key) or "").strip().lower()
            if a and a == frm:
                return v
    if dom and "." in dom:
        for key in ("invoice_email", "statement_email", "request_email"):
            for v in vendors:
                a = (v.get(key) or "").strip().lower()
                if a and "@" in a and a.split("@")[-1] == dom:
                    return v
    for v in vendors:
        n = (v.get("name") or "").strip().lower()
        if len(n) > 3 and n in sub:
            return v
    return None


def classify_document(vendor, subject: str, filename: str) -> str:
    """Invoice, statement, credit or unknown.

    The vendor's own subject phrases win, because whoever set the vendor up
    knows how that supplier words things. Only then the obvious keywords, and
    then 'unknown' rather than a guess — a misfiled statement costs more to
    find than one flagged for a glance.
    """
    hay = f"{(subject or '').lower()} {(filename or '').lower()}"

    if vendor:
        st = (vendor.get("statement_subject") or "").strip().lower()
        iv = (vendor.get("invoice_subject") or "").strip().lower()
        if st and st in hay:
            return "statement"
        if iv and iv in hay:
            return "invoice"

    if re.search(r"\bcredit\s*(note|memo)\b|\bcr\s*note\b", hay):
        return "credit"
    if "remittance" in hay:
        return "remittance"
    if "statement" in hay or "soa" in hay:
        return "statement"
    if "invoice" in hay or re.search(r"\binv[-_ ]?\d", hay):
        return "invoice"
    return "unknown"


def safe_filename(name: str) -> str:
    t = re.sub(r"[^\w .\-&()]", "_", str(name or "")).strip()
    return (t or "file")[:160]
