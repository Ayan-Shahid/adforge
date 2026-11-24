"""AdForge tools. Each tool is a plain function `fn(user_id, **args) -> dict`.

`user_id` is injected by the graph from the API request; the model never supplies
it, so a tool can only ever read or write the caller's own rows.
"""
import math
import re

from . import db

# Published character limits / formats per ad platform.
PLATFORM_SPECS = {
    "google_search": {
        "label": "Google Responsive Search Ad",
        "fields": {"headline": 30, "description": 90, "path": 15},
        "counts": "3-15 headlines, 2-4 descriptions",
        "benchmarks": {"ctr": 0.0317, "cpc": 2.69, "cvr": 0.0375},
    },
    "meta": {
        "label": "Facebook / Instagram feed ad",
        "fields": {"primary_text": 125, "headline": 40, "description": 30},
        "counts": "up to 5 primary texts and 5 headlines per ad",
        "benchmarks": {"ctr": 0.0090, "cpc": 1.72, "cvr": 0.0911},
    },
    "linkedin": {
        "label": "LinkedIn sponsored content",
        "fields": {"intro_text": 150, "headline": 70, "description": 100},
        "counts": "1 intro text, 1 headline",
        "benchmarks": {"ctr": 0.0044, "cpc": 5.58, "cvr": 0.0610},
    },
    "tiktok": {
        "label": "TikTok in-feed ad",
        "fields": {"ad_text": 100, "display_name": 40},
        "counts": "1 ad text per creative; 9-15s video recommended",
        "benchmarks": {"ctr": 0.0084, "cpc": 1.00, "cvr": 0.0150},
    },
    "x": {
        "label": "X (Twitter) promoted post",
        "fields": {"post_text": 280, "headline": 70},
        "counts": "1 post text, optional website card headline",
        "benchmarks": {"ctr": 0.0086, "cpc": 0.58, "cvr": 0.0080},
    },
}
ALIASES = {
    "google": "google_search", "google ads": "google_search", "search": "google_search", "adwords": "google_search",
    "facebook": "meta", "instagram": "meta", "fb": "meta", "ig": "meta",
    "twitter": "x", "linked in": "linkedin",
}

RISKY_PHRASES = [
    (r"\bguarantee[ds]?\b", "Unverifiable guarantee claim"),
    (r"\b(cure|cures|heal|heals)\b", "Medical claim (restricted on most platforms)"),
    (r"\b100\s?%", "Absolute percentage claim"),
    (r"#\s?1\b|\bnumber one\b|\bbest in the world\b", "Superlative ranking claim needs proof"),
    (r"\b(free money|get rich|risk[- ]free)\b", "Financial claim (restricted)"),
    (r"\b(lose \d+ ?(lbs|kg|pounds))\b", "Weight-loss claim (restricted)"),
    (r"\bclick here\b", "Low-quality CTA phrase"),
]


def norm_platform(p: str) -> str | None:
    p = (p or "").strip().lower().replace("-", "_")
    p = ALIASES.get(p, p)
    return p if p in PLATFORM_SPECS else None


# ------------------------------------------------------------------ tools

def get_platform_specs(user_id, platform):
    key = norm_platform(platform)
    if not key:
        return {"error": f"Unknown platform '{platform}'", "supported": list(PLATFORM_SPECS)}
    return {"platform": key, **PLATFORM_SPECS[key]}


def validate_ad_copy(user_id, platform, items):
    """items: list of {field, text}. Checks length limits and policy-risk phrases."""
    key = norm_platform(platform)
    if not key:
        return {"error": f"Unknown platform '{platform}'", "supported": list(PLATFORM_SPECS)}
    limits = PLATFORM_SPECS[key]["fields"]
    results, passed = [], 0
    for it in items or []:
        field = (it.get("field") or "").lower()
        text = it.get("text") or ""
        limit = limits.get(field)
        issues = []
        if limit is None:
            issues.append(f"Unknown field for {key}; valid fields: {', '.join(limits)}")
        elif len(text) > limit:
            issues.append(f"Too long: {len(text)}/{limit} chars")
        for pat, why in RISKY_PHRASES:
            if re.search(pat, text, re.I):
                issues.append(why)
        letters = [ch for ch in text if ch.isalpha()]
        if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.6 and len(letters) > 6:
            issues.append("Excessive capitalisation")
        if re.search(r"[!?]{2,}", text):
            issues.append("Repeated punctuation")
        ok = not issues
        passed += ok
        results.append({"field": field, "text": text, "chars": len(text), "limit": limit, "ok": ok, "issues": issues})
    return {"platform": key, "passed": passed, "total": len(results), "results": results}


