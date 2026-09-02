import calendar
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


# ============================================================
# Configuration
# ============================================================

DATA_DIR = Path(__file__).resolve().parent
EXPENSES_DIR = DATA_DIR / "expenses"
STATEMENTS_DIR = DATA_DIR / "statements_store"

EXPENSES_DIR.mkdir(exist_ok=True)
STATEMENTS_DIR.mkdir(exist_ok=True)

LEDGER_PATH = STATEMENTS_DIR / "ledger.csv"
MANIFEST_PATH = STATEMENTS_DIR / "manifest.json"
CATEGORY_GROUPS_PATH = STATEMENTS_DIR / "category_groups.json"
BUDGETS_PATH = STATEMENTS_DIR / "budgets.json"
BANK_TEMPLATES_PATH = STATEMENTS_DIR / "bank_templates.json"
STATEMENT_CACHES_PATH = STATEMENTS_DIR / "statement_caches.json"

LEDGER_COLUMNS = ["Date", "Description", "Amount", "Category", "SourceFile", "SourceHash", "CacheId"]

UNCATEGORIZED = "Uncategorized"
CURRENCY_SYMBOL = "R"


# ============================================================
# Storage helpers — keyword cache lives in expenses/*.txt (one file
# per category), matching the original CLI tool. All ingested
# statements accumulate in a single persistent ledger
# (statements_store/ledger.csv) so uploads are remembered across
# sessions, and statements_store/manifest.json tracks which files
# have already been ingested so re-uploading the same file is a no-op.
# ============================================================

