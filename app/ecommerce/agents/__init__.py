"""
Agent Framework
Multi-agent system for ecommerce automation
"""

from .base_agent import BaseAgent, Task, Result, TaskStatus
from .orchestrator import AgentOrchestrator
from .variant_selection_agent import VariantSelectionAgent
from .checkout_agent import CheckoutAgent

__all__ = [
    'BaseAgent',
    'Task',
    'Result',
    'TaskStatus',
    'AgentOrchestrator',
    'VariantSelectionAgent',
    'CheckoutAgent'
]
