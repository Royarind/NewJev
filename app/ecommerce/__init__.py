"""NewJev Agentic Ecommerce & Shopping Module."""

from .main_orchestrator import AgenticAutomationSystem, AutomationRequest
from .google_shopping import GoogleShoppingSession
from .profiles import CheckoutProfile
from .bridge import is_ecommerce_goal, run_ecommerce, run_google_shopping_search, run_checkout_automation

__version__ = "2.2.0"

__all__ = [
    "AgenticAutomationSystem",
    "AutomationRequest",
    "GoogleShoppingSession",
    "CheckoutProfile",
    "is_ecommerce_goal",
    "run_ecommerce",
    "run_google_shopping_search",
    "run_checkout_automation",
]
