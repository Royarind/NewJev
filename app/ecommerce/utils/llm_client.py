#!/usr/bin/env python3
"""
Simple LLM Client Wrapper
Supports OpenRouter, Groq, and Gemini
"""

import os
from typing import Optional, Dict, Any
import logging
import openai
import json
from PIL import Image

logger = logging.getLogger(__name__)


class SimpleLLMClient:
    """
    Simple LLM client that works with text-only models
    No vision capabilities needed
    """
    
    def __init__(self):
        self.provider = os.getenv('TEXT_LLM_PROVIDER') or os.getenv('LLM_PROVIDER', 'openrouter')
        
        if self.provider == 'openrouter':
            self.api_key = os.getenv('OPENROUTER_API_KEY')
            self.model = os.getenv('TEXT_LLM_MODEL') or os.getenv('OPENROUTER_MODEL', 'nvidia/nemotron-3-ultra-550b-a55b:free')
            self.base_url = "https://openrouter.ai/api/v1"
        elif self.provider == 'groq':
            self.api_key = os.getenv('GROQ_API_KEY')
            self.model = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
            self.base_url = "https://api.groq.com/openai/v1"
        elif self.provider == 'gemini':
            self.api_key = os.getenv('GEMINI_API_KEY')
            self.model = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')
            self.base_url = None  # Gemini uses different API
        else:
            self.api_key = os.getenv('OPENROUTER_API_KEY')
            self.model = os.getenv('TEXT_LLM_MODEL', 'nvidia/nemotron-3-ultra-550b-a55b:free')
            self.base_url = "https://openrouter.ai/api/v1"
            self.provider = 'openrouter'
        
        self.client = self._init_client()
        
        # Initialize backup Gemini client if key is available (essential for vision routing when using non-vision groq)
        self.gemini_backup_key = os.getenv('GEMINI_API_KEY')
        self.gemini_backup_model_name = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')
        self.gemini_backup_client = None
        if self.gemini_backup_key and self.provider != 'gemini':
            try:
                import google.generativeai as genai
                # Configure backup genai instance
                genai.configure(api_key=self.gemini_backup_key)
                self.gemini_backup_client = genai.GenerativeModel(self.gemini_backup_model_name)
                logger.info(f"Initialized backup Gemini client with model: {self.gemini_backup_model_name}")
            except Exception as e:
                logger.warning(f"Could not initialize backup Gemini client: {e}")
                
        # Initialize backup OpenRouter client if key is available (essential for vision routing fallback)
        self.openrouter_backup_key = os.getenv('OPENROUTER_API_KEY')
        self.openrouter_backup_client = None
        if self.openrouter_backup_key and self.provider != 'openrouter':
            try:
                self.openrouter_backup_client = openai.OpenAI(
                    api_key=self.openrouter_backup_key,
                    base_url="https://openrouter.ai/api/v1"
                )
                logger.info("Initialized backup OpenRouter client for cross-provider vision tasks.")
            except Exception as e:
                logger.warning(f"Could not initialize backup OpenRouter client: {e}")
    
    def _init_client(self):
        """Initialize OpenAI-compatible client"""
        if self.provider == 'gemini':
            # Use Gemini SDK
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                return genai.GenerativeModel(self.model)
            except ImportError:
                logger.error("google-generativeai not installed. Install with: pip install google-generativeai")
                return None
        else:
            # OpenAI-compatible (OpenRouter, Groq)
            return openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )
    
    async def complete(self, prompt: str, **kwargs) -> str:
        """
        Get LLM completion with automatic fallback
        """
        try:
            from app.llm import get_text_llm
            resilient = get_text_llm()
            resp = resilient.complete([{"role": "user", "content": prompt}])
            if resp and resp.content and resp.content.strip():
                return resp.content.strip()
        except Exception as r_err:
            logger.debug(f"NewJev resilient LLM bridge error: {r_err}, trying direct client fallback")

        # Build fallback list
        primary_model = kwargs.get('model', self.model)
        fallbacks = [primary_model]
        
        if self.provider == 'openrouter':
            fb1 = os.getenv('OPENROUTER_FALLBACK_MODEL')
            fb2 = os.getenv('SECONDARY_LLM_FALLBACK')
            if fb1: fallbacks.append(fb1)
            if fb2: fallbacks.append(fb2)
        
        last_error = None
        for model in fallbacks:
            try:
                logger.info(f"LLM Chat: Trying model {model}")
                if self.provider == 'gemini':
                    response = self.client.generate_content(prompt)
                    return response.text
                else:
                    # OpenAI-compatible API
                    response = self.client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=kwargs.get('temperature', 0.7),
                        max_tokens=kwargs.get('max_tokens', 1024)
                    )
                    return response.choices[0].message.content
            
            except Exception as e:
                last_error = str(e)
                # Only fallback on credit/rate limit errors
                if "402" in last_error or "429" in last_error or "credit" in last_error.lower():
                    logger.warning(f"Model {model} failed with credit/rate error. Trying fallback...")
                    continue
                else:
                    logger.error(f"LLM completion failed for {model}: {e}")
                    break # Critical failure
        
        return json.dumps({"error": last_error or "All models failed"})

    async def complete_with_vision(self, prompt: str, image_path: str, **kwargs) -> str:
        """
        Get LLM completion with vision and automatic fallback across providers.
        """
        import base64
        
        # Determine mime type from extension
        ext = os.path.splitext(image_path)[1].lower()
        mime_type = "image/png" if ext == ".png" else "image/jpeg"
        
        # Read and encode image
        try:
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            return json.dumps({"error": f"Failed to read image: {e}"})

        # --- Tier 1: Try Gemini API directly ---
        # (either as primary provider, or as backup client for Groq/OpenRouter)
        gemini_client = None
        if self.provider == 'gemini':
            gemini_client = self.client
        elif self.gemini_backup_client:
            gemini_client = self.gemini_backup_client

        if gemini_client:
            try:
                logger.info("Routing vision task to Gemini client...")
                from PIL import Image
                img = Image.open(image_path)
                response = gemini_client.generate_content([prompt, img])
                return response.text
            except Exception as e:
                logger.warning(f"Gemini vision call failed: {e}")
                # Fall through to next tier

        # --- Tier 2: Try OpenRouter backup/primary client with vision-capable models ---
        openrouter_client = None
        if self.provider == 'openrouter':
            openrouter_client = self.client
        elif self.openrouter_backup_client:
            openrouter_client = self.openrouter_backup_client

        if openrouter_client:
            # We use a set of known-good OpenRouter vision models
            openrouter_models = []
            
            # 1. Use the model passed to the function if it's likely a vision model (has -vl, qwen, etc.)
            req_model = kwargs.get('model')
            if req_model and any(x in req_model.lower() for x in ['vl', 'vision', 'gemini', 'qwen2']):
                openrouter_models.append(req_model)
                
            # 2. Add fallback models
            vfb = os.getenv('VLM_FALLBACK_MODEL')
            if vfb:
                openrouter_models.append(vfb)
            
            openrouter_models.extend([
                'openrouter/free',
                'google/gemini-2.0-flash-exp:free',
                'google/gemini-2.0-flash-lite-preview-02-05:free',
                'qwen/qwen-2-vl-72b-instruct',
                'nvidia/nemotron-nano-12b-v2-vl:free'
            ])
            
            # Deduplicate models preserving order
            seen = set()
            openrouter_models = [x for x in openrouter_models if not (x in seen or seen.add(x))]

            for model in openrouter_models:
                try:
                    logger.info(f"Trying OpenRouter vision fallback model: {model}")
                    response = openrouter_client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                                    }
                                ]
                            }
                        ],
                        temperature=kwargs.get('temperature', 0.2),
                        max_tokens=kwargs.get('max_tokens', 1024)
                    )
                    return response.choices[0].message.content
                except Exception as e:
                    logger.warning(f"OpenRouter vision model {model} failed: {e}")
                    # Continue trying next OpenRouter model

        # --- Tier 3: General OpenAI-compatible vision call (if not Groq or already handled) ---
        if self.provider not in ('groq', 'gemini', 'openrouter'):
            # Generic OpenAI compatible provider (e.g. local vLLM or custom endpoint that supports vision)
            try:
                model = kwargs.get('model', self.model)
                logger.info(f"Trying generic OpenAI-compatible vision call with model: {model}")
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                                }
                            ]
                        }
                    ],
                    temperature=kwargs.get('temperature', 0.2),
                    max_tokens=kwargs.get('max_tokens', 1024)
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"Generic OpenAI-compatible vision call failed: {e}")

        return json.dumps({"error": "All vision models and backup providers failed/exhausted quota"})


def get_llm_client() -> Optional[SimpleLLMClient]:
    """Get singleton LLM client"""
    try:
        # Check if API key is available
        provider = os.getenv('LLM_PROVIDER', 'openrouter')
        api_key = None
        
        if provider == 'openrouter':
            api_key = os.getenv('OPENROUTER_API_KEY')
        elif provider == 'groq':
            api_key = os.getenv('GROQ_API_KEY')
        elif provider == 'gemini':
            api_key = os.getenv('GEMINI_API_KEY')
        
        if not api_key:
            logger.info("No LLM API key found - LLM tier disabled")
            return None
        
        return SimpleLLMClient()
    except Exception as e:
        logger.warning(f"LLM client initialization failed: {e}")
        return None
