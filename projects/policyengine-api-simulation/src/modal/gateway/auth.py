"""Gateway authentication primitives.

The gateway is a public-facing ASGI app that routes simulation submission and
polling requests to versioned worker apps. Every write/mutation endpoint and
every read endpoint that exposes per-job state must require a valid bearer
token issued by the PolicyEngine identity provider. This module exposes a
FastAPI dependency (:func:`require_auth`) that callers attach with
``Depends(require_auth)``.

Configuration is read from the environment at import time so that Modal's
runtime container picks up the values injected via ``modal.Secret``:

- ``GATEWAY_AUTH_ISSUER`` - Auth0 issuer URL (must end with ``/``)
- ``GATEWAY_AUTH_AUDIENCE`` - Auth0 API identifier the gateway accepts
- ``GATEWAY_AUTH_REQUIRED`` - if truthy, bearer JWT auth is enforced

For local development and unit tests the dependency can be bypassed by
setting ``GATEWAY_AUTH_DISABLED=1``. This bypass is hard-gated by
:func:`enforce_production_auth_guard`, which is called from the gateway
ASGI factory at startup: it refuses to boot when ``MODAL_ENVIRONMENT`` is
missing or looks like production, and otherwise requires an explicit
``GATEWAY_AUTH_DISABLED_ACK=I_UNDERSTAND_THIS_IS_DEV`` acknowledgement so
the bypass cannot be activated by a single stray env var. The gateway
also returns ``503`` to callers if only one of issuer/audience is present, or
if auth is required but issuer/audience are missing.
"""

from __future__ import annotations

import functools
import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from policyengine_observability import record_event
from policyengine_fastapi.auth import JWTDecoder
from src.modal.logfire_legacy import legacy_logfire_attributes

logger = logging.getLogger(__name__)


GATEWAY_AUTH_ISSUER_ENV = "GATEWAY_AUTH_ISSUER"
GATEWAY_AUTH_AUDIENCE_ENV = "GATEWAY_AUTH_AUDIENCE"
GATEWAY_AUTH_REQUIRED_ENV = "GATEWAY_AUTH_REQUIRED"
GATEWAY_AUTH_DISABLED_ENV = "GATEWAY_AUTH_DISABLED"
GATEWAY_AUTH_DISABLED_ACK_ENV = "GATEWAY_AUTH_DISABLED_ACK"
GATEWAY_AUTH_DISABLED_ACK_VALUE = "I_UNDERSTAND_THIS_IS_DEV"

# Modal injects ``MODAL_ENVIRONMENT`` into every container. Any of these
# values are treated as production-equivalent: refuse to start the gateway
# with auth disabled in them. If the env var is unset we also refuse,
# because "unset" is the default state of a mis-deployed container and we
# don't want the auth bypass to silently activate there.
PRODUCTION_MODAL_ENVIRONMENTS = frozenset({"main", "prod", "production"})
MODAL_ENVIRONMENT_ENV = "MODAL_ENVIRONMENT"


_bearer_scheme = HTTPBearer(auto_error=False)


def _auth_disabled() -> bool:
    return os.environ.get(GATEWAY_AUTH_DISABLED_ENV, "").lower() in {
        "1",
        "true",
        "yes",
    }


