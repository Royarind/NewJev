#!/usr/bin/env python3
"""
Main Agentic Orchestrator
Entry point for the adaptive automation system
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
import logging
import asyncio
import os

from .agents import AgentOrchestrator, Task, VariantSelectionAgent, CheckoutAgent
from .memory import AgentMemory
from .utils.llm_client import get_llm_client
from .utils.distraction_manager import DistractionManager
from .profiles import CheckoutProfile

logger = logging.getLogger(__name__)


@dataclass
class AutomationRequest:
    """Request for automation"""
    url: str
    variants: Dict[str, str]  # {type: value}, e.g., {'color': 'navy', 'size': 'L'}
    quantity: int = 1
    profile: Optional[CheckoutProfile] = None  # Address/contact information for checkout
    
    @classmethod
    def from_json(cls, json_data: Dict[str, Any]) -> 'AutomationRequest':
        """
        Create AutomationRequest from JSON dictionary
        
        Example JSON:
        {
            "url": "https://example.com/product",
            "variants": {"Color": "Blue", "Size": "L"},
            "quantity": 1,
            "profile": {
                "first_name": "John",
                "last_name": "Doe",
                "email": "john@example.com",
                ...
            }
        }
        """
        profile = None
        if 'profile' in json_data and json_data['profile']:
            profile = CheckoutProfile.from_dict(json_data['profile'])
        
        return cls(
            url=json_data['url'],
            variants=json_data.get('variants', {}),
            quantity=json_data.get('quantity', 1),
            profile=profile
        )


class AgenticAutomationSystem:
    """
    Main entry point for the adaptive agentic automation system
    
    Usage:
        system = AgenticAutomationSystem()
        await system.initialize(page)
        result = await system.automate(AutomationRequest(...))
    """
    
    def __init__(self):
        self.orchestrator = AgentOrchestrator()
        self.memory = AgentMemory()
        self.llm = get_llm_client()
        self.page = None
        self.distraction_manager = None
        self.initialized = False
    
    async def initialize(self, page):
        """
        Initialize the system with a Playwright page
        """
        self.page = page
        
        # Ensure consistent viewport for VLM reliability
        try:
            current_size = page.viewport_size
            if not current_size or current_size['width'] != 1280:
                logger.info("Setting standard viewport 1280x800 for VLM consistency")
                await page.set_viewport_size({"width": 1280, "height": 800})
        except Exception as e:
            logger.warning(f"Could not set viewport: {e}")
            
        self.distraction_manager = DistractionManager(page, llm_client=self.llm)
        
        # Register agents
        variant_agent = VariantSelectionAgent(
            page=page,
            llm_client=self.llm,
            memory=self.memory
        )
        self.orchestrator.register_agent(variant_agent)
        
        checkout_agent = CheckoutAgent(
            page=page,
            llm_client=self.llm,
            memory=self.memory
        )
        self.orchestrator.register_agent(checkout_agent)
        
        # TODO: Register more agents as they're implemented
        # - NavigationAgent
        # - FormFillingAgent
        
        self.initialized = True
        logger.info("Agentic automation system initialized")
    
    async def automate(self, request: AutomationRequest) -> Dict[str, Any]:
        """
        Run full automation for a product
        
        Args:
            request: AutomationRequest with URL and variant selections
        
        Returns:
            Result dictionary with success status and details
        """
        if not self.initialized:
            return {
                'success': False,
                'error': 'System not initialized. Call initialize(page) first.'
            }
        
        logger.info(f"Starting automation for: {request.url}")
        
        # Store profile for use by checkout agents
        self.profile = request.profile
        
        try:
            # 1. Clear previous session's cart items by clearing cookies/storage for target domain
            logger.info("Clearing target domain cookies and storage to reset any previous cart items...")
            try:
                from urllib.parse import urlparse
                parsed_url = urlparse(request.url)
                domain = parsed_url.netloc
                
                # Clear cookies for target domain
                await self.page.context.clear_cookies(domain=domain)
                if domain.startswith("www."):
                    sub_domain = domain[3:]
                    await self.page.context.clear_cookies(domain=sub_domain)
                
                # Navigate to domain to clear localStorage/sessionStorage
                logger.info(f"Navigating to {request.url} to clear local/session storage...")
                await self.page.goto(request.url, wait_until='domcontentloaded', timeout=60000)
                await self.page.evaluate("""() => {
                    try {
                        localStorage.clear();
                        sessionStorage.clear();
                    } catch (e) {
                        console.error('Failed to clear storage:', e);
                    }
                }""")
                
                # 2. Explicitly clear any server-persisted cart items by visiting cart and removing them
                logger.info("Checking cart for existing items from previous sessions...")
                cart_url_patterns = [
                    f"https://{domain}/cart",
                    f"https://{domain}/bag",
                    f"https://{domain}/basket",
                    f"https://www.{domain}/cart" if not domain.startswith('www.') else None,
                ]
                for cart_url in cart_url_patterns:
                    if not cart_url:
                        continue
                    try:
                        resp = await self.page.goto(cart_url, wait_until='domcontentloaded', timeout=10000)
                        if resp and resp.ok:
                            await asyncio.sleep(1.5)
                            # Try to click Remove / Delete buttons on cart items
                            remove_selectors = [
                                'button:has-text("Remove")', 'a:has-text("Remove")',
                                'button:has-text("Delete")', 'a:has-text("Delete")',
                                '[data-action="remove"]', '[data-testid*="remove" i]',
                                '[class*="remove" i] button', 'button[aria-label*="remove" i]',
                                'button[aria-label*="delete" i]',
                                '.cart-item__remove', '.remove-item', '.delete-item',
                            ]
                            for attempt_clear in range(10):  # Try up to 10 times for multiple items
                                clicked_remove = False
                                for sel in remove_selectors:
                                    try:
                                        items = await self.page.query_selector_all(sel)
                                        for item in items:
                                            if await item.is_visible():
                                                await item.click()
                                                await asyncio.sleep(1)
                                                clicked_remove = True
                                                break  # After removing one, recheck
                                        if clicked_remove:
                                            break
                                    except:
                                        pass
                                if not clicked_remove:
                                    break  # No more remove buttons found
                            logger.info("Cart clearing complete.")
                            break  # Found and handled a cart page
                    except Exception as cart_e:
                        logger.debug(f"Cart URL {cart_url} not accessible: {cart_e}")
                        continue
            except Exception as e:
                logger.warning(f"Could not clear target domain storage/cookies: {e}")
                
            # Navigation with better wait
            logger.info(f"Reloading {request.url} with clean state...")
            await self.page.goto(request.url, wait_until='load', timeout=60000)
            await asyncio.sleep(1)  # Brief settle time
            
            # CLEAR DISTRACTIONS (Pop-ups, Cookies, etc.)
            logger.info("Waiting for clean viewport (removing pop-ups/modals)...")
            is_clean = await self.distraction_manager.wait_for_clean_viewport(max_wait=20.0)
            
            if not is_clean:
                logger.warning("Viewport may not be fully clean, but proceeding with automation tasks...")
            else:
                logger.info("Viewport confirmed clean. Starting automation tasks.")
            
            # TAKE VISUAL CONFIRMATION
            try:
                screenshot_path = os.path.join(os.getcwd(), f"viewport_clean_{int(asyncio.get_event_loop().time())}.png")
                await self.page.screenshot(path=screenshot_path)
                logger.info(f"Visual confirmation saved to: {screenshot_path}")
            except Exception as e:
                logger.warning(f"Could not take visual confirmation: {e}")
            
            await asyncio.sleep(1) 
        except Exception as e:
            logger.warning(f"Navigation or initial clearing had issues: {e}. Proceeding anyway...")
        
        # Create tasks for variant selection
        tasks = []
        
        for variant_type, variant_value in request.variants.items():
            task = Task(
                type='select_variant',
                description=f"Select {variant_type}: {variant_value}",
                context={
                    'variant_type': variant_type,
                    'variant_value': variant_value,
                    'url': request.url
                },
                priority=8  # High priority
            )
            tasks.append(task)
        
        # Add quantity task if needed
        if request.quantity > 1:
            tasks.append(Task(
                type='quantity',
                description=f"Set quantity to {request.quantity}",
                context={
                    'variant_type': 'quantity',
                    'variant_value': str(request.quantity),
                    'url': request.url
                },
                priority=6
            ))
        
        # Add to Cart task
        tasks.append(Task(
            type='add_to_cart',
            description="Click Add to Cart",
            context={
                'url': request.url,
                'expected_variants': request.variants,
                'quantity': request.quantity
            },
            priority=5.5
        ))

        # Add tasks for cart navigation and checkout
        tasks.append(Task(
            type='navigate_to_cart',
            description="Navigate to Cart and proceed to checkout",
            context={'url': request.url},
            priority=5
        ))
        
        tasks.append(Task(
            type='login_or_guest',
            description="Handle login or guest checkout wall",
            context={'url': request.url, 'profile': request.profile},
            priority=4.5
        ))
        
        tasks.append(Task(
            type='fill_contact',
            description="Fill contact information",
            context={'url': request.url, 'profile': request.profile},
            priority=4
        ))
        
        tasks.append(Task(
            type='fill_shipping',
            description="Fill shipping information",
            context={'url': request.url, 'profile': request.profile},
            priority=3
        ))
        
        tasks.append(Task(
            type='select_shipping_method',
            description="Select best shipping method",
            context={'url': request.url},
            priority=2
        ))
        
        tasks.append(Task(
            type='place_order',
            description="Place order (Review only)",
            context={'url': request.url},
            priority=1
        ))
        
        # Execute all tasks
        results = await self.orchestrator.execute_workflow(tasks)
        
        # Aggregate results
        successful = sum(1 for r in results if r.success)
        total = len(results)
        
        return {
            'success': successful == total,
            'total_tasks': total,
            'successful_tasks': successful,
            'failed_tasks': total - successful,
            'results': [
                {
                    'task': r.task.description,
                    'type': r.task.type,
                    'success': r.success,
                    'method': r.data.get('method') if r.data else None,
                    'confidence': r.confidence,
                    'error': r.error
                }
                for r in results
            ],
            'agent_stats': self.orchestrator.get_stats(),
            'memory_stats': self.memory.get_pattern_stats()
        }
    
    async def select_variant(self, variant_type: str, variant_value: str) -> Dict[str, Any]:
        """
        Select a single variant (convenience method)
        
        Args:
            variant_type: Type of variant (color, size, etc.)
            variant_value: Value to select
        
        Returns:
            Result dictionary
        """
        task = Task(
            type='select_variant',
            description=f"Select {variant_type}: {variant_value}",
            context={
                'variant_type': variant_type,
                'variant_value': variant_value
            }
        )
        
        result = await self.orchestrator.execute_task(task)
        
        return result.__dict__
