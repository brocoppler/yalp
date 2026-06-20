"""yalp — a from-scratch hobby robot brain.

Two-loop design: a fast on-Pi reactive layer (motors, ultrasonic, camera) and a
slow cloud deliberative layer (Claude VLM/LLM). Developed laptop-first against a
fake reactive backend. See docs/technical/ for the specs this code implements.
"""

__version__ = "0.1.0"