def save_campaign(user_id, name, platform, variants, daily_budget=0, objective=None):
    key = norm_platform(platform) or (platform or "other").lower()
    if not variants:
        return {"error": "At least one ad variant is required"}
    for i, v in enumerate(variants):
        v.setdefault("label", chr(65 + i))
    try:
        budget = max(0.0, float(daily_budget or 0))
    except (TypeError, ValueError):
        budget = 0.0
    c = db.create_campaign(user_id, name[:120], key, objective, budget, variants)
    return {"saved": True, "campaign_id": c["id"], "status": c["status"], "name": c["name"],
            "platform": c["platform"], "variants": len(variants), "daily_budget": c["daily_budget"]}


def list_campaigns(user_id):
    rows = db.list_campaigns(user_id)
    return {"count": len(rows), "campaigns": [
        {"campaign_id": r["id"], "name": r["name"], "platform": r["platform"], "status": r["status"],
         "daily_budget": r["daily_budget"], "variants": [v.get("label") for v in r["variants"]],
         "ab_winner": (r["ab_result"] or {}).get("winner")}
        for r in rows]}


def get_campaign(user_id, campaign_id):
    c = db.get_campaign(user_id, int(campaign_id))
    return c or {"error": f"Campaign {campaign_id} not found for this account"}


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def analyze_ab_test(user_id, variant_a, variant_b, metric="ctr", campaign_id=None):
    """Two-proportion z-test. variant_*: {impressions, clicks, conversions?, spend?}."""
    def rate(v, num, den):
        n, d = float(v.get(num) or 0), float(v.get(den) or 0)
        return (n / d if d else 0.0), n, d

    if metric == "cvr":
        num, den = "conversions", "clicks"
    else:
        metric, num, den = "ctr", "clicks", "impressions"
    pa, xa, na = rate(variant_a, num, den)
    pb, xb, nb = rate(variant_b, num, den)
    if na <= 0 or nb <= 0:
        return {"error": f"Need {den} > 0 for both variants"}
    if xa > na or xb > nb:
        return {"error": f"{num} cannot exceed {den}"}
    pool_p = (xa + xb) / (na + nb)
    se = math.sqrt(pool_p * (1 - pool_p) * (1 / na + 1 / nb)) if 0 < pool_p < 1 else 0
    z = (pb - pa) / se if se else 0.0
    p_value = 2 * (1 - _phi(abs(z)))
    lift = (pb - pa) / pa if pa else None
    significant = p_value < 0.05
    winner = ("B" if pb > pa else "A") if significant else "no clear winner yet"
    # sample size per arm for 80% power at the observed effect (alpha 0.05)
    need = None
    if pa != pb:
        pbar = (pa + pb) / 2
        need = math.ceil(((1.96 * math.sqrt(2 * pbar * (1 - pbar)) + 0.84 * math.sqrt(pa * (1 - pa) + pb * (1 - pb))) ** 2) / (pb - pa) ** 2)

    def cpx(v, key):
        s = float(v.get("spend") or 0)
        d = float(v.get(key) or 0)
        return round(s / d, 2) if s and d else None

    res = {
        "metric": metric,
        "A": {"rate": round(pa, 5), num: xa, den: na, "cpc": cpx(variant_a, "clicks"), "cpa": cpx(variant_a, "conversions")},
        "B": {"rate": round(pb, 5), num: xb, den: nb, "cpc": cpx(variant_b, "clicks"), "cpa": cpx(variant_b, "conversions")},
        "relative_lift_b_vs_a": round(lift, 4) if lift is not None else None,
        "z_score": round(z, 3), "p_value": round(p_value, 4),
        "significant_at_95": significant, "winner": winner,
        "sample_needed_per_variant": need,
    }
    if campaign_id:
        updated = db.update_campaign(user_id, int(campaign_id), ab_result=res)
        res["saved_to_campaign"] = bool(updated)
    return res


