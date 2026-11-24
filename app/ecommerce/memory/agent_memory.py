#!/usr/bin/env python3
"""
Simple Agent Memory System
Stores learning from past interactions
"""

from typing import Dict, Any, List
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class AgentMemory:
    """
    Hybrid memory system for agents
    - Episodic: Recent session history
    - Semantic: Learned patterns (file-based for simplicity)
    """
    
    def __init__(self, memory_dir: str = './agent_memory'):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(exist_ok=True)
        
        # Episodic memory: Recent interactions (in-memory)
        self.episodic = deque(maxlen=100)
        
        # Semantic memory: Learned patterns (file-based)
        self.semantic_file = self.memory_dir / 'semantic_memory.json'
        self.semantic = self._load_semantic()
    
    def _load_semantic(self) -> Dict:
        """Load semantic memory from disk"""
        if self.semantic_file.exists():
            try:
                with open(self.semantic_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load semantic memory: {e}")
        
        return {'patterns': [], 'success_counts': {}}
    
    def _save_semantic(self):
        """Save semantic memory to disk with custom serialization"""
        def default_serializer(obj):
            if hasattr(obj, 'to_dict'):
                return obj.to_dict()
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            return str(obj)
            
        try:
            with open(self.semantic_file, 'w') as f:
                json.dump(self.semantic, f, indent=2, default=default_serializer)
        except Exception as e:
            logger.error(f"Could not save semantic memory: {e}")
    
    async def remember_interaction(self, interaction: Dict[str, Any]):
        """
        Store an interaction in episodic memory
        Extract patterns for semantic memory if successful
        """
        # Add to episodic memory
        self.episodic.append({
            'timestamp': interaction.get('timestamp', datetime.now().isoformat()),
            **interaction
        })
        
        # If successful, extract pattern for semantic memory
        if interaction.get('success'):
            await self._extract_pattern(interaction)
    
    async def _extract_pattern(self, interaction: Dict):
        """Extract successful pattern and store in semantic memory"""
        try:
            pattern_key = f"{interaction.get('task', 'unknown')}_{interaction.get('agent', 'unknown')}"
            
            # Increment success count
            if pattern_key not in self.semantic['success_counts']:
                self.semantic['success_counts'][pattern_key] = 0
            
            self.semantic['success_counts'][pattern_key] += 1
            
            # Store pattern context
            pattern = {
                'key': pattern_key,
                'task': interaction.get('task'),
                'agent': interaction.get('agent'),
                'context': interaction.get('context', {}),
                'success_count': self.semantic['success_counts'][pattern_key],
                'last_used': datetime.now().isoformat()
            }
            
            # Update or add pattern
            existing_idx = next(
                (i for i, p in enumerate(self.semantic['patterns']) 
                 if p.get('key') == pattern_key),
                None
            )
            
            if existing_idx is not None:
                self.semantic['patterns'][existing_idx] = pattern
            else:
                self.semantic['patterns'].append(pattern)
            
            # Save to disk
            self._save_semantic()
            
        except Exception as e:
            logger.warning(f"Could not extract pattern: {e}")
    
    async def recall_similar(self, current_context: Dict, k: int = 5) -> List[Dict]:
        """
        Retrieve similar past experiences from episodic memory
        
        Simple similarity: match on task type
        """
        task_type = current_context.get('task_type', '')
        
        # Filter episodic memory for matching task type
        matches = [
            interaction for interaction in self.episodic
            if interaction.get('task', '') == task_type
        ]
        
        # Return most recent k matches
        return list(reversed(matches))[:k]
    
    def get_pattern_stats(self) -> Dict:
        """Get statistics about learned patterns"""
        return {
            'total_patterns': len(self.semantic['patterns']),
            'total_interactions': len(self.episodic),
            'top_patterns': sorted(
                self.semantic['success_counts'].items(),
                key=lambda x: x[1],
                reverse=True
            )[:10]
        }
