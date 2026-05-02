import hashlib
import secrets
import string


def generate_api_key() -> str:
    return f"cue_sk_{secrets.token_hex(16)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def get_api_key_prefix(api_key: str) -> str:
    return api_key[:12]


def generate_webhook_secret() -> str:
    return f"whsec_{secrets.token_hex(32)}"


def generate_cue_id() -> str:
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(12))
    return f"cue_{suffix}"


def generate_agent_id() -> str:
    """Format ``agt_<12 alphanumeric>``. See MESSAGING_SPEC §2.1."""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(12))
    return f"agt_{suffix}"


def generate_message_id() -> str:
    """Format ``msg_<12 alphanumeric>``. See MESSAGING_SPEC §3.1."""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(12))
    return f"msg_{suffix}"
