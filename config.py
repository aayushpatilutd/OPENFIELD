"""
InvoiceHub configuration.

Two sources of truth, in this order:

  1. the `settings` table in the database  — edited in the UI, survives restarts
  2. environment / .env                    — the fallback, and how you bootstrap

The UI is the primary one on purpose. The whole point of this portal is that an
AP clerk can connect ServiceTitan, Microsoft Graph, QuickBooks Online and Claude
without opening a terminal or redeploying. Environment variables stay supported
so a container can come up already configured.

Secrets are stored encrypted at rest with Fernet. The key comes from
INVOICEHUB_SECRET_KEY; if that is absent a key file is generated next to the
database so a single-box install still encrypts rather than storing plaintext.
"""

import os
import base64
import hashlib
import logging

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.getenv("INVOICEHUB_DB", os.path.join(BASE_DIR, "invoicehub.db"))
STORAGE    = os.getenv("INVOICEHUB_STORAGE", os.path.join(BASE_DIR, "storage"))
KEYFILE    = os.path.join(BASE_DIR, ".fernet_key")

SECRET_KEY = os.getenv("INVOICEHUB_SECRET_KEY", "invoicehub-dev-key-change-me")

# Keep uploads generous: a scanned 40-page statement is not small.
MAX_UPLOAD_MB = int(os.getenv("INVOICEHUB_MAX_UPLOAD_MB", "64"))

# Demo mode makes every integration return canned data instead of calling out.
# It is a runtime setting (settings.demo_mode) rather than a constant, so the
# same deployment can be flipped into demo for a walkthrough and back again.
DEMO_DEFAULT = os.getenv("INVOICEHUB_DEMO", "1") not in ("0", "false", "False", "")

os.makedirs(STORAGE, exist_ok=True)


# ── secret encryption ────────────────────────────────────────────────────────

