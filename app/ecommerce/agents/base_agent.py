#!/usr/bin/env python3
"""
Base Agent Framework
All specialized agents inherit from BaseAgent
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum
import logging
import asyncio
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Status of agent task execution"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"


@dataclass
class Task:
    """Represents a task for an agent"""
    type: str
    description: str
    context: Dict[str, Any]
    priority: int = 5
    max_retries: int = 3
    retry_count: int = 0
    
    def __str__(self):
        return f"Task({self.type}: {self.description})"


@dataclass
class Result:
    """Result from agent execution"""
    success: bool
    task: Task
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    reasoning: Optional[str] = None
    confidence: float = 0.0
    execution_time: float = 0.0
    
    def __str__(self):
        status = "SUCCESS" if self.success else "FAILED"
        return f"Result({status}, confidence={self.confidence:.2f})"


class BaseAgent(ABC):
    """
    Abstract base class for all agents
    Provides common functionality: logging, memory, self-reflection
    """
    
    def __init__(self, name: str, llm_client=None, memory=None):
        self.name = name
        self.llm = llm_client
        self.memory = memory
        self.success_rate = 0.0
        self.total_tasks = 0
        self.successful_tasks = 0
        self.logger = logging.getLogger(f"agent.{name}")
    
    @abstractmethod
    async def can_handle(self, task: Task) -> float:
        """
        Return confidence score (0-1) for handling this task
        
        Args:
            task: Task to evaluate
            
        Returns:
            Confidence score (0.0 = cannot handle, 1.0 = perfect match)
        """
        pass
    
    @abstractmethod
    async def execute(self, task: Task) -> Result:
        """
        Execute the task and return structured result
        
        Args:
            task: Task to execute
            
        Returns:
            Result object with execution outcome
        """
        pass
    
    async def run(self, task: Task) -> Result:
        """
        Main execution method with error handling and logging
        
        This wraps execute() with:
        - Pre-execution checks
        - Timing
        - Error handling
        - Post-execution reflection
        """
        start_time = datetime.now()
        self.logger.info(f"Starting task: {task}")
        
        try:
            # Check if we can handle this task
            confidence = await self.can_handle(task)
            if confidence < 0.3:
                return Result(
                    success=False,
                    task=task,
                    error=f"Low confidence ({confidence:.2f}) for this task",
                    confidence=confidence
                )
            
            # Execute the task
            result = await self.execute(task)
            
            # Calculate execution time
            execution_time = (datetime.now() - start_time).total_seconds()
            result.execution_time = execution_time
            
            # Update statistics
            self.total_tasks += 1
            if result.success:
                self.successful_tasks += 1
            
            # Self-reflection
            await self.reflect(result)
            
            self.logger.info(f"Task completed: {result}")
            return result
            
        except Exception as e:
            self.logger.error(f"Task execution failed: {e}", exc_info=True)
            execution_time = (datetime.now() - start_time).total_seconds()
            
            return Result(
                success=False,
                task=task,
                error=str(e),
                execution_time=execution_time
            )
    
    async def reflect(self, result: Result):
        """
        Self-reflection: update internal model based on outcome
        
        This method is called after each task execution to:
        - Update success rate
        - Learn from failures
        - Store patterns in memory
        """
        # Update success rate with exponential moving average
        if result.success:
            self.success_rate = 0.9 * self.success_rate + 0.1 * 1.0
        else:
            self.success_rate = 0.9 * self.success_rate
            await self._analyze_failure(result)
        
        # Store in memory if available
        if self.memory:
            await self.memory.remember_interaction({
                'agent': self.name,
                'task': result.task.type,
                'success': result.success,
                'context': result.task.context,
                'result': result.data,
                'timestamp': datetime.now().isoformat()
            })
    
    async def _analyze_failure(self, result: Result):
        """
        Analyze why a task failed and learn from it
        
        Uses LLM (if available) to understand root cause
        """
        if not self.llm:
            return
            
        # Optional: Allow disabling reflection via environment variable
        if os.getenv('DISABLE_AGENT_REFLECTION', 'false').lower() == 'true':
            return
        
        try:
            # Check for credits error in previous messages to avoid repeated 402s
            if result.error and ("402" in result.error or "Payment Required" in result.error):
                self.logger.warning("Skipping failure analysis due to detected credit error (402).")
                return

            analysis = await self.llm.complete(f"""
            Analyze this agent failure:
            
            Agent: {self.name}
            Task: {result.task.type} - {result.task.description}
            Error: {result.error}
            Context: {result.task.context}
            
            What went wrong and how can the agent improve?
            Return JSON with: {{"root_cause": str, "suggestions": list}}
            """, max_tokens=256) # Keep it small to save credits
            
            self.logger.warning(f"Failure analysis: {analysis}")
            
        except Exception as e:
            # Handle 402 gracefully
            if "402" in str(e) or "Payment Required" in str(e):
                self.logger.warning("Failure analysis skipped: OpenRouter credits exhausted (402).")
            else:
                self.logger.error(f"Could not analyze failure: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get agent performance statistics"""
        return {
            'name': self.name,
            'success_rate': self.success_rate,
            'total_tasks': self.total_tasks,
            'successful_tasks': self.successful_tasks,
            'failed_tasks': self.total_tasks - self.successful_tasks
        }
    
    async def recall_similar_tasks(self, task: Task, k: int = 5) -> List[Dict]:
        """
        Recall similar past tasks from memory
        
        Args:
            task: Current task
            k: Number of similar tasks to retrieve
            
        Returns:
            List of similar past interactions
        """
        if not self.memory:
            return []
        
        try:
            similar = await self.memory.recall_similar({
                'task_type': task.type,
                'description': task.description,
                'context': task.context
            }, k=k)
            
            return similar
        except Exception as e:
            self.logger.warning(f"Could not recall similar tasks: {e}")
            return []