def list_categories() -> dict:
    categories = {}
    for path in sorted(EXPENSES_DIR.glob("*.txt")):
        keywords = [
            line.strip().upper()
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        categories[path.stem] = keywords
    return categories


def save_category(name: str, keywords: list) -> None:
    unique = sorted({k.strip().upper() for k in keywords if k.strip()})
    content = "\n".join(unique) + ("\n" if unique else "")
    (EXPENSES_DIR / f"{name}.txt").write_text(content)


def delete_category(name: str) -> None:
    path = EXPENSES_DIR / f"{name}.txt"
    if path.exists():
        path.unlink()


def categorize(description: str, categories: dict) -> str:
    """Longest-keyword-match categorization (same algorithm as the original CLI tool)."""
    return categorize_detailed(description, categories)[0]


def _keyword_matches(keyword: str, text: str) -> bool:
    """True if `keyword` appears in `text` as a whole token — not just as a
    substring buried inside a longer word (e.g. keyword "UBER" must not
    match inside "BLOUBERG"). Boundaries are anywhere the character before
    or after isn't a letter/digit, so this still matches keywords that
    contain spaces (e.g. "MUGG AND BEAN") or punctuation."""
    pattern = r"(?<![A-Z0-9])" + re.escape(keyword) + r"(?![A-Z0-9])"
    return re.search(pattern, text) is not None


def categorize_detailed(description: str, categories: dict):
    """Same matching logic as categorize(), but also returns which specific
    keyword won — used to explain *why* a transaction was (mis)matched."""
    text = str(description).upper()
    best, best_len, best_keyword = UNCATEGORIZED, 0, None

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword and len(keyword) > best_len and _keyword_matches(keyword, text):
                best, best_len, best_keyword = category, len(keyword), keyword

    return best, best_keyword


_KEYWORD_NOISE_RE = re.compile(
    r"\b(POS|PURCHASE|PAYMENT|APP|DEBIT|CREDIT|ORDER|EFT|IB|TRANSFER|FROM|TO)\b",
    re.IGNORECASE,
)
_KEYWORD_TRAILING_REF_RE = re.compile(
    r"\s+\d[\d\*]{3,}.*$"  # trailing card/reference numbers, e.g. "457896*5031"
    r"|\s+\d{1,2}\s+[A-Za-z]{3}\s*$"  # trailing "DD Mon" date
)


def suggest_keyword(description: str) -> str:
    """A short starting-point keyword for a description — strips reference
    numbers, trailing dates, and generic banking noise words, then keeps the
    first few remaining words. Meant to be reviewed/edited by the user, not
    used blindly."""
    text = description.strip()
    text = _KEYWORD_TRAILING_REF_RE.sub("", text)
    text = _KEYWORD_NOISE_RE.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    words = text.split()
    return " ".join(words[:3]).upper() if words else description.strip().upper()


def load_category_groups() -> dict:
    """Maps a category name -> the display label it's merged into for charts.
    A category with no entry is displayed under its own name. This only
    affects visualization; the underlying transaction Category (and the
    keyword cache) is untouched."""
    if not CATEGORY_GROUPS_PATH.exists():
        return {}
    try:
        return json.loads(CATEGORY_GROUPS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_category_groups(mapping: dict) -> None:
    CATEGORY_GROUPS_PATH.write_text(json.dumps(mapping, indent=2))


def apply_category_groups(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    df = df.copy()
    df["DisplayCategory"] = df["Category"].apply(lambda c: mapping.get(c, c))
    return df


def load_budgets() -> dict:
    """Maps a category or group label (as used in Category View) -> a
    monthly spending cap."""
    if not BUDGETS_PATH.exists():
        return {}
    try:
        return json.loads(BUDGETS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_budgets(budgets: dict) -> None:
    BUDGETS_PATH.write_text(json.dumps(budgets, indent=2))


def load_statement_caches() -> dict:
    if not STATEMENT_CACHES_PATH.exists():
        caches = {"main": {"name": "Main", "created_at": datetime.now().isoformat(timespec="seconds")}}
        save_statement_caches(caches)
        return caches
    try:
        data = json.loads(STATEMENT_CACHES_PATH.read_text())
        return data if isinstance(data, dict) and data else {"main": {"name": "Main"}}
    except (json.JSONDecodeError, OSError):
        return {"main": {"name": "Main"}}


def save_statement_caches(caches: dict) -> None:
    STATEMENT_CACHES_PATH.write_text(json.dumps(caches, indent=2))


def cache_name(cache_id: str) -> str:
    return load_statement_caches().get(cache_id, {}).get("name", cache_id)


def cache_id_from_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "cache"
    caches = load_statement_caches()
    candidate = base
    n = 2
    while candidate in caches:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def statements_in_cache(cache_id: str) -> list:
    return [h for h, info in load_manifest().items() if info.get("cache_id", "main") == cache_id]


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def load_ledger() -> pd.DataFrame:
    if not LEDGER_PATH.exists():
        return pd.DataFrame(columns=LEDGER_COLUMNS)

    df = pd.read_csv(LEDGER_PATH, parse_dates=["Date"])
    for col in LEDGER_COLUMNS:
        if col not in df.columns:
            df[col] = "" if col not in ("Amount",) else 0.0
    df["CacheId"] = df["CacheId"].fillna("main").astype(str)
    df.loc[df["CacheId"].str.strip() == "", "CacheId"] = "main"
    df = df[LEDGER_COLUMNS]

    # Self-heal any blank/corrupted cells (e.g. an empty CSV field reads
    # back as a float NaN) so a stray blank row can't silently break
    # string operations like sorted()/groupby() elsewhere in the app.
    df["Category"] = df["Category"].fillna(UNCATEGORIZED).astype(str)
    df.loc[df["Category"].str.strip() == "", "Category"] = UNCATEGORIZED
    df["Description"] = df["Description"].fillna("").astype(str)
    df["SourceFile"] = df["SourceFile"].fillna("").astype(str)
    df["SourceHash"] = df["SourceHash"].fillna("").astype(str)
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["Date"])

    return df


def save_ledger(df: pd.DataFrame) -> None:
    df = df[LEDGER_COLUMNS].sort_values("Date").reset_index(drop=True)
    df.to_csv(LEDGER_PATH, index=False)


# ============================================================
# Statement parsing / bank templates
#
# Every parser returns the same canonical transaction columns:
# Date, Description, Amount. Built-in parsers cover FNB, ABSA and
# Capitec. User-created templates are stored as JSON so new banks
# can be added without changing Python code.
# ============================================================

BUILTIN_BANKS = {
    "FNB": "fnb",
    "ABSA": "absa",
    "Capitec": "capitec",
}


def _default_templates() -> dict:
    return {
        "fnb": {"name": "FNB", "builtin": True, "parser": "fnb", "row_style": "single_amount", "date_format": "%d %b %Y", "date_pattern": r"\d{2} [A-Za-z]{3}", "continuation_lines": True},
        "absa": {"name": "ABSA", "builtin": True, "parser": "absa", "row_style": "single_amount", "date_format": "%Y-%m-%d", "date_pattern": r"\d{4}-\d{2}-\d{2}", "continuation_lines": True},
        "capitec": {"name": "Capitec", "builtin": True, "parser": "capitec", "row_style": "money_in_out", "date_format": "%d/%m/%Y", "date_pattern": r"\d{2}/\d{2}/\d{4}", "continuation_lines": True},
    }


def load_custom_templates() -> dict:
    defaults = _default_templates()
    if not BANK_TEMPLATES_PATH.exists():
        save_custom_templates(defaults)
        return defaults
    try:
        data = json.loads(BANK_TEMPLATES_PATH.read_text())
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}
    changed = False
    for template_id, default in defaults.items():
        if template_id not in data:
            data[template_id] = default
            changed = True
        else:
            # Upgrade older custom/builtin records without overwriting edits.
            for key, value in default.items():
                if key not in data[template_id]:
                    data[template_id][key] = value
                    changed = True
    if changed:
        save_custom_templates(data)
    return data


def save_custom_templates(templates: dict) -> None:
    BANK_TEMPLATES_PATH.write_text(json.dumps(templates, indent=2))


def list_bank_templates() -> dict:
    """Returns display name -> template metadata for every active template."""
    result = {}
    for template_id, template in load_custom_templates().items():
        if template.get("deleted", False):
            continue
        result[template.get("name", template_id)] = {"id": template_id, **template}
    return result



def _parse_money(value: str):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    negative = "-" in text or text.startswith("(")
    cleaned = re.sub(r"[^0-9.]", "", text.replace(",", "").replace(" ", ""))
    if not cleaned:
        return None
    amount = float(cleaned)
    return -amount if negative else amount


# A properly-grouped money token: 1-3 digits, then any number of
# comma/space-separated 3-digit groups, then a 2-decimal amount. This
# matters more than it looks — a naive "[\d, ]+\.\d{2}" (digits/commas/
# spaces, unbounded) also matches things it shouldn't, like a stray
# card-number fragment sitting right before a real amount in the
# description (e.g. "Card •••• 7977   0.00" gets misread as one number
# "79770.00"). Requiring proper 3-digit grouping after the first group
# rules that out, since raw description digits practically never fall
# into a valid thousands-grouping pattern.
_MONEY_TOKEN = r"\d{1,3}(?:[ ,]\d{3})*\.\d{2}"


def _pick_money_in_out(money_in_raw: str, money_out_raw: str):
    """Resolve a Money-In/Money-Out row pair to one signed amount.

    Some banks (e.g. Capitec) leave the unused column blank — there,
    "is this column present at all" reliably means "is this the one that
    happened". Others (e.g. GoTyme) always print BOTH columns and put a
    literal 0.00 in whichever one doesn't apply — there, "present" is
    true for both columns on every row, so presence alone can't
    distinguish them. Treating an explicit 0.00 the same as "absent"
    handles both conventions correctly, and a real (if unusual) 0.00
    transaction — both columns present and both genuinely zero — still
    resolves to 0.0 rather than being dropped as invalid.
    """
    money_in = _parse_money(money_in_raw)
    money_out = _parse_money(money_out_raw)
    if money_in:
        return money_in
    if money_out:
        return -money_out
    if money_in is not None or money_out is not None:
        return 0.0
    return None


def _normalise_description(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


@st.cache_data(show_spinner=False)
def parse_fnb_statement_pdf(pdf_bytes: bytes):
    """Parse the current FNB PDF layout."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("PDF statement parsing requires pdfplumber.") from exc

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)

    period_match = re.search(
        r"Statement Period\s*:\s*\d{1,2} (\w+) (\d{4}) to\s*\d{1,2} (\w+) (\d{4})",
        text,
    )
    if not period_match:
        raise ValueError(
            "Could not find an FNB 'Statement Period' line. "
            "Select another bank template or create a new template."
        )

    start_month, start_year, end_month, end_year = period_match.groups()
    start_year, end_year = int(start_year), int(end_year)

    row_re = re.compile(
        r"^(?P<date>\d{2} [A-Za-z]{3})\s+(?P<desc>.+?)\s+"
        r"(?P<amount>[\d,]+\.\d{2})(?P<credit>Cr)?\s+"
        r"(?P<balance>[\d,]+\.\d{2})(?:Cr|Dr)?\s*"
        r"(?:[\d,]+\.\d{2})?\s*$"
    )

    records, invalid_rows = [], 0

    for line in text.splitlines():
        match = row_re.match(line.strip())
        if not match:
            continue

        month_abbr = match.group("date").split()[1]
        year = end_year if month_abbr.lower() == end_month[:3].lower() else start_year
        date = pd.to_datetime(
            f"{match.group('date')} {year}",
            format="%d %b %Y",
            errors="coerce",
        )
        amount = _parse_money(match.group("amount"))

        if pd.isna(date) or amount is None:
            invalid_rows += 1
            continue

        if not match.group("credit"):
            amount = -amount

        records.append(
            {
                "Date": date,
                "Description": _normalise_description(match.group("desc")),
                "Amount": amount,
            }
        )

    return pd.DataFrame(records, columns=["Date", "Description", "Amount"]), invalid_rows


@st.cache_data(show_spinner=False)
def parse_absa_statement_pdf(pdf_bytes: bytes):
    """Parse ABSA's date / description / signed amount / balance layout."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("PDF statement parsing requires pdfplumber.") from exc

    line_re = re.compile(
        r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s+"
        r"(?P<desc>.*?)\s+"
        r"(?P<amount>[+-]?R\s?[\d ]+\.\d{2})\s+"
        r"(?P<balance>R\s?[\d ]+\.\d{2})\s*$"
    )

    records, invalid_rows = [], 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)

    current = None

    for line in text.splitlines():
        match = line_re.match(line)
        if match:
            if current:
                records.append(current)

            date = pd.to_datetime(match.group("date"), format="%Y-%m-%d", errors="coerce")
            amount = _parse_money(match.group("amount"))

            if pd.isna(date) or amount is None:
                invalid_rows += 1
                current = None
                continue

            current = {
                "Date": date,
                "Description": _normalise_description(match.group("desc")),
                "Amount": amount,
            }
            continue

        if current and line.strip():
            stripped = line.strip()
            footer_or_header = re.search(
                r"(Page\s+\d+\s+of\s+\d+|eStamp|Ref:|Transaction History|"
                r"Statement for the Period|Balance Brought Forward|Balance Carried Forward|Current Balance|"
                r"Available Balance|Uncleared Cheques)",
                stripped,
                re.IGNORECASE,
            )
            if not footer_or_header:
                current["Description"] = _normalise_description(
                    current["Description"] + " " + stripped
                )

    if current:
        records.append(current)

    return pd.DataFrame(records, columns=["Date", "Description", "Amount"]), invalid_rows


@st.cache_data(show_spinner=False)
def parse_capitec_statement_pdf(pdf_bytes: bytes):
    """Parse Capitec's two-date + description + money-in/out + balance layout."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("PDF statement parsing requires pdfplumber.") from exc

    records, invalid_rows = [], 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = pdf.pages
        column_positions = None

        for page in pages:
            words = page.extract_words()

            # The header may only appear on the first page. Detect the column
            # positions once and reuse them for later pages.
            if column_positions is None and any(w["text"] == "Posting" for w in words):
                def x_for(text_value):
                    matches = [w["x0"] for w in words if w["text"] == text_value]
                    return min(matches) if matches else None

                x_transaction = x_for("Transaction")
                x_description = x_for("Description")
                money_x = sorted(w["x0"] for w in words if w["text"] == "Money")
                balance_x = x_for("Balance")

                if (
                    x_transaction is not None
                    and x_description is not None
                    and balance_x is not None
                    and len(money_x) >= 2
                ):
                    column_positions = (
                        x_transaction,
                        x_description,
                        money_x[0],
                        money_x[1],
                        balance_x,
                    )

            if column_positions is None:
                continue

            (
                x_transaction,
                x_description,
                money_in_x,
                money_out_x,
                balance_x,
            ) = column_positions

            # Group words into visual rows.
            rows = {}
            for word in words:
                key = round(word["top"], 1)
                rows.setdefault(key, []).append(word)

            current = None
            for _, row_words in sorted(rows.items()):
                row_words = sorted(row_words, key=lambda w: w["x0"])
                row_text = " ".join(w["text"] for w in row_words)
                if not re.match(r"^\d{2}/\d{2}/\d{4}", row_text):
                    if current and row_text.strip():
                        # Continuation rows belong to the preceding transaction.
                        footer_or_header = re.search(
                            r"(Page\s+\d+\s+of\s+\d+|Capitec Bank|Transactions not yet processed|Amount\s+\(R\)|Date\s+Description|^E\s*nd$|"
                            r"Unique Document|24hr Client Care|Tax Invoice)",
                            row_text,
                            re.IGNORECASE,
                        )
                        if not footer_or_header:
                            current["Description"] = _normalise_description(
                                current["Description"] + " " + row_text
                            )
                    continue

                if current:
                    records.append(current)

                date_words = [
                    w for w in row_words
                    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", w["text"])
                ]
                if len(date_words) < 2:
                    invalid_rows += 1
                    current = None
                    continue

                date = pd.to_datetime(
                    date_words[1]["text"], format="%d/%m/%Y", errors="coerce"
                )

                # Numeric values are classified by their x position:
                # Money In, Money Out, then Balance.
                in_parts, out_parts, balance_parts = [], [], []
                description_parts = []

                for word in row_words:
                    text_value = word["text"]
                    x = word["x0"]

                    if word in date_words:
                        continue
                    if x >= balance_x:
                        balance_parts.append(text_value)
                    elif x >= money_out_x:
                        out_parts.append(text_value)
                    elif x >= money_in_x:
                        in_parts.append(text_value)
                    elif x >= x_description:
                        description_parts.append(text_value)

                money_in = _parse_money(" ".join(in_parts))
                money_out = _parse_money(" ".join(out_parts))

                if pd.isna(date) or (money_in is None and money_out is None):
                    invalid_rows += 1
                    current = None
                    continue

                amount = money_in if money_in is not None else -money_out
                current = {
                    "Date": date,
                    "Description": _normalise_description(" ".join(description_parts)),
                    "Amount": amount,
                }

            if current:
                records.append(current)

    return pd.DataFrame(records, columns=["Date", "Description", "Amount"]), invalid_rows


def _parse_custom_statement(pdf_bytes: bytes, template: dict):
    """Generic template parser.

    The custom format intentionally uses simple regular expressions rather
    than Python code. This keeps user-created templates portable and safe:
    a template describes the date, description and money fields but cannot
    execute arbitrary code.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("PDF statement parsing requires pdfplumber.") from exc

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)

    row_style = template.get("row_style", "single_amount")
    date_pattern = template.get("date_pattern", r"\d{4}-\d{2}-\d{2}")
    date_format = template.get("date_format", "%Y-%m-%d")
    continuation = bool(template.get("continuation_lines", True))

    if row_style == "money_in_out":
        row_re = re.compile(
            rf"^\s*(?P<date>{date_pattern})\s+(?P<desc>.*?)"
            r"(?:\s{2,}|\s+)"
            rf"(?:(?P<money_in>{_MONEY_TOKEN})\s+)?"
            rf"(?:(?P<money_out>{_MONEY_TOKEN})\s+)?"
            rf"(?P<balance>[+-]?{_MONEY_TOKEN})\s*$"
        )
    else:
        row_re = re.compile(
            rf"^\s*(?P<date>{date_pattern})\s+(?P<desc>.*?)\s+"
            rf"(?P<amount>[+-]?R?\s?{_MONEY_TOKEN})"
            rf"(?:\s+(?P<balance>[+-]?R?\s?{_MONEY_TOKEN}))?\s*$"
        )

    records, invalid_rows = [], 0
    current = None
    current_continuations = 0
    # Real wrapped description text (e.g. a reference number spilling onto
    # its own line) is almost always exactly one extra line. Statements
    # with a full page of contact info/disclaimers *after* the transaction
    # table (GoTyme does this) would otherwise get all of that glued onto
    # the last transaction's description forever, since nothing else
    # signals "the table has ended".
    max_continuation_lines = 1

    for line in text.splitlines():
        match = row_re.match(line)
        if match:
            if current:
                records.append(current)

            date = pd.to_datetime(
                match.group("date"), format=date_format, errors="coerce"
            )

            if row_style == "money_in_out":
                amount = _pick_money_in_out(
                    match.group("money_in"), match.group("money_out")
                )
            else:
                amount = _parse_money(match.group("amount"))

            if pd.isna(date) or amount is None:
                invalid_rows += 1
                current = None
                continue

            current = {
                "Date": date,
                "Description": _normalise_description(match.group("desc")),
                "Amount": amount,
            }
            current_continuations = 0
        elif (
            current
            and continuation
            and line.strip()
            and current_continuations < max_continuation_lines
        ):
            current["Description"] = _normalise_description(
                current["Description"] + " " + line.strip()
            )
            current_continuations += 1

    if current:
        records.append(current)

    return pd.DataFrame(records, columns=["Date", "Description", "Amount"]), invalid_rows


# Candidates tried in order when auto-detecting a date format from a sample
# statement. Several formats can share the exact same regex shape (e.g.
# "08-22-2024" could be DD-MM-YYYY or MM-DD-YYYY) — those are disambiguated
# in analyze_sample_statement() by which one actually parses successfully
# on the real dates found, not by listing order.
_DATE_PATTERN_CANDIDATES = [
    (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    (r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y"),
    (r"\d{2}/\d{2}/\d{4}", "%m/%d/%Y"),
    (r"\d{2}-\d{2}-\d{4}", "%d-%m-%Y"),
    (r"\d{2}-\d{2}-\d{4}", "%m-%d-%Y"),
    (r"\d{2}\.\d{2}\.\d{4}", "%d.%m.%Y"),
    (r"\d{2} [A-Za-z]{3} \d{4}", "%d %b %Y"),
    (r"\d{2} [A-Za-z]{3}", "%d %b"),
]


def _score_row_style(text: str, date_pattern: str, date_format: str, row_style: str):
    """Parses `text` with one candidate row layout and scores it by how often
    consecutive transactions' balances actually reconcile (Balance[i-1] +
    Amount[i] == Balance[i]) — a much more reliable signal than raw match
    count. Match count alone can't tell a correct layout from a wrong one:
    e.g. a statement with separate Money In / Money Out columns will still
    produce plenty of "matches" if mis-read as a single signed Amount column
    (whichever of the two figures appears is just read as positive), but
    those amounts will almost never reconcile against the balance."""
    if row_style == "money_in_out":
        row_re = re.compile(
            rf"^\s*(?P<date>{date_pattern})\s+(?P<desc>.*?)"
            r"(?:\s{2,}|\s+)"
            rf"(?:(?P<money_in>{_MONEY_TOKEN})\s+)?"
            rf"(?:(?P<money_out>{_MONEY_TOKEN})\s+)?"
            rf"(?P<balance>[+-]?{_MONEY_TOKEN})\s*$"
        )
    else:
        row_re = re.compile(
            rf"^\s*(?P<date>{date_pattern})\s+(?P<desc>.*?)\s+"
            rf"(?P<amount>[+-]?R?\s?{_MONEY_TOKEN})"
            rf"(?:\s+(?P<balance>[+-]?R?\s?{_MONEY_TOKEN}))?\s*$"
        )

    amount_balance_pairs = []
    for line in text.splitlines():
        match = row_re.match(line)
        if not match:
            continue

        if row_style == "money_in_out":
            amount = _pick_money_in_out(match.group("money_in"), match.group("money_out"))
        else:
            amount = _parse_money(match.group("amount"))

        balance = _parse_money(match.groupdict().get("balance"))
        if amount is None or balance is None:
            continue
        amount_balance_pairs.append((amount, balance))

    row_count = len(amount_balance_pairs)
    if row_count < 2:
        return row_count, 0.0

    reconciled = sum(
        1
        for i in range(1, row_count)
        if abs((amount_balance_pairs[i - 1][1] + amount_balance_pairs[i][0]) - amount_balance_pairs[i][1]) < 0.01
    )
    return row_count, reconciled / (row_count - 1)


def analyze_sample_statement(pdf_bytes: bytes) -> dict:
    """Best-effort inspection of an uploaded sample statement to pre-fill
    the generic template form: which date pattern/format actually shows up
    at the start of transaction lines, and whether the statement uses a
    single signed Amount column or separate Money In / Money Out columns.
    Never guesses at parser-breaking specifics (currency symbol, thousands
    separator, sign convention) since the generic amount regex already
    handles those flexibly. The user can still edit anything afterward."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("PDF statement parsing requires pdfplumber.") from exc

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)

    # Pick the (pattern, format) pair with the most dates that actually
    # parse successfully — not just the most regex matches. Several
    # candidates share the same digit shape (DD-MM-YYYY vs MM-DD-YYYY,
    # DD/MM/YYYY vs MM/DD/YYYY); counting valid parses is what tells them
    # apart, since the wrong interpretation usually produces some
    # impossible date (e.g. month 22) as soon as a day-of-month over 12
    # shows up anywhere in the sample.
    best_pattern, best_format, best_valid_count = None, None, 0
    for pattern, fmt in _DATE_PATTERN_CANDIDATES:
        line_re = re.compile(rf"^\s*(?P<d>{pattern})\s+\S")
        valid = 0
        for line in text.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            if pd.notna(pd.to_datetime(m.group("d"), format=fmt, errors="coerce")):
                valid += 1
        if valid > best_valid_count:
            best_pattern, best_format, best_valid_count = pattern, fmt, valid

    if best_valid_count < 3:
        return {"detected": False}

    scores = {
        style: _score_row_style(text, best_pattern, best_format, style)
        for style in ("single_amount", "money_in_out")
    }
    # A statement genuinely using separate Money In / Money Out columns
    # can't be told apart from a single-column one using flattened text
    # alone (there's no positional information left once columns collapse
    # into plain text) — both layouts tend to "match" a similar number of
    # rows. Reconciliation rate is what actually distinguishes them: the
    # correct layout's amounts add up against the balance far more often.
    row_style = max(scores, key=lambda style: scores[style][1])
    row_count, confidence = scores[row_style]

    if row_count < 3:
        return {"detected": False}

    return {
        "detected": True,
        "date_pattern": best_pattern,
        "date_format": best_format,
        "row_style": row_style,
        "transaction_count": row_count,
        "confidence": confidence,
    }


def parse_statement_pdf(pdf_bytes: bytes, template_id: str):
    template = load_custom_templates().get(template_id)
    if not template or template.get("deleted", False):
        raise ValueError("Statement template not found or has been deleted.")
    parser = template.get("parser", "custom")
    if parser == "fnb":
        return parse_fnb_statement_pdf(pdf_bytes)
    if parser == "absa":
        return parse_absa_statement_pdf(pdf_bytes)
    if parser == "capitec":
        return parse_capitec_statement_pdf(pdf_bytes)
    return _parse_custom_statement(pdf_bytes, template)


# ============================================================
# Ingest — parses a newly uploaded statement, skips it if it has
# already been ingested (by file hash), drops any transactions that
# duplicate ones already in the ledger (by Date + Description +
# Amount, e.g. from an overlapping statement period), then appends
# the rest to the persistent ledger.
# ============================================================

def ingest_statement(filename: str, file_bytes: bytes, template_id: str, cache_id: str = "main") -> dict:
    file_hash = hash_bytes(file_bytes)
    manifest = load_manifest()

    # Manifest keys are scoped per cache (cache_id + file hash) so the same
    # physical statement can be uploaded into more than one cache without
    # tripping the "already uploaded" check for caches it hasn't touched yet.
    manifest_key = f"{cache_id}::{file_hash}"

    if manifest_key in manifest:
        return {"status": "already_uploaded", "filename": filename}

    try:
        raw_df, invalid_rows = parse_statement_pdf(file_bytes, template_id)
    except Exception as exc:
        return {"status": "error", "filename": filename, "template_id": template_id, "error": str(exc)}

    if raw_df.empty:
        return {"status": "empty", "filename": filename}

    categories = list_categories()
    raw_df = raw_df.copy()
    raw_df["Category"] = raw_df["Description"].apply(lambda d: categorize(d, categories))
    raw_df["SourceFile"] = filename
    raw_df["SourceHash"] = file_hash
    raw_df["CacheId"] = cache_id

    ledger = load_ledger()

    # Row-level duplicate detection (e.g. overlapping statement periods)
    # is scoped to this cache's own transactions — a matching Date +
    # Description + Amount in a *different* cache is a coincidence, not
    # a duplicate, and shouldn't cause a row to be dropped here.
    ledger_in_cache = ledger[ledger["CacheId"] == cache_id]
    existing_keys = set(
        zip(ledger_in_cache["Date"], ledger_in_cache["Description"], ledger_in_cache["Amount"])
    )
    is_duplicate = raw_df.apply(
        lambda r: (r["Date"], r["Description"], r["Amount"]) in existing_keys, axis=1
    )
    duplicate_count = int(is_duplicate.sum())
    new_rows = raw_df[~is_duplicate]

    combined = pd.concat([ledger, new_rows], ignore_index=True)
    save_ledger(combined)

    manifest[manifest_key] = {
        "filename": filename,
        "bank_template": template_id,
        "cache_id": cache_id,
        "file_hash": file_hash,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "rows_added": int(len(new_rows)),
        "rows_duplicate": duplicate_count,
        "invalid_rows": int(invalid_rows),
    }
    save_manifest(manifest)

    return {
        "status": "ok",
        "filename": filename,
        "bank_template": template_id,
        "rows_added": int(len(new_rows)),
        "rows_duplicate": duplicate_count,
        "invalid_rows": int(invalid_rows),
    }


def remove_statement(manifest_key: str) -> None:
    manifest = load_manifest()
    info = manifest.pop(manifest_key, None) or {}
    save_manifest(manifest)

    # Older manifest entries (from before per-cache keys) were keyed
    # directly by file hash and have no "file_hash"/"cache_id" fields —
    # fall back to treating the key itself as the hash in that case.
    file_hash = info.get("file_hash", manifest_key)
    cache_id = info.get("cache_id", "main")

    ledger = load_ledger()
    ledger = ledger[~((ledger["SourceHash"] == file_hash) & (ledger["CacheId"] == cache_id))]
    save_ledger(ledger)


def remove_cache(cache_id: str) -> None:
    if cache_id == "main":
        raise ValueError("The Main cache cannot be deleted.")
    manifest = load_manifest()
    manifest_keys = statements_in_cache(cache_id)
    for manifest_key in manifest_keys:
        manifest.pop(manifest_key, None)
    save_manifest(manifest)
    ledger = load_ledger()
    if not ledger.empty:
        ledger = ledger[ledger["CacheId"] != cache_id]
        save_ledger(ledger)
    caches = load_statement_caches()
    caches.pop(cache_id, None)
    save_statement_caches(caches)


def rename_cache(cache_id: str, name: str) -> None:
    caches = load_statement_caches()
    if cache_id not in caches:
        raise ValueError("Statement cache not found.")
    if not name.strip():
        raise ValueError("Cache name cannot be empty.")
    caches[cache_id]["name"] = name.strip()
    save_statement_caches(caches)


def recategorize_uncategorized(categories: dict) -> int:
    """Re-applies the given keyword cache to every Uncategorized transaction
    in the ledger. Categorization normally only happens once, at upload
    time — if a keyword gets added to the cache afterward, already-ingested
    transactions never automatically pick it up otherwise. Never touches a
    row that already has a category assigned, so manual corrections are
    left alone. Returns the number of rows that got a category."""
    ledger_df = load_ledger()
    mask = ledger_df["Category"] == UNCATEGORIZED
    if not mask.any():
        return 0

    predicted = ledger_df.loc[mask, "Description"].apply(
        lambda d: categorize(d, categories)
    )
    fixed = int((predicted != UNCATEGORIZED).sum())

    if fixed:
        ledger_df.loc[mask, "Category"] = predicted
        save_ledger(ledger_df)

    return fixed


# ============================================================
# Page setup
# ============================================================

st.set_page_config(page_title="Finance Illustrator", page_icon="💰", layout="wide")

st.title("💰 Finance Illustrator")
st.caption(
    "Categorizes bank statement transactions against a keyword cache and "
    "visualizes spending trends. Upload as many statements as you like — "
    "they're remembered and merged into one running history."
)

tab_dashboard, tab_budgets, tab_keywords, tab_templates = st.tabs(
    ["📈 Dashboard", "🎯 Budgets", "🏷️ Keyword Cache", "🏦 Statement Templates"]
)


# ============================================================
# Sidebar — statement upload / cache selection
# ============================================================

with st.sidebar:
    st.header("Statement Caches")
    caches = load_statement_caches()
    cache_options = {v.get("name", k): k for k, v in caches.items()}
    sorted_cache_names = sorted(cache_options)

    # Widgets can't have their session_state key reassigned after they're
    # instantiated in the same run (Streamlit raises a StreamlitAPIException).
    # So the create/rename/delete actions below stage the desired selection
    # under a separate "_pending_active_cache_name" key and rerun; we apply
    # that staged value here, before the selectbox is created, which is the
    # one point where updating a widget's key via session_state is allowed.
    pending_cache_name = st.session_state.pop("_pending_active_cache_name", None)
    if pending_cache_name and pending_cache_name in sorted_cache_names:
        st.session_state["active_cache_name"] = pending_cache_name
    elif (
        "active_cache_name" not in st.session_state
        or st.session_state["active_cache_name"] not in sorted_cache_names
    ):
        st.session_state["active_cache_name"] = (
            sorted_cache_names[0] if sorted_cache_names else None
        )

    active_cache_name = st.selectbox(
        "Active statement cache",
        options=sorted_cache_names,
        key="active_cache_name",
        help="Only statements assigned to this cache are shown in the dashboard."
    )
    active_cache_id = cache_options[active_cache_name]
    st.session_state["active_cache_id"] = active_cache_id

    with st.expander("Manage statement caches"):
        new_cache_name = st.text_input("New cache name", key="new_cache_name")
        if st.button("Create cache", key="create_cache"):
            if not new_cache_name.strip():
                st.error("Enter a cache name.")
            elif new_cache_name.strip() in cache_options:
                st.error("A cache with that name already exists.")
            else:
                new_id = cache_id_from_name(new_cache_name)
                caches[new_id] = {"name": new_cache_name.strip(), "created_at": datetime.now().isoformat(timespec="seconds")}
                save_statement_caches(caches)
                st.session_state["_pending_active_cache_name"] = new_cache_name.strip()
                st.rerun()

        rename_name = st.text_input("Rename active cache", value=active_cache_name, key="rename_cache_name")
        if st.button("Rename cache", key="rename_cache"):
            if rename_name.strip() and rename_name.strip() != active_cache_name:
                rename_cache(active_cache_id, rename_name)
                st.session_state["_pending_active_cache_name"] = rename_name.strip()
                st.rerun()

        cache_count = len(statements_in_cache(active_cache_id))
        st.caption(f"{cache_count} statement(s) in **{active_cache_name}**.")
        if active_cache_id != "main":
            if st.button("🗑️ Delete this cache", key="delete_cache"):
                remove_cache(active_cache_id)
                st.session_state["_pending_active_cache_name"] = "Main"
                st.rerun()

    st.divider()
    st.header("Statements")

    bank_templates = list_bank_templates()
    if not bank_templates:
        st.warning("No active statement templates. Create one in the Statement Templates tab.")
    else:
        bank_choice = st.selectbox(
            "Bank / statement template",
            options=sorted(bank_templates.keys()),
            help="Choose the bank format that matches the uploaded statement.",
        )
        selected_template_id = bank_templates[bank_choice]["id"]

        uploaded_files = st.file_uploader(
            f"Upload statement PDF(s) to '{active_cache_name}'",
            type=["pdf"],
            accept_multiple_files=True,
            help="Each uploaded statement is assigned to the active cache."
        )

        if uploaded_files:
            for f in uploaded_files:
                result = ingest_statement(f.name, f.getvalue(), selected_template_id, active_cache_id)
                if result["status"] == "ok":
                    msg = f"**{result['filename']}** ({bank_choice}) → **{active_cache_name}** — added {result['rows_added']} transaction(s)."
                    if result["rows_duplicate"]:
                        msg += f" Skipped {result['rows_duplicate']} already in your history."
                    if result["invalid_rows"]:
                        msg += f" ({result['invalid_rows']} row(s) unreadable and skipped.)"
                    st.success(msg)
                elif result["status"] == "already_uploaded":
                    st.info(f"**{result['filename']}** was already uploaded — skipped.")
                elif result["status"] == "empty":
                    st.warning(f"**{result['filename']}**: no valid transactions found.")
                else:
                    st.error(f"**{result['filename']}**: {result['error']}")

    manifest = load_manifest()
    cache_manifest_keys = statements_in_cache(active_cache_id)
    if cache_manifest_keys:
        st.divider()
        st.caption(f"{len(cache_manifest_keys)} statement(s) in '{active_cache_name}'")
        with st.expander("Manage statements in this cache"):
            for manifest_key in sorted(
                cache_manifest_keys, key=lambda k: manifest[k].get("uploaded_at", "")
            ):
                info = manifest[manifest_key]
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.write(
                        f"**{info['filename']}**  \n"
                        f"{info.get('bank_template', 'unknown')} · "
                        f"{info.get('rows_added', 0)} txns · "
                        f"uploaded {info.get('uploaded_at', '')[:10]}"
                    )
                with col2:
                    if st.button("🗑️", key=f"del_{manifest_key}", help="Remove this statement from the cache"):
                        remove_statement(manifest_key)
                        st.rerun()

    st.divider()
    st.caption(
        "Statement caches are named collections of uploaded statements. "
        "Each cache has its own transaction history, while templates describe "
        "how bank statements are parsed."
    )

# ============================================================
# Dashboard tab
# ============================================================

with tab_dashboard:
    ledger = load_ledger()
    active_cache_id = st.session_state.get("active_cache_id", "main")
    ledger = ledger[ledger["CacheId"] == active_cache_id].copy()

    if ledger.empty:
        st.info("Choose a bank template and upload one or more statement PDFs using the sidebar to begin.")
    else:
        # The full ledger (across every uploaded statement) lives on disk;
        # session state only holds in-progress edits made in this session.
        if "transactions" not in st.session_state or st.session_state.get(
            "_ledger_len"
        ) != len(ledger):
            st.session_state["transactions"] = ledger
            st.session_state["_ledger_len"] = len(ledger)

        categories = list_categories()
        # Always include whatever Category values are already in the ledger,
        # even one whose keyword-cache file was since deleted or renamed —
        # otherwise a SelectboxColumn with a value outside its options list
        # silently blanks that cell out on the next edit.
        category_options = sorted(
            set(categories.keys()) | set(ledger["Category"].unique()) | {UNCATEGORIZED}
        )

        transactions = st.session_state["transactions"]

        # ---- KPI cards ----
        income = transactions.loc[transactions["Amount"] > 0, "Amount"].sum()
        expenses = -transactions.loc[transactions["Amount"] < 0, "Amount"].sum()
        net = income - expenses

        spend_by_category = (
            transactions[transactions["Amount"] < 0]
            .assign(Spend=lambda d: -d["Amount"])
            .groupby("Category")["Spend"]
            .sum()
            .sort_values(ascending=False)
        )

        top_category = spend_by_category.index[0] if not spend_by_category.empty else "—"
        top_category_amount = spend_by_category.iloc[0] if not spend_by_category.empty else 0

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("Income", f"{CURRENCY_SYMBOL} {income:,.2f}")
        with col2:
            st.metric("Expenses", f"{CURRENCY_SYMBOL} {expenses:,.2f}")
        with col3:
            st.metric("Net", f"{CURRENCY_SYMBOL} {net:,.2f}")
        with col4:
            st.metric(
                "Top Category",
                top_category,
                f"{CURRENCY_SYMBOL} {top_category_amount:,.2f}",
            )

        st.caption(
            f"Showing {len(transactions):,} transaction(s) across "
            f"{transactions['SourceFile'].nunique()} statement(s)."
        )

        st.divider()

        # ---- Cash flow overview (always the full, unfiltered history) ----
        st.subheader("Cash Flow Overview")

        monthly_flow = (
            transactions.assign(
                Month=transactions["Date"].dt.to_period("M").astype(str),
                Flow=lambda d: d["Amount"].where(d["Amount"] > 0, 0.0),
                Outflow=lambda d: (-d["Amount"]).where(d["Amount"] < 0, 0.0),
            )
            .groupby("Month")
            .agg(Income=("Flow", "sum"), Expenses=("Outflow", "sum"))
            .reset_index()
        )
        monthly_flow["Net"] = monthly_flow["Income"] - monthly_flow["Expenses"]

        cash_col1, cash_col2 = st.columns(2)

        with cash_col1:
            income_expense_long = monthly_flow.melt(
                id_vars="Month",
                value_vars=["Income", "Expenses"],
                var_name="Type",
                value_name="Amount",
            )
            fig_income_expense = px.bar(
                income_expense_long,
                x="Month",
                y="Amount",
                color="Type",
                barmode="group",
                title="Income vs Expenses by Month",
                color_discrete_map={"Income": "#2E8B57", "Expenses": "#C0392B"},
            )
            fig_income_expense.update_layout(yaxis_title=f"Amount ({CURRENCY_SYMBOL})")
            st.plotly_chart(fig_income_expense, use_container_width=True)

        with cash_col2:
            cumulative = transactions.sort_values("Date").copy()
            cumulative["Cumulative Net"] = cumulative["Amount"].cumsum()
            fig_cumulative = px.line(
                cumulative,
                x="Date",
                y="Cumulative Net",
                title="Cumulative Net Flow",
                markers=False,
            )
            fig_cumulative.update_layout(yaxis_title=f"Cumulative Net ({CURRENCY_SYMBOL})")
            st.plotly_chart(fig_cumulative, use_container_width=True)
            st.caption(
                "The running total of every transaction in your history, in order — "
                "shows whether your overall position is trending up or down. It's "
                "relative to your first uploaded transaction, not your real account "
                "balance."
            )

        if monthly_flow["Month"].nunique() > 1:
            avg_savings_rate = (
                monthly_flow["Net"].sum() / monthly_flow["Income"].sum() * 100
                if monthly_flow["Income"].sum()
                else 0
            )
            st.caption(
                f"Average savings rate across the months shown: **{avg_savings_rate:,.0f}%** "
                "of income (Net ÷ Income)."
            )

        st.divider()

        # ---- Editable transaction table ----
        st.subheader("Transactions")
        st.caption(
            "Fix the Category where needed — edits save to your history "
            "immediately. For any row the keyword cache doesn't yet "
            "recognize, a **Suggested Keyword** is pre-filled (trim it down "
            "to just the merchant name — reference numbers and dates won't "
            "repeat on future statements) along with **Why Flagged**, "
            "showing what it currently matches instead, if anything. Clear "
            "a suggestion to skip it, or fill one in for any row yourself, "
            "then save."
        )

        current_categories = list_categories()

        def _keyword_hint(row):
            category = row["Category"]
            desc = str(row["Description"]).strip()
            if not desc:
                return pd.Series({"Suggested Keyword": "", "Why Flagged": ""})

            predicted, matched_keyword = categorize_detailed(desc, current_categories)

            if category == UNCATEGORIZED:
                if predicted != UNCATEGORIZED:
                    explanation = (
                        f"Would now match '{matched_keyword}' → {predicted} — "
                        "use Recategorize above, or set the Category yourself."
                    )
                else:
                    explanation = "No keyword matches it yet"
                return pd.Series({"Suggested Keyword": "", "Why Flagged": explanation})

            if category not in current_categories:
                return pd.Series({"Suggested Keyword": "", "Why Flagged": ""})

            if predicted == category:
                return pd.Series({"Suggested Keyword": "", "Why Flagged": ""})

            explanation = (
                f"Currently matches '{matched_keyword}' → {predicted}"
                if matched_keyword
                else "No keyword matches it at all"
            )
            return pd.Series(
                {"Suggested Keyword": suggest_keyword(desc), "Why Flagged": explanation}
            )

        display_df = transactions.copy()
        display_df[["Suggested Keyword", "Why Flagged"]] = display_df.apply(
            _keyword_hint, axis=1
        )
        display_df = display_df[
            [
                "Date",
                "Description",
                "Amount",
                "Category",
                "Suggested Keyword",
                "Why Flagged",
                "SourceFile",
                "SourceHash",
            ]
        ]

        needs_attention = (display_df["Category"] == UNCATEGORIZED) | (
            display_df["Suggested Keyword"] != ""
        )
        focus_on_flagged = st.checkbox(
            f"Show only rows needing attention ({int(needs_attention.sum())})",
            value=False,
        )
        table_view = display_df[needs_attention] if focus_on_flagged else display_df

        edited_view = st.data_editor(
            table_view,
            column_config={
                "Date": st.column_config.DateColumn("Date", disabled=True),
                "Description": st.column_config.TextColumn("Description", disabled=True),
                "Amount": st.column_config.NumberColumn(
                    "Amount", format="%.2f", disabled=True
                ),
                "Category": st.column_config.SelectboxColumn(
                    "Category", options=category_options
                ),
                "Suggested Keyword": st.column_config.TextColumn(
                    "Suggested Keyword",
                    help="Edit down to just the merchant name, or clear to skip.",
                ),
                "Why Flagged": st.column_config.TextColumn(
                    "Why Flagged",
                    disabled=True,
                    help=(
                        "If this shows another category, a longer keyword "
                        "there is beating the match you'd expect — check "
                        "that category's keyword list for something too "
                        "broad (e.g. a short word that's a substring of an "
                        "unrelated name)."
                    ),
                ),
                "SourceFile": st.column_config.TextColumn("Statement", disabled=True),
                "SourceHash": None,  # hide the internal dedup key
            },
            use_container_width=True,
            hide_index=True,
            key="transactions_editor",
        )

        # Merge edits back by index so filtering to "needs attention" never
        # drops the rows that were hidden from view.
        edited = display_df.copy()
        edited.loc[edited_view.index] = edited_view

        # display_df only carries the columns shown/edited in the table above,
        # so CacheId (a real ledger column, just not displayed here) needs to
        # be reattached before touching LEDGER_COLUMNS — otherwise the
        # edited[LEDGER_COLUMNS] lookup below raises a KeyError.
        edited["CacheId"] = transactions["CacheId"]

        if not edited[LEDGER_COLUMNS].equals(transactions[LEDGER_COLUMNS]):
            save_ledger(edited[LEDGER_COLUMNS])

        st.session_state["transactions"] = edited[LEDGER_COLUMNS]
        transactions = edited[LEDGER_COLUMNS]

        if st.button("💾 Save keywords & recategorize"):
            updated_categories = list_categories()
            added = 0

            for _, row in edited.iterrows():
                keyword = str(row["Suggested Keyword"]).strip().upper()
                category = row["Category"]
                if not keyword or category == UNCATEGORIZED or category not in updated_categories:
                    continue
                if keyword not in updated_categories[category]:
                    updated_categories[category].append(keyword)
                    added += 1

            for category, keywords in updated_categories.items():
                save_category(category, keywords)

            # Always reapply the (possibly just-updated) keyword cache to
            # every Uncategorized transaction — not just ones matching a
            # keyword added this round — so this one button also covers
            # keywords that were added separately via the Keyword Cache tab.
            fixed = recategorize_uncategorized(updated_categories)

            if added or fixed:
                parts = []
                if added:
                    parts.append(f"added {added} new keyword(s) to the cache")
                if fixed:
                    parts.append(f"categorized {fixed} previously-uncategorized transaction(s)")
                st.success((", ".join(parts) + ".").capitalize())
                st.rerun()
            else:
                st.info("Nothing to save — no new keywords, and no uncategorized transactions match the current cache.")

        st.download_button(
            "Download categorized transactions as CSV",
            data=transactions.drop(columns=["SourceHash"]).to_csv(index=False).encode("utf-8"),
            file_name="categorized_transactions.csv",
            mime="text/csv",
        )

        st.divider()

        # ============================================================
        # Category view — filter to a subset of categories and/or
        # collapse several categories into one label, purely for these
        # charts. Nothing here touches the underlying Category column,
        # the ledger, or the keyword cache.
        # ============================================================

        st.subheader("Category View")
        st.caption(
            "Focus on particular month(s) and/or a subset of categories, and/or "
            "combine several categories into one group — all of this applies "
            "only to the charts below."
        )

        transactions["Month"] = transactions["Date"].dt.to_period("M").astype(str)
        all_months = sorted(transactions["Month"].unique())
        all_categories = sorted(transactions["Category"].unique())
        category_groups = load_category_groups()
        # Drop stale mappings for categories that no longer exist.
        category_groups = {
            c: g for c, g in category_groups.items() if c in all_categories
        }

        with st.expander("🎛️ Filter & group", expanded=False):
            month_col, filter_col, group_col = st.columns(3)

            with month_col:
                st.markdown("**Months**")
                selected_months = st.multiselect(
                    "Months to include",
                    options=all_months,
                    default=all_months,
                    key="month_filter",
                )

            with filter_col:
                st.markdown("**Categories**")
                selected_categories = st.multiselect(
                    "Categories to include",
                    options=all_categories,
                    default=all_categories,
                    key="category_filter",
                )

            with group_col:
                st.markdown("**Group for charts**")
                merge_targets = st.multiselect(
                    "Categories to combine",
                    options=all_categories,
                    key="merge_targets",
                )
                merge_label = st.text_input("Group label", key="merge_label")
                if st.button("Merge into group", disabled=not merge_targets):
                    if not merge_label.strip():
                        st.error("Give the group a label first.")
                    else:
                        for c in merge_targets:
                            category_groups[c] = merge_label.strip()
                        save_category_groups(category_groups)
                        st.rerun()

            active_groups = {}
            for cat, label in category_groups.items():
                active_groups.setdefault(label, []).append(cat)

            if active_groups:
                st.markdown("**Active groups**")
                for label, members in active_groups.items():
                    gcol1, gcol2 = st.columns([4, 1])
                    with gcol1:
                        st.write(f"**{label}** = {', '.join(sorted(members))}")
                    with gcol2:
                        if st.button("Ungroup", key=f"ungroup_{label}"):
                            for c in members:
                                category_groups.pop(c, None)
                            save_category_groups(category_groups)
                            st.rerun()

        if not selected_months or not selected_categories:
            st.warning("Select at least one month and one category above to see charts.")
        else:
            view_df = transactions[
                transactions["Month"].isin(selected_months)
                & transactions["Category"].isin(selected_categories)
            ]
            view_df = apply_category_groups(view_df, category_groups)

            if len(selected_months) < len(all_months) or len(
                selected_categories
            ) < len(all_categories):
                st.caption(
                    f"Showing {len(view_df):,} transaction(s) for "
                    f"{len(selected_months)} of {len(all_months)} month(s) and "
                    f"{len(selected_categories)} of {len(all_categories)} category(ies)."
                )

            spend_by_category = (
                view_df[view_df["Amount"] < 0]
                .assign(Spend=lambda d: -d["Amount"])
                .groupby("DisplayCategory")["Spend"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"DisplayCategory": "Category"})
            )

            # ---- Bar + pie ----
            st.subheader("Spending by Category")

            col1, col2 = st.columns(2)

            with col1:
                fig_bar = px.bar(
                    spend_by_category,
                    x="Category",
                    y="Spend",
                    text="Spend",
                    title="Spend by Category",
                )
                fig_bar.update_traces(
                    texttemplate=f"{CURRENCY_SYMBOL} %{{text:,.0f}}",
                    textposition="outside",
                )
                st.plotly_chart(fig_bar, use_container_width=True)

            with col2:
                fig_pie = px.pie(
                    spend_by_category,
                    names="Category",
                    values="Spend",
                    title="Spend Share",
                )
                st.plotly_chart(fig_pie, use_container_width=True)

            st.download_button(
                "Download category totals as CSV",
                data=spend_by_category.to_csv(index=False).encode("utf-8"),
                file_name="category_totals.csv",
                mime="text/csv",
            )

            # ---- Monthly trend (only meaningful across multiple months) ----
            if view_df["Month"].nunique() > 1:
                st.subheader("Monthly Spending Trend")

                monthly_spend = (
                    view_df[view_df["Amount"] < 0]
                    .assign(Spend=lambda d: -d["Amount"])
                    .groupby(["Month", "DisplayCategory"])["Spend"]
                    .sum()
                    .reset_index()
                )

                fig_trend = px.bar(
                    monthly_spend,
                    x="Month",
                    y="Spend",
                    color="DisplayCategory",
                    barmode="stack",
                    title="Spend by Month and Category",
                    labels={"DisplayCategory": "Category"},
                )
                fig_trend.update_layout(yaxis_title=f"Spend ({CURRENCY_SYMBOL})")
                st.plotly_chart(fig_trend, use_container_width=True)

                st.subheader("Category Mix Over Time")
                st.caption(
                    "Same data as a % share of each month's spend — useful for "
                    "spotting a category creeping up even if total spend is flat."
                )
                fig_share = px.area(
                    monthly_spend,
                    x="Month",
                    y="Spend",
                    color="DisplayCategory",
                    groupnorm="percent",
                    title="Category Mix by Month (%)",
                    labels={"DisplayCategory": "Category"},
                )
                fig_share.update_layout(yaxis_title="Share of spend (%)")
                st.plotly_chart(fig_share, use_container_width=True)

                # ---- This month vs. your average ----
                months_sorted = sorted(view_df["Month"].unique())
                latest_month = months_sorted[-1]
                prior_months = months_sorted[:-1]

                if prior_months:
                    st.subheader("This Month vs. Your Average")
                    st.caption(
                        f"**{latest_month}** compared with the average of the "
                        f"{len(prior_months)} prior month(s) shown, per category — "
                        "handy for spotting where you're overspending your norm."
                    )

                    latest = (
                        monthly_spend[monthly_spend["Month"] == latest_month]
                        .set_index("DisplayCategory")["Spend"]
                    )
                    average = (
                        monthly_spend[monthly_spend["Month"].isin(prior_months)]
                        .groupby("DisplayCategory")["Spend"]
                        .sum()
                        / len(prior_months)
                    )

                    comparison = pd.DataFrame(
                        {"This Month": latest, "Your Average": average}
                    ).fillna(0).reset_index().rename(columns={"DisplayCategory": "Category"})
                    comparison = comparison.sort_values("This Month", ascending=False)

                    fig_compare = px.bar(
                        comparison.melt(
                            id_vars="Category", var_name="Period", value_name="Spend"
                        ),
                        x="Category",
                        y="Spend",
                        color="Period",
                        barmode="group",
                        title=f"{latest_month} vs. Average Monthly Spend",
                    )
                    fig_compare.update_layout(yaxis_title=f"Spend ({CURRENCY_SYMBOL})")
                    st.plotly_chart(fig_compare, use_container_width=True)

            # ---- Top merchants ----
            st.subheader("Top Merchants")
            top_merchants = (
                view_df[view_df["Amount"] < 0]
                .assign(Spend=lambda d: -d["Amount"])
                .groupby("Description")
                .agg(Spend=("Spend", "sum"), Transactions=("Spend", "count"))
                .sort_values("Spend", ascending=False)
                .head(10)
                .reset_index()
            )
            if not top_merchants.empty:
                st.dataframe(
                    top_merchants,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Spend": st.column_config.NumberColumn(
                            "Spend", format=f"{CURRENCY_SYMBOL} %.2f"
                        ),
                    },
                )

        # ---- Uncategorized callout ----
        uncategorized = transactions[transactions["Category"] == UNCATEGORIZED]

        if not uncategorized.empty:
            with st.expander(f"⚠️ {len(uncategorized)} uncategorized transaction(s)"):
                st.dataframe(
                    uncategorized[["Date", "Description", "Amount", "SourceFile"]],
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(
                    "Set their Category in the table above, then click "
                    "\"Save keywords & recategorize\" so future statements "
                    "recognize the same description automatically."
                )


# ============================================================
# Budgets tab
# ============================================================

with tab_budgets:
    ledger = load_ledger()
    active_cache_id = st.session_state.get("active_cache_id", "main")
    ledger = ledger[ledger["CacheId"] == active_cache_id].copy()

    if ledger.empty:
        st.info("Choose a bank template and upload one or more statement PDFs using the sidebar to begin.")
    else:
        category_groups = load_category_groups()
        all_categories = sorted(ledger["Category"].unique())
        category_groups = {
            c: g for c, g in category_groups.items() if c in all_categories
        }
        # A budget target is either a standalone category or a group label
        # from the Dashboard tab's Category View — whichever a category
        # currently resolves to.
        budget_targets = sorted({category_groups.get(c, c) for c in all_categories})

        budgets = load_budgets()
        budgets = {k: v for k, v in budgets.items() if k in budget_targets}

        st.subheader("Monthly Budgets")
        st.caption(
            "Set a monthly spending cap for a category, or for a whole group "
            "of categories (create groups in the Dashboard tab's Category "
            "View — grouped categories share one budget here)."
        )

        add_col1, add_col2, add_col3 = st.columns([2, 1, 1])

        with add_col1:
            budget_target = st.selectbox(
                "Category or group", options=budget_targets, key="budget_target_select"
            )
        with add_col2:
            budget_amount = st.number_input(
                "Monthly budget",
                min_value=0.0,
                step=50.0,
                value=float(budgets.get(budget_target, 0.0)),
                key=f"budget_amount_{budget_target}",
            )
        with add_col3:
            st.write("")
            st.write("")
            if st.button("Save budget"):
                if budget_amount <= 0:
                    budgets.pop(budget_target, None)
                    st.info(f"Removed any budget for '{budget_target}'.")
                else:
                    budgets[budget_target] = budget_amount
                    st.success(
                        f"Saved a {CURRENCY_SYMBOL} {budget_amount:,.2f}/month "
                        f"budget for '{budget_target}'."
                    )
                save_budgets(budgets)
                st.rerun()

        if budgets:
            st.markdown("**Current budgets**")
            for target, amount in sorted(budgets.items()):
                bcol1, bcol2 = st.columns([5, 1])
                with bcol1:
                    st.write(f"**{target}** — {CURRENCY_SYMBOL} {amount:,.2f} / month")
                with bcol2:
                    if st.button("🗑️", key=f"del_budget_{target}"):
                        budgets.pop(target, None)
                        save_budgets(budgets)
                        st.rerun()

        if not budgets:
            st.info("No budgets set yet — add one above to see performance tracking.")
        else:
            st.divider()
            st.subheader("Budget Performance")

            perf_df = apply_category_groups(ledger, category_groups)
            perf_df["Month"] = perf_df["Date"].dt.to_period("M").astype(str)

            all_months = sorted(perf_df["Month"].unique())
            latest_month = all_months[-1]

            budgeted_spend = (
                perf_df[
                    (perf_df["Amount"] < 0)
                    & (perf_df["DisplayCategory"].isin(budgets.keys()))
                ]
                .assign(Spend=lambda d: -d["Amount"])
                .groupby(["Month", "DisplayCategory"])["Spend"]
                .sum()
                .reset_index()
            )

            current_spend_map = (
                budgeted_spend[budgeted_spend["Month"] == latest_month]
                .set_index("DisplayCategory")["Spend"]
                .to_dict()
            )

            total_budget = sum(budgets.values())
            total_spent = sum(current_spend_map.get(t, 0.0) for t in budgets)

            today = datetime.now()
            is_current_month = latest_month == today.strftime("%Y-%m")

            st.markdown(f"#### This Month ({latest_month})")

            kcol1, kcol2, kcol3 = st.columns(3)
            with kcol1:
                st.metric("Total Budgeted", f"{CURRENCY_SYMBOL} {total_budget:,.2f}")
            with kcol2:
                st.metric(
                    "Total Spent",
                    f"{CURRENCY_SYMBOL} {total_spent:,.2f}",
                    delta=f"{CURRENCY_SYMBOL} {total_spent - total_budget:,.2f} vs budget",
                    delta_color="inverse",
                )
            with kcol3:
                pct_used = (total_spent / total_budget * 100) if total_budget else 0
                st.metric("% of Budget Used", f"{pct_used:,.0f}%")

            if is_current_month:
                days_in_month = calendar.monthrange(today.year, today.month)[1]
                day_of_month = today.day
                projected_total = (
                    total_spent / day_of_month * days_in_month if day_of_month else 0
                )
                pace_note = (
                    f"You're {day_of_month} of {days_in_month} days into the month — "
                    f"at this pace you're on track to spend "
                    f"**{CURRENCY_SYMBOL} {projected_total:,.2f}** this month "
                    f"against a total budget of {CURRENCY_SYMBOL} {total_budget:,.2f}."
                )
                if total_budget and projected_total > total_budget:
                    st.warning(pace_note)
                else:
                    st.caption(pace_note)
            else:
                st.caption(
                    "This is the most recent month in your uploaded statements — "
                    "upload a newer statement to track the current month."
                )

            st.markdown("##### By category")
            for target in sorted(budgets.keys()):
                budget_amt = budgets[target]
                spent = current_spend_map.get(target, 0.0)
                fraction = min(spent / budget_amt, 1.0) if budget_amt else 0.0
                over = budget_amt and spent > budget_amt
                pct_label = f"{spent / budget_amt * 100:,.0f}%" if budget_amt else "—"

                st.write(
                    f"{'⚠️ ' if over else ''}**{target}**: "
                    f"{CURRENCY_SYMBOL} {spent:,.2f} / {CURRENCY_SYMBOL} {budget_amt:,.2f} "
                    f"({pct_label})"
                )
                st.progress(fraction)

            st.markdown("##### Trend vs. Budget")

            targets = sorted(budgets.keys())
            trend_cols = st.columns(2)
            for i, target in enumerate(targets):
                target_df = (
                    pd.DataFrame({"Month": all_months})
                    .merge(
                        budgeted_spend[budgeted_spend["DisplayCategory"] == target],
                        on="Month",
                        how="left",
                    )
                    .fillna({"Spend": 0.0})
                )

                fig = px.bar(target_df, x="Month", y="Spend", title=target)

                # Show both the monthly budget and the average monthly spend
                # across all months currently displayed.
                average_spend = target_df["Spend"].mean()
                fig.add_hline(
                    y=budgets[target],
                    line_dash="dash",
                    line_color="#C0392B",
                    annotation_text="Budget",
                    annotation_position="top left",
                )
                fig.add_hline(
                    y=average_spend,
                    line_dash="dot",
                    line_color="#2E8B57",
                    annotation_text="Average",
                    annotation_position="bottom left",
                )
                fig.update_layout(
                    yaxis_title=f"Spend ({CURRENCY_SYMBOL})", showlegend=False
                )

                with trend_cols[i % 2]:
                    st.plotly_chart(fig, use_container_width=True)

            if len(all_months) > 1:
                st.markdown("##### Monthly Adherence")

                adherence_rows = []
                for target in targets:
                    budget_amt = budgets[target]
                    target_series = budgeted_spend[
                        budgeted_spend["DisplayCategory"] == target
                    ].set_index("Month")["Spend"]

                    for month in all_months:
                        spend = target_series.get(month, 0.0)
                        adherence_rows.append(
                            {
                                "Month": month,
                                "Category": target,
                                "Spent": spend,
                                "Budget": budget_amt,
                                "% Used": (spend / budget_amt * 100) if budget_amt else 0,
                                "Status": "⚠️ Over" if spend > budget_amt else "✅ On budget",
                            }
                        )

                adherence_df = pd.DataFrame(adherence_rows)
                st.dataframe(
                    adherence_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Spent": st.column_config.NumberColumn(
                            "Spent", format=f"{CURRENCY_SYMBOL} %.2f"
                        ),
                        "Budget": st.column_config.NumberColumn(
                            "Budget", format=f"{CURRENCY_SYMBOL} %.2f"
                        ),
                        "% Used": st.column_config.NumberColumn(
                            "% Used", format="%.0f%%"
                        ),
                    },
                )

                on_budget_months = (adherence_df["Status"] == "✅ On budget").sum()
                st.caption(
                    f"On budget in {on_budget_months} of {len(adherence_df)} "
                    "category-month(s) shown."
                )

            unbudgeted = [t for t in budget_targets if t not in budgets]
            if unbudgeted:
                st.divider()
                st.markdown("##### Not Yet Budgeted")
                st.caption(
                    "Average monthly spend for categories without a budget — "
                    "candidates worth capping."
                )

                avg_spend = (
                    perf_df[
                        (perf_df["Amount"] < 0)
                        & (perf_df["DisplayCategory"].isin(unbudgeted))
                    ]
                    .assign(Spend=lambda d: -d["Amount"])
                    .groupby(["Month", "DisplayCategory"])["Spend"]
                    .sum()
                    .reset_index()
                    .groupby("DisplayCategory")["Spend"]
                    .mean()
                    .sort_values(ascending=False)
                    .reset_index()
                    .rename(
                        columns={
                            "DisplayCategory": "Category",
                            "Spend": "Avg Monthly Spend",
                        }
                    )
                )

                if not avg_spend.empty:
                    st.dataframe(
                        avg_spend.head(8),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Avg Monthly Spend": st.column_config.NumberColumn(
                                "Avg Monthly Spend", format=f"{CURRENCY_SYMBOL} %.2f"
                            ),
                        },
                    )


# ============================================================
# Keyword Cache tab
# ============================================================

with tab_templates:
    st.subheader("Bank Statement Templates")
    st.caption(
        "Every statement template — including the built-in FNB, ABSA and Capitec templates — "
        "can be renamed, edited, tested and deleted. Deleting a template does not delete statements "
        "that were already imported with it."
    )

    templates = load_custom_templates()
    active_templates = {k: v for k, v in templates.items() if not v.get("deleted", False)}

    summary_rows = []
    for template_id, template in active_templates.items():
        summary_rows.append({
            "Name": template.get("name", template_id),
            "Type": "Built-in" if template.get("builtin") else "Custom",
            "Parser": template.get("parser", "custom"),
            "ID": template_id,
        })
    if summary_rows:
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Edit an existing template")

    edit_options = ["+ Create new template"] + sorted(
        active_templates, key=lambda tid: active_templates[tid].get("name", tid).lower()
    )
    edit_choice = st.selectbox(
        "Template",
        options=edit_options,
        format_func=lambda tid: tid if tid == "+ Create new template" else active_templates[tid].get("name", tid),
        key="template_edit_choice",
    )

    if edit_choice == "+ Create new template":
        editing_id = None
        base = {
            "name": "",
            "builtin": False,
            "parser": "custom",
            "row_style": "single_amount",
            "date_format": "%Y-%m-%d",
            "date_pattern": r"\d{4}-\d{2}-\d{2}",
            "continuation_lines": True,
        }
    else:
        editing_id = edit_choice
        base = dict(active_templates[editing_id])

    template_name = st.text_input(
        "Template / bank name",
        value=base.get("name", ""),
        key=f"template_name_{editing_id or 'new'}",
    )

    parser_options = {
        "Built-in FNB parser": "fnb",
        "Built-in ABSA parser": "absa",
        "Built-in Capitec parser": "capitec",
        "Generic configurable parser": "custom",
    }
    parser_labels = list(parser_options)
    current_parser = base.get("parser", "custom")
    parser_default = current_parser if current_parser in parser_options.values() else "custom"
    parser_label = st.selectbox(
        "Parsing engine",
        parser_labels,
        index=list(parser_options.values()).index(parser_default),
        key=f"template_parser_{editing_id or 'new'}",
        help="Built-in parsers handle their bank's known layout. The generic parser exposes the editable date/amount rules below.",
    )
    parser = parser_options[parser_label]

    date_format_options = {
        "YYYY-MM-DD": "%Y-%m-%d",
        "DD/MM/YYYY": "%d/%m/%Y",
        "MM/DD/YYYY": "%m/%d/%Y",
        "DD-MM-YYYY": "%d-%m-%Y",
        "MM-DD-YYYY": "%m-%d-%Y",
        "DD.MM.YYYY": "%d.%m.%Y",
        "DD Mon YYYY": "%d %b %Y",
        "DD Mon": "%d %b",
    }
    row_style_label_map = {
        "single_amount": "Date + Description + Amount + Balance",
        "money_in_out": "Date + Description + Money In + Money Out + Balance",
    }

    sample = st.file_uploader(
        "Sample statement",
        type=["pdf"],
        key=f"template_test_file_{editing_id or 'new'}",
        help="Upload a representative statement — used to auto-detect the fields below and to test the template before saving.",
    )

    if parser == "custom":
        if sample and st.button("🔎 Auto-detect from sample", key=f"autodetect_{editing_id or 'new'}"):
            try:
                detected = analyze_sample_statement(sample.getvalue())
            except Exception as exc:
                st.error(f"Could not analyze the sample: {exc}")
                detected = {"detected": False}

            if not detected.get("detected"):
                st.warning(
                    "Couldn't confidently detect a date format or transaction "
                    "layout from this sample — fill in the fields below "
                    "manually, or double-check this is a text-based (not "
                    "scanned/image) PDF."
                )
            else:
                date_format_reverse = {v: k for k, v in date_format_options.items()}
                # These must be staged and applied on the NEXT run, before
                # the row_style/date_format/date_pattern widgets below are
                # instantiated — Streamlit doesn't allow setting a widget's
                # session_state key after that widget already exists in the
                # current run (same rule as the active-cache-name fix).
                st.session_state[f"_pending_row_style_{editing_id or 'new'}"] = (
                    row_style_label_map[detected["row_style"]]
                )
                if detected["date_format"] in date_format_reverse:
                    st.session_state[f"_pending_date_format_{editing_id or 'new'}"] = (
                        date_format_reverse[detected["date_format"]]
                    )
                st.session_state[f"_pending_date_pattern_{editing_id or 'new'}"] = detected["date_pattern"]

                layout_name = (
                    "Money In / Money Out" if detected["row_style"] == "money_in_out" else "single Amount"
                )
                if detected["confidence"] < 0.5:
                    st.warning(
                        f"Detected {detected['transaction_count']} transaction(s) and pre-filled a "
                        f"best-guess **{layout_name}** layout, but the balance didn't reconcile "
                        "confidently either way — double-check the **Transaction layout** setting "
                        "below manually. If this statement has genuinely separate Money In / Money "
                        "Out columns, flattened PDF text can't always tell them apart reliably; a "
                        "dedicated built-in parser (like FNB, ABSA or Capitec) may be needed instead."
                    )
                else:
                    st.success(
                        f"Detected {detected['transaction_count']} transaction(s) with a "
                        f"**{layout_name}** layout ({detected['confidence']:.0%} of balances "
                        "reconcile). Fields below are pre-filled — check them over, then Test."
                    )
                st.rerun()

    # Apply any pending auto-detected values before the widgets below are
    # created (see comment above).
    pending_row_style = st.session_state.pop(f"_pending_row_style_{editing_id or 'new'}", None)
    if pending_row_style:
        st.session_state[f"template_row_style_{editing_id or 'new'}"] = pending_row_style
    pending_date_format = st.session_state.pop(f"_pending_date_format_{editing_id or 'new'}", None)
    if pending_date_format:
        st.session_state[f"template_date_format_{editing_id or 'new'}"] = pending_date_format
    pending_date_pattern = st.session_state.pop(f"_pending_date_pattern_{editing_id or 'new'}", None)
    if pending_date_pattern:
        st.session_state[f"template_date_pattern_{editing_id or 'new'}"] = pending_date_pattern

    row_style_label = st.selectbox(
        "Transaction layout",
        list(row_style_label_map.values()),
        index=1 if base.get("row_style") == "money_in_out" else 0,
        key=f"template_row_style_{editing_id or 'new'}",
    )
    row_style = "money_in_out" if row_style_label.startswith("Date + Description + Money") else "single_amount"

    reverse_date = {v: k for k, v in date_format_options.items()}
    current_date_format = base.get("date_format", "%Y-%m-%d")
    date_label = st.selectbox(
        "Date format",
        list(date_format_options),
        index=list(date_format_options).index(reverse_date.get(current_date_format, "YYYY-MM-DD")),
        key=f"template_date_format_{editing_id or 'new'}",
    )
    date_format = date_format_options[date_label]
    date_pattern = st.text_input(
        "Date pattern (advanced)",
        value=base.get("date_pattern", r"\d{4}-\d{2}-\d{2}"),
        key=f"template_date_pattern_{editing_id or 'new'}",
        help="Regular expression matching the transaction date at the start of a transaction row.",
    )
    continuation_lines = st.checkbox(
        "Append wrapped/continuation lines to the previous transaction",
        value=bool(base.get("continuation_lines", True)),
        key=f"template_continuation_{editing_id or 'new'}",
    )

    candidate = {
        "name": template_name.strip(),
        "builtin": bool(base.get("builtin", False)) if editing_id else False,
        "parser": parser,
        "row_style": row_style,
        "date_format": date_format,
        "date_pattern": date_pattern,
        "continuation_lines": continuation_lines,
    }

    if st.button("🧪 Test template", key=f"test_template_{editing_id or 'new'}"):
        if not template_name.strip():
            st.error("Enter a template name first.")
        elif not sample:
            st.error("Upload a sample statement first.")
        else:
            try:
                if parser in {"fnb", "absa", "capitec"}:
                    test_df, invalid = parse_statement_pdf(sample.getvalue(), parser)
                else:
                    test_df, invalid = _parse_custom_statement(sample.getvalue(), candidate)
                st.session_state[f"tested_template_{editing_id or 'new'}"] = candidate
                st.session_state[f"tested_template_name_{editing_id or 'new'}"] = template_name.strip()
                if test_df.empty:
                    st.error("No transactions were detected with these settings.")
                else:
                    st.success(f"Detected {len(test_df)} transaction(s); {invalid} row(s) could not be parsed.")
                    st.dataframe(test_df.head(20), use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(f"Template test failed: {exc}")

    tested_key = f"tested_template_{editing_id or 'new'}"
    tested = st.session_state.get(tested_key)
    if tested and st.session_state.get(f"tested_template_name_{editing_id or 'new'}") == template_name.strip():
        save_label = "💾 Save template" if editing_id else "💾 Create template"
        if st.button(save_label, key=f"save_template_{editing_id or 'new'}"):
            templates = load_custom_templates()
            if editing_id:
                tested["builtin"] = templates[editing_id].get("builtin", False)
                templates[editing_id] = tested
            else:
                new_id = cache_id_from_name(template_name)  # stable slug generator is sufficient for template IDs too
                templates[new_id] = tested
            save_custom_templates(templates)
            st.session_state.pop(tested_key, None)
            st.session_state.pop(f"tested_template_name_{editing_id or 'new'}", None)
            st.success(f"Saved '{template_name.strip()}'.")
            st.rerun()

    if editing_id:
        st.divider()
        st.subheader("Delete template")
        st.warning(
            f"Deleting **{active_templates[editing_id].get('name', editing_id)}** removes it from the upload selector. "
            "Existing imported statements are retained."
        )
        confirm_delete = st.checkbox("I understand that this template will no longer be available for new uploads.", key=f"confirm_delete_{editing_id}")
        if st.button("🗑️ Delete template", disabled=not confirm_delete, key=f"delete_template_{editing_id}"):
            templates = load_custom_templates()
            templates.pop(editing_id, None)
            save_custom_templates(templates)
            st.success("Template deleted.")
            st.rerun()

with tab_keywords:
    st.subheader("Keyword Cache")
    st.caption(
        "Each category is a list of keywords. A transaction is matched to the "
        "category whose keyword is the longest substring found in its "
        "(uppercased) description."
    )

    categories = list_categories()

    if categories:
        summary = pd.DataFrame(
            [{"Category": name, "Keywords": len(kws)} for name, kws in categories.items()]
        )
        st.dataframe(summary, use_container_width=True, hide_index=True)
    else:
        st.info("No categories yet — add one below.")

    st.divider()

    category_choice = st.selectbox(
        "Add new / edit existing category",
        options=["+ New category"] + sorted(categories.keys()),
        key="category_choice",
    )

    if category_choice == "+ New category":
        default_cat_name = ""
        default_keywords = ""
    else:
        default_cat_name = category_choice
        default_keywords = "\n".join(categories[category_choice])

    with st.form("category_form"):
        cat_name = st.text_input("Category name", value=default_cat_name)
        keywords_text = st.text_area(
            "Keywords (one per line)",
            value=default_keywords,
            height=200,
            help="Matched as an uppercase substring against each transaction description.",
        )

        submitted = st.form_submit_button("Save category")

        if submitted:
            if not cat_name.strip():
                st.error("Please provide a category name.")
            else:
                keywords = keywords_text.splitlines()
                save_category(cat_name.strip(), keywords)

                # Renamed category — remove the old file.
                if (
                    category_choice != "+ New category"
                    and category_choice != cat_name.strip()
                ):
                    delete_category(category_choice)

                st.success(f"Saved category '{cat_name.strip()}'.")
                st.rerun()

    if categories:
        st.divider()
        delete_choice = st.selectbox(
            "Delete a category", options=[""] + sorted(categories.keys())
        )
        if delete_choice and st.button(f"Delete '{delete_choice}'"):
            delete_category(delete_choice)
            st.success(f"Deleted category '{delete_choice}'.")
            st.rerun()