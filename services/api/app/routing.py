from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class RouteDecision:
    provider: str
    model: str
    fallback_model: str
    profile: str
    location: str
    estimated_context_tokens: int
    reason: str


def route_task(settings: Settings, profile: str, context_characters: int) -> RouteDecision:
    """Apply the deterministic MVP routing policy and preserve its rationale."""
    provider = settings.provider_mode.lower()
    estimated_context_tokens = max(0, context_characters // 4)
    location = "local" if provider in {"ollama", "sglang"} else "remote"
    if profile == "strong":
        reason = "The deterministic difficulty evaluator selected the strong profile."
    else:
        reason = "The deterministic difficulty evaluator selected the fast profile."
    if estimated_context_tokens > 2_000:
        reason += " Context size is above the compact-task threshold."
    return RouteDecision(
        provider=provider,
        model=settings.model_for(profile),
        fallback_model=settings.fallback_for(),
        profile=profile,
        location=location,
        estimated_context_tokens=estimated_context_tokens,
        reason=reason,
    )
