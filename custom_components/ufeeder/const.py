"""U Feeder constants, recovered from the manufacturer's Android app 1.1.4."""

DOMAIN = "ufeeder"
CONF_REGION = "region"
CONF_CLIENT_ID = "client_id"
CONF_SESSION = "session"
APP_ID = "25030009"
APP_KEY = "fbab23c8573b466f83c650b0020cc73b"
PASSWORD_SALT = "9cb06cd103064100b86d67d412e844f0"
PRODUCT_KEY = "41d9698a795"
BOOTSTRAP = "https://ucsp-oversea.ubtrobot.com"
ALLOWED_DOMAINS = frozenset(
    f"https://ucsp-{region}.ubtrobot.com" for region in ("oversea", "eu", "us", "sg")
)
POLL_SECONDS = 60
