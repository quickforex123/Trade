"""Research committee (LLM intelligence layer).

LLMs here PROPOSE only: the committee's product is a ResearchOpinion consumed
by the deterministic fusion layer. Nothing in this package may import from
qft.risk, qft.execution or qft.brokers.
"""

from qft.research.committee import ResearchCommittee
from qft.research.llm import AnthropicLLM, FakeLLM, StructuredLLM

__all__ = ["AnthropicLLM", "FakeLLM", "ResearchCommittee", "StructuredLLM"]