def forecast_results(user_id, platform, daily_budget, days=30, cpc=None, ctr=None, cvr=None, avg_order_value=None):
    key = norm_platform(platform)
    if not key:
        return {"error": f"Unknown platform '{platform}'", "supported": list(PLATFORM_SPECS)}
    b = PLATFORM_SPECS[key]["benchmarks"]
    cpc = float(cpc or b["cpc"]); ctr = float(ctr or b["ctr"]); cvr = float(cvr or b["cvr"])
    spend = float(daily_budget) * int(days)
    clicks = spend / cpc if cpc else 0
    impressions = clicks / ctr if ctr else 0
    conv = clicks * cvr
    out = {"platform": key, "days": int(days), "total_spend": round(spend, 2),
           "assumptions": {"cpc": cpc, "ctr": ctr, "cvr": cvr, "source": "industry benchmarks unless overridden"},
           "est_impressions": int(impressions), "est_clicks": int(clicks), "est_conversions": round(conv, 1),
           "est_cpa": round(spend / conv, 2) if conv else None}
    if avg_order_value:
        rev = conv * float(avg_order_value)
        out["est_revenue"] = round(rev, 2)
        out["est_roas"] = round(rev / spend, 2) if spend else None
    return out


def update_brand_profile(user_id, **fields):
    profile = db.merge_profile(user_id, fields)
    return {"saved": True, "profile": profile}


def forget_brand_info(user_id, keys):
    return {"removed": keys, "profile": db.forget_profile_keys(user_id, keys)}


# ---- risky actions: these need explicit confirmation (enforced in graph.py too)

def launch_campaign(user_id, campaign_id, confirm=False):
    c = db.get_campaign(user_id, int(campaign_id))
    if not c:
        return {"error": f"Campaign {campaign_id} not found for this account"}
    if not confirm:
        return {"status": "confirmation_required",
                "message": f"Launching '{c['name']}' will start spending ${c['daily_budget']:.2f}/day on {c['platform']}. Ask the user to confirm."}
    c = db.update_campaign(user_id, int(campaign_id), status="active")
    return {"launched": True, "campaign_id": c["id"], "status": c["status"], "daily_budget": c["daily_budget"]}


def pause_campaign(user_id, campaign_id):
    c = db.update_campaign(user_id, int(campaign_id), status="paused")
    return {"paused": True, "campaign_id": c["id"]} if c else {"error": f"Campaign {campaign_id} not found for this account"}


def delete_campaign(user_id, campaign_id, confirm=False):
    c = db.get_campaign(user_id, int(campaign_id))
    if not c:
        return {"error": f"Campaign {campaign_id} not found for this account"}
    if not confirm:
        return {"status": "confirmation_required",
                "message": f"Deleting '{c['name']}' is permanent. Ask the user to confirm."}
    db.delete_campaign(user_id, int(campaign_id))
    return {"deleted": True, "campaign_id": int(campaign_id)}


RISKY_TOOLS = {"launch_campaign", "delete_campaign"}

REGISTRY = {
    "get_platform_specs": get_platform_specs,
    "validate_ad_copy": validate_ad_copy,
    "save_campaign": save_campaign,
    "list_campaigns": list_campaigns,
    "get_campaign": get_campaign,
    "analyze_ab_test": analyze_ab_test,
    "forecast_results": forecast_results,
    "update_brand_profile": update_brand_profile,
    "forget_brand_info": forget_brand_info,
    "launch_campaign": launch_campaign,
    "pause_campaign": pause_campaign,
    "delete_campaign": delete_campaign,
}

_variant = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "A, B, C..."},
        "headline": {"type": "string"},
        "body": {"type": "string", "description": "primary text / description"},
        "cta": {"type": "string"},
        "angle": {"type": "string", "description": "the creative angle being tested"},
    },
    "required": ["headline", "body"],
}
_ab_arm = {
    "type": "object",
    "properties": {"impressions": {"type": "number"}, "clicks": {"type": "number"},
                   "conversions": {"type": "number"}, "spend": {"type": "number"}},
    "required": ["impressions", "clicks"],
}
PLATFORM_ENUM = list(PLATFORM_SPECS)

