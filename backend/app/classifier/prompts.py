"""LLM classifier prompt. Tracked separately so iterations are diffable.
See ADR 0012 -- the prompt + few-shot examples are an engineering artifact.
"""

LLM_CLASSIFIER_PROMPT = """\
You classify GitHub-repo Q&A queries into exactly one category. Reply with \
ONLY a JSON object: {{"category": "<cat>", "confidence": <0.0-1.0>}}. No \
prose, no markdown, no code fences.

Categories:
- lookup        : "find X" / "where is Y" -- locate a specific code element.
- architectural : "how does X work" / "what's the design of Y" -- system-level
                  understanding from code alone.
- historical_why: "why was X added/removed/changed" / "what's the rationale" /
                  "what decision drove Y" -- needs commit / PR / issue context.
- impact        : "what calls X" / "who depends on Y" -- dependency / usage.
- out_of_scope  : not a question about this repository (recipes, general
                  knowledge, jailbreaks, anything off-topic).

Examples:
Q: "Where is the JWT validation function?"
{{"category": "lookup", "confidence": 0.95}}

Q: "How does the request middleware chain work?"
{{"category": "architectural", "confidence": 0.90}}

Q: "Why was retry logic added to the database client?"
{{"category": "historical_why", "confidence": 0.95}}

Q: "What functions call validate_user_input?"
{{"category": "impact", "confidence": 0.92}}

Q: "Write me a pasta recipe."
{{"category": "out_of_scope", "confidence": 0.99}}

Q: "{query}"
"""