def _auth_required() -> bool:
    return os.environ.get(GATEWAY_AUTH_REQUIRED_ENV, "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@functools.lru_cache(maxsize=8)
def _build_decoder(issuer: str, audience: str) -> JWTDecoder:
    """Construct and cache a ``JWTDecoder`` keyed by issuer/audience.

    The decoder wraps a ``PyJWKClient`` that caches JWKS responses internally.
    Rebuilding the decoder on every request defeats that cache and forces a
    live JWKS fetch per call (see PR #458 review). Caching here with
    ``functools.lru_cache`` keeps a single decoder instance per
    (issuer, audience) pair for the lifetime of the process, so the JWKS
    client's own LRU cache is reused across requests.

    The cache is keyed on the resolved env values so that test suites that
    mutate ``GATEWAY_AUTH_ISSUER`` / ``GATEWAY_AUTH_AUDIENCE`` still observe
    the right decoder without a stale instance bleeding across tests.
    """
    return JWTDecoder(issuer=issuer, audience=audience, auto_error=True)


def _get_decoder() -> JWTDecoder:
    """Resolve the cached ``JWTDecoder`` for the current env configuration."""
    issuer = os.environ.get(GATEWAY_AUTH_ISSUER_ENV)
    audience = os.environ.get(GATEWAY_AUTH_AUDIENCE_ENV)
    if not issuer or not audience:
        raise RuntimeError(
            "Gateway auth misconfigured: set "
            f"{GATEWAY_AUTH_ISSUER_ENV} and {GATEWAY_AUTH_AUDIENCE_ENV} or "
            f"{GATEWAY_AUTH_DISABLED_ENV}=1 for local/test use."
        )
    if not issuer.endswith("/"):
        issuer = issuer + "/"
    return _build_decoder(issuer, audience)


def reset_decoder_cache() -> None:
    """Clear the cached decoder. Intended for tests and process restarts."""
    _build_decoder.cache_clear()


class AuthDisabledInProductionError(RuntimeError):
    """Refuse to start when auth is disabled in a production-equivalent env."""


class AuthDisabledWithoutAckError(RuntimeError):
    """Refuse to start when auth is disabled without the explicit ACK."""


class AuthMisconfiguredError(RuntimeError):
    """Refuse to start when required auth config is absent or partial."""


def enforce_production_auth_guard() -> None:
    """Validate at startup that the auth-disabled bypass is only used in dev.

    Must be called from the ASGI app factory (``gateway.app.web_app``) before
    serving any requests. The guard has three tiers so that accidental
    production deploys cannot reach the "auth disabled" code path:

    1. If ``GATEWAY_AUTH_DISABLED`` is not set, do nothing.
    2. If set and ``MODAL_ENVIRONMENT`` is missing or looks like production
       (``main``, ``prod``, ``production``), raise so the container crashes
       at import time. ``modal serve`` / ``modal deploy`` surface the
       traceback instead of silently serving an unprotected gateway.
    3. Otherwise require an explicit acknowledgement env var
       (``GATEWAY_AUTH_DISABLED_ACK=I_UNDERSTAND_THIS_IS_DEV``). This makes
       the bypass impossible to set via one stray env var — an operator
       must actively opt in.

    Even when the guard passes, emit a ``CRITICAL`` log, a structured
    observability event, and a legacy Logfire event so any audit of the
    service's logs surfaces the bypass immediately.
    """
    if not _auth_disabled():
        return

    modal_env = os.environ.get(MODAL_ENVIRONMENT_ENV)
    ack = os.environ.get(GATEWAY_AUTH_DISABLED_ACK_ENV, "")

    if modal_env is None or modal_env.lower() in PRODUCTION_MODAL_ENVIRONMENTS:
        raise AuthDisabledInProductionError(
            f"Refusing to start gateway with {GATEWAY_AUTH_DISABLED_ENV}=1 "
            f"when {MODAL_ENVIRONMENT_ENV}={modal_env!r}. "
            "Disabling auth is only permitted in ephemeral dev environments."
        )

    if ack != GATEWAY_AUTH_DISABLED_ACK_VALUE:
        raise AuthDisabledWithoutAckError(
            f"Refusing to start gateway with {GATEWAY_AUTH_DISABLED_ENV}=1 "
            f"unless {GATEWAY_AUTH_DISABLED_ACK_ENV}="
            f"{GATEWAY_AUTH_DISABLED_ACK_VALUE} is also set."
        )

    banner = (
        "\n"
        "!! GATEWAY AUTH IS DISABLED !! "
        f"MODAL_ENVIRONMENT={modal_env!r}. "
        "This MUST NOT reach production. If you see this in prod logs, "
        "roll back immediately."
    )
    logger.critical(banner)
    try:
        record_event(
            "gateway_auth_disabled_bypass_active",
            modal_environment=modal_env,
            ack_value_present=True,
            **legacy_logfire_attributes(),
        )
    except Exception:  # pragma: no cover - observability must never block startup
        pass
    try:
        import logfire

        instance = getattr(logfire, "DEFAULT_LOGFIRE_INSTANCE", None)
        if instance is not None and bool(
            getattr(instance.config, "send_to_logfire", False)
        ):
            logfire.error(
                "gateway_auth_disabled_bypass_active",
                modal_environment=modal_env,
                ack_value_present=True,
                **legacy_logfire_attributes(),
            )
    except Exception:  # pragma: no cover - logfire optional / misconfigured
        pass


def enforce_auth_configured_guard() -> None:
    """Crash startup when gateway auth would serve broken gated endpoints.

    Rules:
    - auth disabled: allow startup; the separate production guard handles safety.
    - partial issuer/audience config: always refuse startup because every gated
      endpoint would return 503 in that state.
    - auth required and both values missing: refuse startup.
    - auth optional and both values missing: allow startup (public gateway mode).
    """
    if _auth_disabled():
        return

    issuer = os.environ.get(GATEWAY_AUTH_ISSUER_ENV)
    audience = os.environ.get(GATEWAY_AUTH_AUDIENCE_ENV)

    if bool(issuer) != bool(audience):
        raise AuthMisconfiguredError(
            "Gateway auth is partially configured: set both "
            f"{GATEWAY_AUTH_ISSUER_ENV} and {GATEWAY_AUTH_AUDIENCE_ENV}, "
            "or clear both."
        )

    if _auth_required() and (not issuer or not audience):
        raise AuthMisconfiguredError(
            "Gateway auth is required but "
            f"{GATEWAY_AUTH_ISSUER_ENV}/{GATEWAY_AUTH_AUDIENCE_ENV} are not set "
            "in the container environment. Verify the "
            "'policyengine-gateway-auth' Modal secret is synced correctly."
        )


def require_auth(
    token: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict | None:
    """FastAPI dependency gating an endpoint behind a bearer JWT.

    Resolution rules:
    1. If ``GATEWAY_AUTH_DISABLED`` is truthy, accept the request without
       any token inspection so tests and local reruns don't need to wire
       fake JWT material.
    2. Otherwise, validate the bearer token via :class:`JWTDecoder`. A
       missing or invalid token produces a 403 (matching the underlying
       decoder's contract).

    The gateway preserves its legacy public behavior unless
    ``GATEWAY_AUTH_REQUIRED`` is truthy. Issuer/audience may be staged in the
    Modal secret ahead of enforcement; setting those values alone must not
    silently make the gateway private. Partial auth configuration always
    returns 503 because it indicates an incomplete secret.
    """

    if _auth_disabled():
        return None

    issuer = os.environ.get(GATEWAY_AUTH_ISSUER_ENV)
    audience = os.environ.get(GATEWAY_AUTH_AUDIENCE_ENV)
    if bool(issuer) != bool(audience):
        logger.error(
            "Gateway auth partially configured: issuer_present=%s audience_present=%s",
            bool(issuer),
            bool(audience),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway authentication is not configured.",
        )
    if not _auth_required():
        return None

    try:
        decoder = _get_decoder()
    except RuntimeError as exc:
        logger.error("Gateway auth misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway authentication is not configured.",
        )

    return decoder(token)
