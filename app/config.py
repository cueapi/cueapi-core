from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://cueapi:cueapi@localhost:5432/cueapi"
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 5
    REDIS_URL: str = "redis://localhost:6379/0"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENV: str = "development"
    WEBHOOK_TIMEOUT_SECONDS: int = 30
    POLLER_INTERVAL_SECONDS: int = 5
    POLLER_BATCH_SIZE: int = 500
    WEBHOOK_CONCURRENCY_PER_WORKER: int = 50
    EXECUTION_STALE_AFTER_SECONDS: int = 300
    # Per-user concurrent delivery cap shared across cue webhooks +
    # messaging push deliveries. Prevents one user with thousands of
    # subscribers from monopolizing the worker pool.
    MAX_CONCURRENT_DELIVERIES_PER_USER: int = 50
    # Messaging push delivery: how long a "delivering" message can sit
    # before stale-recovery transitions it back to retry_ready.
    MESSAGE_DELIVERY_STALE_AFTER_SECONDS: int = 300
    BASE_URL: str = "http://localhost:8000"
    ALLOW_REGISTER: bool = True
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "CueAPI <noreply@cueapi.ai>"
    WORKER_HEARTBEAT_TIMEOUT_SECONDS: int = 180
    WORKER_CLAIM_TIMEOUT_SECONDS: int = 900
    WORKER_UNCLAIMED_TIMEOUT_SECONDS: int = 900
    POLLER_HEARTBEAT_TTL_SECONDS: int = 120
    POLLER_LEADER_LOCK_TTL_SECONDS: int = 30
    SESSION_SECRET: str = ""

    # ─── Dock-readiness external auth backend (PR-5c) ───────────────
    #
    # When ``EXTERNAL_AUTH_BACKEND=True``:
    #
    # 1. Activates the internal-token auth path in ``app/auth.py``.
    #    Bearer requests carrying ``INTERNAL_AUTH_TOKEN`` (constant-time
    #    compared) are treated as service-to-service calls. The caller
    #    sets the ``X-On-Behalf-Of: <user_id>`` header to specify which
    #    user the request acts as. The user must already exist in the
    #    ``users`` table (the integrator is responsible for upserting).
    #
    # 2. Implies device-code-stripping semantics — the email-magic-link
    #    signup is meaningless in this mode (the integrator owns
    #    identity).
    #
    # 3. Exposes ``PUT /v1/internal/users/{user_id}`` for the integrator
    #    to upsert user rows from its own identity system. Auth: only
    #    the INTERNAL_AUTH_TOKEN bearer can call it.
    #
    # The per-user API key path (``cue_sk_*``) and JWT session path
    # remain available — turning this flag on is additive, not
    # mutually exclusive. A self-host can support both internal-token
    # service traffic AND legacy API-key traffic (for migration).
    EXTERNAL_AUTH_BACKEND: bool = False

    # The shared service-to-service token. MUST be set when
    # EXTERNAL_AUTH_BACKEND=True; otherwise the internal-token auth
    # path is unreachable (constant-time compare against the empty
    # string would always fail anyway, but we hard-check at startup).
    # Value should be a high-entropy random string (>= 32 chars).
    # Generate with: ``python3 -c "import secrets; print(secrets.token_urlsafe(48))"``
    INTERNAL_AUTH_TOKEN: str = ""

    # ─── Dock-readiness pluggable authz backend (PR-5b) ──────────────
    #
    # Default behavior is same-tenant only (per spec §3.4). Self-host
    # integrators can override via either:
    #
    # 1. ``AUTHORIZATION_BACKEND`` — Python import path to a subclass
    #    of ``AuthorizationBackend``, format ``module.path:ClassName``.
    #    Loaded once at module import and cached. Use this when you
    #    can ship Python code in your deployment.
    #
    # 2. ``AUTHZ_HOOK_URL`` — HTTPS URL the substrate POSTs to before
    #    accepting any cross-user message. Use this when your authz
    #    logic lives in a separate service (Dock's case — calls back
    #    to ``POST /api/internal/auth/can-message`` on the Dock
    #    cloud, which joins against Dock's WorkspaceMember table).
    #
    # Both unset → SameTenantAuthorizationBackend (default).
    # Both set → AUTHORIZATION_BACKEND wins.
    AUTHORIZATION_BACKEND: str = ""
    AUTHZ_HOOK_URL: str = ""
    AUTHZ_HOOK_SECRET: str = ""

    @property
    def async_database_url(self) -> str:
        """Convert postgresql:// to postgresql+asyncpg://."""
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
