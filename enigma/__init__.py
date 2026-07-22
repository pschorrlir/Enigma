"""Enigma: a background self-learning task engine.

Local Ollama models do the iterative work; a cloud frontier model is pulled
in only when progress stalls (cascade escalation). Every finished task
distills a reusable insight recalled on future tasks.
"""

__version__ = "0.1.0"
