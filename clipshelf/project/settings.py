"""Django settings for the Clipshelf server (rev. 4).

Zero required environment variables: the data directory (CLIPSHELF_DATA_DIR or
./data) holds the SQLite database and a generated secret key; hosts default to
"*" for LAN use. CSRF protection and HTTPS hardening are opt-in via
CLIPSHELF_CSRF=1 and CLIPSHELF_ORIGIN. One web process + one worker
coordinator; caches are process-local on purpose (see CACHES).
"""
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DEBUG = os.environ.get("CLIPSHELF_DEBUG") == "1"
_env_data_dir = os.environ.get("CLIPSHELF_DATA_DIR")
DATA_DIR = Path(_env_data_dir or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY_FALLBACKS = [
    key.strip()
    for key in os.environ.get("CLIPSHELF_SECRET_KEY_FALLBACKS", "").replace("\n", ",").split(",")
    if key.strip()
]

_secret_path = DATA_DIR / "secret_key"
if os.environ.get("CLIPSHELF_SECRET_KEY"):
    SECRET_KEY = os.environ["CLIPSHELF_SECRET_KEY"]
else:
    # Generate and persist a secret on first boot. The O_EXCL create is the
    # race guard: when the web and worker processes start together, exactly
    # one wins the create and the other reads the winner's key off disk.
    try:
        fd = os.open(_secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(get_random_secret_key())
    SECRET_KEY = _secret_path.read_text().strip()


_hosts_env = os.environ.get("CLIPSHELF_ALLOWED_HOSTS", "").strip()
if _hosts_env:
    ALLOWED_HOSTS = [host.strip() for host in _hosts_env.split(",") if host.strip()]
    # The container probes its own /healthz over loopback, so 127.0.0.1 must
    # keep answering when the operator pins real hostnames. Appended, never
    # prepended: account links are built from the first entry.
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "[::1]"]
    if DEBUG:
        ALLOWED_HOSTS.append("testserver")
else:
    ALLOWED_HOSTS = ["*"]  # LAN behind a VPN; every Host header is fine.

# Canonical origin for link-building with no HTTP request to read a Host from
# (account mail, CLI commands). Purely optional: set it only for a non-default
# scheme/port, or to turn on HTTPS transport hardening below.
CLIPSHELF_ORIGIN = os.environ.get("CLIPSHELF_ORIGIN", "").rstrip("/")
if CLIPSHELF_ORIGIN and not CLIPSHELF_ORIGIN.startswith(("http://", "https://")):
    raise ImproperlyConfigured("CLIPSHELF_ORIGIN must start with http:// or https://.")

CLIPSHELF_CSRF = os.environ.get("CLIPSHELF_CSRF") == "1"
if CLIPSHELF_CSRF and not CLIPSHELF_ORIGIN and ALLOWED_HOSTS == ["*"]:
    raise ImproperlyConfigured(
        "CLIPSHELF_CSRF=1 requires CLIPSHELF_ORIGIN or a concrete "
        "CLIPSHELF_ALLOWED_HOSTS list; with hosts=\"*\" CSRF cannot be checked."
    )
CSRF_TRUSTED_ORIGINS = (
    [CLIPSHELF_ORIGIN]
    if CLIPSHELF_ORIGIN
    else [f"https://{host}" for host in ALLOWED_HOSTS if "*" not in host]
)
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

# --- SQLite: durable WAL, single-writer-friendly, version deployment guard ---
SQLITE_MIN_VERSION = (3, 51, 3)  # WAL-reset corruption fix (contract, server-plan §7)
if (
    not DEBUG
    and sqlite3.sqlite_version_info < SQLITE_MIN_VERSION
    and os.environ.get("CLIPSHELF_SQLITE_VERIFIED") != "1"
):
    raise ImproperlyConfigured(
        f"SQLite {sqlite3.sqlite_version} lacks the verified WAL-reset fix "
        f"(need >= {'.'.join(map(str, SQLITE_MIN_VERSION))}). Upgrade SQLite or set "
        "CLIPSHELF_SQLITE_VERIFIED=1 to assert the runtime build is a fixed backport."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=FULL;"
                "PRAGMA busy_timeout=30000;"
            ),
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
    }
}

