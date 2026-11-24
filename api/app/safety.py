"""Rule-based pre-filter (no LLM call, zero cost). Catches prompt injection,
cross-user data requests and clearly harmful ad requests before they reach the
model. Subtler off-topic requests are handled by the agent's system prompt."""
import re

INJECTION = [
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system|the)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?|directives?)",
    r"\b(reveal|show|print|repeat|output|display|leak|tell me|give me|what (is|are))\b.{0,30}\b(system|hidden|initial|original|internal|developer)\s*(prompt|instructions?|message|rules)",
    r"\byour (system )?prompt\b",
    r"\b(you are now|from now on you are|pretend (to be|you are)|act as|roleplay as)\b.{0,40}\b(dan|unrestricted|jailbr\w*|unfiltered|evil|no (rules|limits|restrictions))",
    r"\b(jailbreak|developer mode|dan mode|god mode)\b",
    r"<\s*/?\s*(system|instructions?)\s*>|\[\s*system\s*\]|###\s*system",
    r"\bnew instructions?\s*:",
    r"\b(api[_ ]?key|anthropic[_ ]key|database[_ ]url|env(ironment)? variables?|secrets?)\b.{0,30}\b(show|reveal|print|give|what)|\b(show|reveal|print|give|what)\b.{0,30}\b(api[_ ]?key|database[_ ]url|env(ironment)? variables?)",
]
CROSS_USER = [
    r"\b(other|another|all|every|different)\s+(users?|customers?|accounts?|clients?)('s|')?\b.{0,40}\b(data|campaigns?|profiles?|memory|memories|info|information|details|history|conversations?|chats?)",
    r"\b(data|campaigns?|profiles?|memory|history|conversations?)\b.{0,30}\b(of|for|from|belonging to)\s+(user|user_id|account)\s*[:=#]?\s*\S+",
    r"\b(switch|change|set)\b.{0,20}\buser_?id\b",
    r"\bselect\b.+\bfrom\b|\bdrop\s+table\b|\bunion\s+select\b",
]
HARMFUL = [
    r"\b(fake|fabricated?|made[- ]up|false)\s+(\w+\s+){0,2}(reviews?|testimonials?|endorsements?|ratings?)",
    r"\b(write|generate|create)\b.{0,30}\b(reviews?|testimonials?)\b.{0,40}\b(pretend|as if|posing as|from customers who)",
    r"\b(scam|phishing|ponzi|pyramid scheme)\b",
    r"\b(impersonat\w+)\b",
]

_inj = [re.compile(p, re.I | re.S) for p in INJECTION]
_xu = [re.compile(p, re.I | re.S) for p in CROSS_USER]
_harm = [re.compile(p, re.I | re.S) for p in HARMFUL]

REFUSALS = {
    "injection": "I can't follow instructions that try to change my rules or reveal my internal setup. "
                 "I'm AdForge, your AI ad agency: I can write ad copy, plan A/B tests, forecast budgets and analyse results. "
                 "What would you like to advertise?",
    "cross_user": "I can only access your own account's campaigns and brand memory. Other users' data is private and kept "
                  "separate. I'm happy to help with your own campaigns, though.",
    "harmful": "I can't help create deceptive or harmful advertising (such as fake reviews, scams or impersonation). "
               "I can help you write honest, compliant ads that still convert well.",
}


def check(message: str) -> str | None:
    """Return a refusal category, or None if the message may proceed."""
    m = message or ""
    if any(p.search(m) for p in _inj):
        return "injection"
    if any(p.search(m) for p in _xu):
        return "cross_user"
    if any(p.search(m) for p in _harm):
        return "harmful"
    return None


AFFIRM = re.compile(r"\b(yes|yep|yeah|yup|confirm(ed)?|go ahead|do it|proceed|approved?|sure|ok(ay)?|launch it|delete it|absolutely|please do)\b", re.I)
NEGATE = re.compile(r"\b(no|nope|don'?t|do not|cancel|stop|wait|hold on|not yet|never)\b", re.I)


def is_affirmative(message: str) -> bool:
    m = message or ""
    return bool(AFFIRM.search(m)) and not NEGATE.search(m)