SCHEMAS = [
    {"name": "get_platform_specs",
     "description": "Get character limits, formats and benchmark CTR/CPC/CVR for an ad platform. Call before writing copy for a platform.",
     "input_schema": {"type": "object", "properties": {"platform": {"type": "string", "enum": PLATFORM_ENUM}}, "required": ["platform"]}},
    {"name": "validate_ad_copy",
     "description": "Check ad copy against the platform's character limits and ad-policy risk phrases. Use on every piece of copy you write before presenting or saving it.",
     "input_schema": {"type": "object", "properties": {
         "platform": {"type": "string", "enum": PLATFORM_ENUM},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "field": {"type": "string", "description": "field name from get_platform_specs, e.g. headline, description, primary_text"},
             "text": {"type": "string"}}, "required": ["field", "text"]}}},
         "required": ["platform", "items"]}},
    {"name": "save_campaign",
     "description": "Save a draft campaign with its ad variants (for A/B testing) to the user's account. Does not spend money.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "platform": {"type": "string", "enum": PLATFORM_ENUM},
         "objective": {"type": "string", "description": "e.g. sales, leads, traffic, awareness"},
         "daily_budget": {"type": "number", "description": "USD per day"},
         "variants": {"type": "array", "items": _variant}},
         "required": ["name", "platform", "variants"]}},
    {"name": "list_campaigns", "description": "List the user's saved campaigns with status and A/B winner.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_campaign", "description": "Get full details (variants, budget, A/B results) of one of the user's campaigns.",
     "input_schema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}}, "required": ["campaign_id"]}},
    {"name": "analyze_ab_test",
     "description": "Run a two-proportion z-test on A/B results. metric 'ctr' uses clicks/impressions, 'cvr' uses conversions/clicks. Returns rates, lift, p-value, winner and sample size needed. Optionally saves the result to a campaign.",
     "input_schema": {"type": "object", "properties": {
         "variant_a": _ab_arm, "variant_b": _ab_arm,
         "metric": {"type": "string", "enum": ["ctr", "cvr"]},
         "campaign_id": {"type": "integer"}}, "required": ["variant_a", "variant_b"]}},
    {"name": "forecast_results",
     "description": "Forecast impressions, clicks, conversions, CPA (and revenue/ROAS if avg_order_value is given) for a budget, using platform benchmarks unless overridden.",
     "input_schema": {"type": "object", "properties": {
         "platform": {"type": "string", "enum": PLATFORM_ENUM}, "daily_budget": {"type": "number"},
         "days": {"type": "integer"}, "cpc": {"type": "number"}, "ctr": {"type": "number"},
         "cvr": {"type": "number"}, "avg_order_value": {"type": "number"}},
         "required": ["platform", "daily_budget"]}},
    {"name": "update_brand_profile",
     "description": "Save durable facts about the user's business to long-term memory (remembered across all future conversations). Call whenever the user shares brand name, product, audience, tone, platforms, budget, website, competitors or preferences.",
     "input_schema": {"type": "object", "properties": {
         "brand_name": {"type": "string"}, "product": {"type": "string"}, "audience": {"type": "string"},
         "tone": {"type": "string"}, "platforms": {"type": "array", "items": {"type": "string"}},
         "monthly_budget": {"type": "number"}, "website": {"type": "string"}, "location": {"type": "string"},
         "competitors": {"type": "string"}, "preferences": {"type": "string", "description": "other likes/dislikes, e.g. 'no emojis'"},
         "user_name": {"type": "string"}}}},
    {"name": "forget_brand_info", "description": "Remove keys from the user's long-term memory when they ask you to forget something.",
     "input_schema": {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}}}, "required": ["keys"]}},
    {"name": "launch_campaign",
     "description": "RISKY (spends money): set a campaign live. Call first with confirm=false to get the confirmation message; only call with confirm=true after the user explicitly says yes in their latest message.",
     "input_schema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}, "confirm": {"type": "boolean"}}, "required": ["campaign_id"]}},
    {"name": "pause_campaign", "description": "Pause a running campaign (safe, stops spend).",
     "input_schema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}}, "required": ["campaign_id"]}},
    {"name": "delete_campaign",
     "description": "RISKY (permanent): delete a campaign. Call with confirm=false first; confirm=true only after the user explicitly says yes in their latest message.",
     "input_schema": {"type": "object", "properties": {"campaign_id": {"type": "integer"}, "confirm": {"type": "boolean"}}, "required": ["campaign_id"]}},
]
