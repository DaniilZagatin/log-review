"""Independent researcher configuration, using Belka's existing model client."""
from __future__ import annotations

import os

from pko import defaults
from pko.errors import PkoError
from pko.llm.registry import ModelSpec, context_window_for


def researcher_spec(thinking: bool | None = None) -> ModelSpec:
    """Keep existing connection settings unless a researcher override is explicit.

    An endpoint override must never silently forward the previous endpoint's
    credentials or client certificate to a different host.
    """
    def first(*names):
        return next((os.environ[name].strip() for name in names if os.environ.get(name, "").strip()), "")

    legacy_url = first("PKO_MATCHER_BASE_URL", "PKO_ASSEMBLER_BASE_URL", "GLM_52_API", "GLM_51_API") or defaults.GLM_BASE_URL
    url = (first("PKO_SCENARIO_BASE_URL") or legacy_url).rstrip("/")
    model = first("PKO_SCENARIO_MODEL", "PKO_MATCHER_MODEL", "PKO_ASSEMBLER_MODEL", "GLM_MODEL_NAME") or defaults.DEFAULT_MODEL
    if not url or not model:
        raise PkoError("Модель исследователя не настроена.",
                       hint="задайте PKO_SCENARIO_BASE_URL и PKO_SCENARIO_MODEL")
    different_endpoint = url != legacy_url.rstrip("/")
    key = first("PKO_SCENARIO_API_KEY")
    cert, cert_key = first("PKO_SCENARIO_CLIENT_CERT"), first("PKO_SCENARIO_CLIENT_KEY")
    if not different_endpoint:
        key = key or first("PKO_MATCHER_API_KEY", "PKO_ASSEMBLER_API_KEY", "GLM_API_KEY")
        if not cert and not cert_key:
            cert = first("PKO_MATCHER_CLIENT_CERT", "PKO_ASSEMBLER_CLIENT_CERT", "PKO_CLIENT_CERT")
            cert_key = first("PKO_MATCHER_CLIENT_KEY", "PKO_ASSEMBLER_CLIENT_KEY", "PKO_CLIENT_KEY")
    if bool(cert) != bool(cert_key):
        raise PkoError("mTLS исследователя настроен частично.",
                       hint="задайте вместе PKO_SCENARIO_CLIENT_CERT и PKO_SCENARIO_CLIENT_KEY")
    if thinking is None:
        thinking = first("PKO_THINKING").lower() in ("1", "true", "yes", "on") if first("PKO_THINKING") else not different_endpoint
    extra = {"chat_template_kwargs": {"enable_thinking": bool(thinking)}} if thinking or "glm" in model.lower() else None
    return ModelSpec(role="scenario_researcher", base_url=url, model=model,
                     api_key=key or "not-needed", client_cert=cert, client_key=cert_key,
                     extra_body=extra, context_window=context_window_for(model))
