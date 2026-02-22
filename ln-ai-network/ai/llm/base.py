from abc import ABC, abstractmethod
from typing import List, Dict, Any

class LLMBackend(ABC):

    @abstractmethod
    def step(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Returns either:
        - tool call request
        - final response
        """
        pass