# One web process only: LocMemCache gives allauth its atomic process-local
# rate-limit/lock backend. Documented ceiling: limits are per-process and
# reset on restart. Never add web workers without switching to a shared cache
# backend first.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "clipshelf-default",
    }
}

# --- Application definition ---
INSTALLED_APPS = [
    # clipshelf first: its templates/account/base.html must win over allauth's
    "clipshelf.project.apps.ClipshelfConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.headless",
]
ROOT_URLCONF = "clipshelf.urls"
WSGI_APPLICATION = "clipshelf.project.wsgi.application"
SITE_ID = 1
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
]
if CLIPSHELF_CSRF:
    MIDDLEWARE.append("django.middleware.csrf.CsrfViewMiddleware")
MIDDLEWARE += [
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "clipshelf.setup.SetupMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],  # templates ship inside the clipshelf app package
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
AUTH_USER_MODEL = "clipshelf.User"

# django-allauth: email-only auth, username auto-generated (never collected),
# invitation-only admission enforced by the account adapter.
ACCOUNT_ADAPTER = "clipshelf.accounts.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_NOTIFICATIONS = True
ACCOUNT_REAUTHENTICATION_REQUIRED = True
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True  # invalidates browser + mobile sessions
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[clipshelf] "  # not the Sites default domain
PASSWORD_RESET_TIMEOUT = 3600  # bounded, single-use reset links
HEADLESS_ENABLED = True
HEADLESS_CLIENTS = ("app",)  # native clients only; browsers use /accounts/ views
ACCOUNT_FORMS = {"signup": "clipshelf.accounts.InviteSignupForm"}
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "allauth.account.auth_backends.AuthenticationBackend",
]


LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"

# --- Internationalization ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ---
STATIC_URL = "static/"
# clipshelf/static/ is served directly by views.static_asset in every mode, so
# there is no STATIC_ROOT and no collectstatic step in the image build.

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Upload ceilings: honest 413, no truncation ---
CLIPSHELF_MAX_IMPORT_BYTES = int(os.environ.get("CLIPSHELF_MAX_IMPORT_BYTES", 32 * 1024 * 1024))
DATA_UPLOAD_MAX_MEMORY_SIZE = CLIPSHELF_MAX_IMPORT_BYTES
FILE_UPLOAD_MAX_MEMORY_SIZE = CLIPSHELF_MAX_IMPORT_BYTES
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

# --- Email: console backend until real SMTP is configured ---
_SMTP_HOST = os.environ.get("CLIPSHELF_SMTP_HOST", "")
if DEBUG or not _SMTP_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = _SMTP_HOST
    EMAIL_PORT = int(os.environ.get("CLIPSHELF_SMTP_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("CLIPSHELF_SMTP_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("CLIPSHELF_SMTP_PASSWORD", "")
    EMAIL_USE_SSL = os.environ.get("CLIPSHELF_SMTP_USE_SSL") == "1"
    EMAIL_USE_TLS = not EMAIL_USE_SSL and os.environ.get("CLIPSHELF_SMTP_USE_TLS", "1") == "1"
    EMAIL_TIMEOUT = 30
# --- Transport security (opt-in: an explicitly configured https:// origin) ---
if CLIPSHELF_ORIGIN.startswith("https://") and not DEBUG:
    SECURE_SSL_REDIRECT = True
    # The container's loopback readiness probe must not be redirected to a TLS
    # port the app does not terminate; /healthz exposes no account data.
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
DEFAULT_FROM_EMAIL = os.environ.get("CLIPSHELF_EMAIL_FROM", "").strip() or (
    f"clipshelf@{urlsplit(CLIPSHELF_ORIGIN or 'http://localhost').hostname or 'localhost'}"
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
APPEND_SLASH = False  # API routes have no trailing slash; no silent redirects

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.security.DisallowedHost": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
