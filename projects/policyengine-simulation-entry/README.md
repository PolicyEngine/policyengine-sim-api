# policyengine-simulation-entry

The permanent Cloud Run entrypoint for PolicyEngine simulation submission and
polling.

During migration Stage 5 this service is a contract-compatible control-plane
proxy backed by the existing Modal-hosted simulation gateway. It authenticates
callers, uses its own machine identity for the Modal hop, and preserves existing
job identifiers. It does not yet own job persistence, version routing, or
compute dispatch.

## Local development

    uv sync --extra test
    uv run pytest tests/ -v

The production app is policyengine_simulation_entry.app:app. Required runtime
configuration is documented in policyengine_simulation_entry.config.Settings.
The service does not require its own public URL as runtime configuration.
Cloud Run's generated revision name is returned in a response header so
deployment checks can prove which revision a tagged or stable URL serves.

Live caller/backend authentication checks are isolated in `authenticated_tests`.
They qualify tagged Cloud Run candidates before production traffic changes and
verify the stable production endpoint immediately after promotion.