def _fernet():
    """Return a Fernet, or None if the library is missing.

    Falling back to None rather than raising means a box without `cryptography`
    still runs — it just stores secrets in the clear and says so loudly in the
    log and on the Settings page, which is better than refusing to boot.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    raw = os.getenv("INVOICEHUB_FERNET_KEY", "").strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except Exception:
            # A malformed key in the environment is a typo, not a reason to
            # silently generate a different one and orphan every stored secret.
            log.error("INVOICEHUB_FERNET_KEY is not a valid Fernet key — "
                      "secrets will not decrypt. Fix the key or unset it.")
            return None

    if os.path.exists(KEYFILE):
        try:
            with open(KEYFILE, "rb") as fh:
                return Fernet(fh.read().strip())
        except Exception as e:
            log.error("Could not read %s (%s)", KEYFILE, e)
            return None

    key = Fernet.generate_key()
    try:
        with open(KEYFILE, "wb") as fh:
            fh.write(key)
        os.chmod(KEYFILE, 0o600)
        log.info("Generated a new encryption key at %s — back this up. "
                 "Lose it and stored credentials become unreadable.", KEYFILE)
    except Exception as e:
        log.warning("Could not persist the encryption key (%s). Secrets will "
                    "re-encrypt on every restart.", e)
    return Fernet(key)


_FERNET = _fernet()
ENCRYPTION_AVAILABLE = _FERNET is not None

_PREFIX = "enc::"


def encrypt(value: str) -> str:
    """Encrypt a secret for storage. Returns plaintext if crypto is unavailable."""
    if value is None:
        return ""
    value = str(value)
    if not value or not _FERNET:
        return value
    try:
        return _PREFIX + _FERNET.encrypt(value.encode()).decode()
    except Exception as e:
        log.warning("Encrypt failed (%s) — storing as plaintext", e)
        return value


def decrypt(value: str) -> str:
    """Decrypt a stored secret. Plaintext passes straight through."""
    if not value:
        return ""
    value = str(value)
    if not value.startswith(_PREFIX):
        return value          # written before encryption was available
    if not _FERNET:
        return ""             # encrypted but we have no key — do not guess
    try:
        return _FERNET.decrypt(value[len(_PREFIX):].encode()).decode()
    except Exception:
        log.warning("Could not decrypt a stored secret — wrong key?")
        return ""


def mask(value: str, keep: int = 4) -> str:
    """What the UI shows instead of a secret: '••••••••cd34'."""
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= keep:
        return "•" * 8
    return "•" * 8 + v[-keep:]


def fingerprint(value: str) -> str:
    """A stable short hash, so two environments can be compared without
    revealing the secret itself."""
    if not value:
        return ""
    return hashlib.sha256(str(value).encode()).hexdigest()[:8]


# ── the credential catalogue ─────────────────────────────────────────────────
#
# Every field the Settings page knows how to render. Declaring them here rather
# than hardcoding the form means adding a credential is a one-line change and
# the API, the UI and the connection test all stay in step.

SECRET = True
PLAIN  = False

INTEGRATIONS = {
    "graph": {
        "label": "Microsoft Graph",
        "blurb": "Reads the AP mailbox so invoices and statements file themselves, "
                 "and sends the emails your agents draft.",
        "docs": "https://learn.microsoft.com/graph/auth-v2-service",
        "fields": [
            ("graph_tenant_id",     "Directory (tenant) ID", PLAIN,  "", True),
            ("graph_client_id",     "Application (client) ID", PLAIN, "", True),
            ("graph_client_secret", "Client secret",         SECRET, "", True),
            ("graph_mailbox",       "AP mailbox",            PLAIN,
             "ap@yourcompany.com — the mailbox invoices arrive in", True),
            ("graph_send_as",       "Send outbound mail as", PLAIN,
             "Defaults to the AP mailbox above", False),
        ],
        "env": {
            "graph_tenant_id":     ["AZURE_TENANT_ID", "GRAPH_TENANT_ID"],
            "graph_client_id":     ["AZURE_CLIENT_ID", "GRAPH_CLIENT_ID"],
            "graph_client_secret": ["AZURE_CLIENT_SECRET", "GRAPH_CLIENT_SECRET"],
            "graph_mailbox":       ["GRAPH_USER_EMAIL", "GRAPH_MAILBOX"],
        },
    },
    "qbo": {
        "label": "QuickBooks Online",
        "blurb": "The book of record. Bills are pushed here, and the unpaid-bill "
                 "list is what statements get matched against.",
        "docs": "https://developer.intuit.com/app/developer/qbo/docs/develop",
        "fields": [
            ("qbo_client_id",     "Client ID",     PLAIN,  "", True),
            ("qbo_client_secret", "Client secret", SECRET, "", True),
            ("qbo_realm_id",      "Realm (company) ID", PLAIN, "", True),
            ("qbo_refresh_token", "Refresh token", SECRET,
             "Obtained once via the OAuth connect button", False),
            ("qbo_sandbox",       "Use sandbox",   PLAIN,
             "1 for sandbox, 0 for production", False),
            ("qbo_redirect_uri",  "OAuth redirect URI", PLAIN,
             "Must match the Intuit app exactly", False),
        ],
        "env": {
            "qbo_client_id":     ["QBO_CLIENT_ID"],
            "qbo_client_secret": ["QBO_CLIENT_SECRET"],
            "qbo_realm_id":      ["QBO_REALM_ID"],
            "qbo_refresh_token": ["QBO_REFRESH_TOKEN"],
            "qbo_sandbox":       ["QBO_SANDBOX"],
            "qbo_redirect_uri":  ["QBO_REDIRECT_URI"],
        },
    },
    "servicetitan": {
        "label": "ServiceTitan",
        "blurb": "Optional. Ties a vendor bill back to the job it was bought for, "
                 "so job costing stops being a monthly guess.",
        "docs": "https://developer.servicetitan.io/",
        "fields": [
            ("st_tenant_id",     "Tenant ID",     PLAIN,  "", True),
            ("st_client_id",     "Client ID",     PLAIN,  "", True),
            ("st_client_secret", "Client secret", SECRET, "", True),
            ("st_app_key",       "App key",       SECRET, "", True),
        ],
        "env": {
            "st_tenant_id":     ["ST_TENANT_ID"],
            "st_client_id":     ["ST_CLIENT_ID"],
            "st_client_secret": ["ST_CLIENT_SECRET"],
            "st_app_key":       ["ST_APP_KEY"],
        },
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "blurb": "Reads the PDFs. Pulls invoice numbers, dates and totals off a "
                 "scan, and turns a statement into matchable lines.",
        "docs": "https://docs.claude.com/en/api/overview",
        "fields": [
            ("claude_api_key", "API key", SECRET, "", True),
            ("claude_model",   "Model",   PLAIN,
             "Defaults to claude-sonnet-5", False),
        ],
        "env": {
            "claude_api_key": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
            "claude_model":   ["CLAUDE_MODEL", "APX_CLAUDE_MODEL"],
        },
    },
}


def env_fallback(key: str) -> str:
    """The environment value for a setting key, if any integration maps it."""
    for spec in INTEGRATIONS.values():
        for env_name in spec.get("env", {}).get(key, []):
            v = os.getenv(env_name, "").strip()
            if v:
                return v
    return ""


def field_spec(key: str):
    """(integration_name, label, is_secret, hint, required) for a settings key."""
    for name, spec in INTEGRATIONS.items():
        for f_key, label, secret, hint, required in spec["fields"]:
            if f_key == key:
                return name, label, secret, hint, required
    return None, key, False, "", False


def is_secret(key: str) -> bool:
    return bool(field_spec(key)[2])


DEFAULTS = {
    "claude_model":      "claude-sonnet-5",
    "qbo_sandbox":       "0",
    "scan_days":         "7",
    "default_terms":     "30",
    "company_name":      "Your Company",
    "demo_mode":         "1" if DEMO_DEFAULT else "0",
    "auto_push_to_qbo":  "0",   # off by default: pushing a bill is a write
    "match_tolerance":   "0.02",
}
