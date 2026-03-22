#!/usr/bin/env python3

from __future__ import annotations
"""
Payment Integrator - Multi-processor payment automation.

Routes products to optimal payment processor based on type, fees, and risk.
Manages products/prices across Stripe, PayPal, Whop, RevenueCat, LemonSqueezy, AdMob.

Monetization routing (auto-apply):
  Mobile app (iOS/Android) -> RevenueCat IAP + AdMob ads
  Web app / SaaS           -> Stripe Checkout / Payment Links
  Landing page / site      -> Stripe Payment Link + AdMob display ads
  Digital product          -> Stripe or Whop

Usage:
    python3 AUTOMATIONS/payment_integrator.py --status
    python3 AUTOMATIONS/payment_integrator.py --sync-products
    python3 AUTOMATIONS/payment_integrator.py --create-links
    python3 AUTOMATIONS/payment_integrator.py --route PRODUCT_TYPE
    python3 AUTOMATIONS/payment_integrator.py --wire-app APP_DIR
    python3 AUTOMATIONS/payment_integrator.py --wire-mobile APP_DIR
    python3 AUTOMATIONS/payment_integrator.py --dry-run
"""

import json
import os
import sys
import csv
import subprocess
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTS_DB = PROJECT_ROOT / "AUTOMATIONS" / "agent" / "payment_products.json"
STRIPE_REF = PROJECT_ROOT / "OPS" / "STRIPE_PRODUCTS.md"


def safe_path(target: Path) -> Path:
    """Verify path is within project root. Raises ValueError if not."""
    resolved = Path(target).resolve()
    if not str(resolved).startswith(str(PROJECT_ROOT)):
        raise ValueError(f"BLOCKED: {resolved} is outside project root {PROJECT_ROOT}")
    return resolved


def load_env() -> None:
    """Load .env and SECRETS/CREDENTIALS.env into os.environ if not already set."""
    for env_file in [PROJECT_ROOT / ".env", PROJECT_ROOT / "SECRETS" / "CREDENTIALS.env"]:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


# Load env on import so all functions see the keys
load_env()

# ─── Processor Registry ────────────────────────────────────────────

PROCESSORS = {
    "stripe": {
        "name": "Stripe",
        "fees": "2.9% + $0.30",
        "fee_pct": 0.029,
        "fee_flat": 0.30,
        "best_for": ["web_apps", "subscriptions", "saas", "digital_products", "api_payments"],
        "risks": ["holds on new accounts", "6-month reserve possible", "slow first payout"],
        "env_key": "STRIPE_SECRET_KEY",
        "api_available": True,
        "setup_url": "https://dashboard.stripe.com/account/onboarding",
        "priority": 1,
    },
    "whop": {
        "name": "Whop",
        "fees": "Platform fee varies (typically free for sellers, buyers pay)",
        "fee_pct": 0.0,
        "fee_flat": 0.0,
        "best_for": ["digital_products", "courses", "communities", "templates", "software_access"],
        "risks": ["smaller audience than gumroad", "newer platform"],
        "env_key": "WHOP_API_KEY",
        "api_available": True,
        "setup_url": "https://whop.com/sell",
        "priority": 2,
    },
    "paypal": {
        "name": "PayPal",
        "fees": "3.49% + $0.49 (digital goods)",
        "fee_pct": 0.0349,
        "fee_flat": 0.49,
        "best_for": ["international", "digital_products", "one_time_purchases", "trust_signal"],
        "risks": ["buyer-favored disputes", "account limitations"],
        "env_key": "PAYPAL_CLIENT_SECRET",
        "api_available": True,
        "setup_url": "https://developer.paypal.com/dashboard/applications",
        "priority": 3,
    },
    "revenuecat": {
        "name": "RevenueCat",
        "fees": "Free <$2.5K MTR, then 1%",
        "fee_pct": 0.01,
        "fee_flat": 0.0,
        "best_for": ["mobile_apps", "ios_subscriptions", "android_subscriptions", "in_app_purchases"],
        "risks": ["only for mobile", "apple/google take 15-30% on top"],
        "env_key": "REVENUECAT_API_KEY",
        "api_available": True,
        "setup_url": "https://app.revenuecat.com",
        "priority": 4,
    },
    "lemonsqueezy": {
        "name": "Lemon Squeezy",
        "fees": "5% + $0.50",
        "fee_pct": 0.05,
        "fee_flat": 0.50,
        "best_for": ["saas", "digital_products", "merchant_of_record", "tax_handling"],
        "risks": ["higher fees", "smaller ecosystem"],
        "env_key": "LEMONSQUEEZY_API_KEY",
        "api_available": True,
        "setup_url": "https://app.lemonsqueezy.com",
        "priority": 5,
    },
    "gumroad": {
        "name": "Gumroad",
        "fees": "10%",
        "fee_pct": 0.10,
        "fee_flat": 0.0,
        "best_for": ["digital_products", "ebooks", "templates", "simple_checkout"],
        "risks": ["high fees", "limited customization"],
        "env_key": "GUMROAD_ACCESS_TOKEN",
        "api_available": True,
        "setup_url": "https://gumroad.com",
        "priority": 6,
    },
    "admob": {
        "name": "AdMob",
        "fees": "Ad network (revenue share, ~$1-10 RPM)",
        "fee_pct": 0.0,
        "fee_flat": 0.0,
        "best_for": ["mobile_app_ads", "web_display_ads", "landing_page_ads", "free_tier_monetization"],
        "risks": ["low RPM for small audiences", "requires approval", "policy violations risk account ban"],
        "env_key": "ADMOB_APP_ID",
        "api_available": False,
        "setup_url": "https://admob.google.com",
        "priority": 7,
        "app_id": "ca-app-pub-5277873663568466~6431629011",
        "banner_ad_unit": "ca-app-pub-5277873663568466/8124162242",
    },
}

# ─── Product Types → Processor Routing ──────────────────────────────

ROUTING_RULES = {
    "digital_product": ["stripe", "whop", "gumroad"],
    "ebook_guide": ["whop", "gumroad", "stripe"],
    "template_pack": ["whop", "gumroad", "stripe"],
    "web_app_premium": ["stripe", "paypal"],
    "saas_subscription": ["stripe", "lemonsqueezy"],
    "mobile_app": ["revenuecat", "admob", "stripe"],
    "ios_subscription": ["revenuecat"],
    "android_subscription": ["revenuecat"],
    "mobile_app_ads": ["admob"],
    "landing_page": ["stripe", "admob"],
    "course": ["whop", "gumroad", "stripe"],
    "community_access": ["whop", "stripe"],
    "one_time_tool": ["stripe", "paypal", "whop"],
    "api_access": ["stripe", "lemonsqueezy"],
}


def get_active_processors():
    """Check which processors have API keys configured."""
    active = {}
    for key, proc in PROCESSORS.items():
        env_val = os.environ.get(proc["env_key"], "")
        active[key] = {
            **proc,
            "configured": bool(env_val),
            "key_prefix": env_val[:8] + "..." if env_val else "NOT SET",
        }
    return active


def route_product(product_type):
    """Determine optimal processor(s) for a product type."""
    if product_type not in ROUTING_RULES:
        return {"error": f"Unknown product type: {product_type}", "valid_types": list(ROUTING_RULES.keys())}

    candidates = ROUTING_RULES[product_type]
    active = get_active_processors()

    result = []
    for proc_key in candidates:
        proc = active[proc_key]
        result.append({
            "processor": proc_key,
            "name": proc["name"],
            "fees": proc["fees"],
            "configured": proc["configured"],
            "recommended": proc["configured"] and proc_key == candidates[0],
        })

    return {"product_type": product_type, "processors": result}


def load_products_db():
    """Load the products database."""
    if PRODUCTS_DB.exists():
        return json.loads(PRODUCTS_DB.read_text())
    return {"products": [], "last_sync": None}


def save_products_db(db):
    """Save the products database."""
    PRODUCTS_DB.parent.mkdir(parents=True, exist_ok=True)
    db["last_sync"] = datetime.now().isoformat()
    PRODUCTS_DB.write_text(json.dumps(db, indent=2))


def scan_ready_products():
    """Scan project for products that need payment integration."""
    products = []

    # Digital products
    dp_dir = PROJECT_ROOT / "DIGITAL_PRODUCTS" / "ready_to_sell"
    if dp_dir.exists():
        for f in dp_dir.glob("*.md"):
            content = f.read_text()
            price = None
            for line in content.split("\n"):
                if "$" in line and any(w in line.lower() for w in ["price", "cost", "tier"]):
                    import re
                    m = re.search(r'\$(\d+)', line)
                    if m:
                        price = int(m.group(1))
                        break
            products.append({
                "name": f.stem.replace("LISTING_", "").replace("_", " ").title(),
                "type": "digital_product",
                "price_usd": price or 29,
                "source_file": str(f.relative_to(PROJECT_ROOT)),
                "needs_payment": True,
            })

    # App premium tiers
    builds_dir = PROJECT_ROOT / "MONEY_METHODS" / "APP_FACTORY" / "builds"
    if builds_dir.exists():
        for app_dir in sorted(builds_dir.iterdir()):
            if app_dir.is_dir():
                index = app_dir / "index.html"
                if index.exists():
                    content = index.read_text()
                    has_payment = "stripe" in content.lower() or "payment" in content.lower()
                    products.append({
                        "name": app_dir.name.replace("-", " ").title(),
                        "type": "web_app_premium",
                        "price_usd": 19 if "streak" in app_dir.name else 29,
                        "source_file": str(app_dir.relative_to(PROJECT_ROOT)),
                        "deployed_url": f"https://{app_dir.name}.surge.sh",
                        "needs_payment": not has_payment,
                    })

    return products


def status():
    """Show payment integration status."""
    active = get_active_processors()
    products = scan_ready_products()
    db = load_products_db()

    print("=" * 60)
    print("PAYMENT INTEGRATOR STATUS")
    print("=" * 60)

    print("\n## Processors")
    for key, proc in active.items():
        status_icon = "+" if proc["configured"] else "-"
        print(f"  [{status_icon}] {proc['name']:20s} | {proc['fees']:25s} | {proc['key_prefix']}")

    configured = sum(1 for p in active.values() if p["configured"])
    print(f"\n  {configured}/{len(active)} configured")

    print(f"\n## Products Found: {len(products)}")
    needs_payment = [p for p in products if p.get("needs_payment")]
    has_payment = [p for p in products if not p.get("needs_payment")]
    print(f"  Needs payment: {len(needs_payment)}")
    print(f"  Has payment:   {len(has_payment)}")

    if needs_payment[:10]:
        print("\n## Top Products Needing Payment Links")
        for p in needs_payment[:10]:
            proc = route_product(p["type"])
            best = proc["processors"][0]["name"] if "processors" in proc else "?"
            print(f"  - {p['name']}: ${p['price_usd']} → {best}")

    print(f"\n## DB: {len(db.get('products', []))} synced | Last: {db.get('last_sync', 'never')}")

    # Payment processor status summary
    stripe_ok = active["stripe"]["configured"]
    rc_ok = active["revenuecat"]["configured"]
    admob_ok = bool(os.environ.get("ADMOB_APP_ID", ""))

    print("\n## Core Processors (monetization triangle)")
    print(f"  Stripe      (web/SaaS):   {'LIVE' if stripe_ok else 'BLOCKED — set STRIPE_SECRET_KEY'}")
    print(f"  RevenueCat  (mobile IAP):  {'LIVE' if rc_ok else 'BLOCKED — set REVENUECAT_API_KEY'}")
    print(f"  AdMob       (ads):         {'LIVE — ' + os.environ.get('ADMOB_APP_ID','') if admob_ok else 'BLOCKED — set ADMOB_APP_ID'}")

    if stripe_ok and rc_ok and admob_ok:
        print("\n>> All three monetization processors LIVE. Every new build can auto-wire.")
    else:
        print("\n!! At least one processor not configured. Check .env and SECRETS/CREDENTIALS.env")


def sync_products():
    """Sync all products to the database and flag what needs payment links."""
    products = scan_ready_products()
    db = load_products_db()
    db["products"] = products
    save_products_db(db)
    print(f"Synced {len(products)} products to {PRODUCTS_DB}")
    return products


def wire_app(app_dir_name):
    """Generate Stripe checkout snippet for a web app or landing page."""
    pub_key = os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_live_YOUR_KEY")
    snippet = f"""
<!-- Stripe Payment Integration -->
<!-- STRIPE_PUBLISHABLE_KEY: {pub_key[:16]}... -->
<script src="https://js.stripe.com/v3/"></script>
<script>
  const stripe = Stripe('{pub_key}');
  document.querySelectorAll('[data-upgrade]').forEach(btn => {{
    btn.addEventListener('click', async () => {{
      const res = await fetch('/api/checkout', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ app: '{app_dir_name}' }})
      }});
      const {{ url }} = await res.json();
      window.location = url;
    }});
  }});
</script>
"""
    print(f"Stripe checkout snippet for {app_dir_name}:")
    print(snippet)
    return snippet


def wire_mobile(app_dir_name, dry_run=False):
    """Print RevenueCat + AdMob wiring instructions for a mobile app build."""
    rc_key = os.environ.get("REVENUECAT_API_KEY", "NOT SET")
    admob_app_id = os.environ.get("ADMOB_APP_ID", PROCESSORS["admob"]["app_id"])
    banner_unit = PROCESSORS["admob"]["banner_ad_unit"]

    rc_configured = rc_key != "NOT SET"
    admob_configured = bool(admob_app_id)

    print(f"Mobile monetization wiring for: {app_dir_name}")
    print(f"  RevenueCat API key: {'CONFIGURED (' + rc_key[:8] + '...)' if rc_configured else 'NOT SET — add REVENUECAT_API_KEY to .env'}")
    print(f"  AdMob App ID:       {admob_app_id}")
    print(f"  AdMob Banner Unit:  {banner_unit}")
    print()

    env_block = f"""# Add to app .env or env.template:
EXPO_PUBLIC_REVENUECAT_APPLE_API_KEY={rc_key if rc_configured else 'YOUR_REVENUECAT_IOS_KEY'}
EXPO_PUBLIC_REVENUECAT_GOOGLE_API_KEY=YOUR_REVENUECAT_ANDROID_KEY
EXPO_PUBLIC_ADMOB_APP_ID={admob_app_id}
EXPO_PUBLIC_ADMOB_BANNER_ID={banner_unit}
"""
    print(env_block)

    instructions = """Wiring checklist:
  1. Copy src/lib/purchases.ts from base-template (RevenueCat IAP)
  2. Copy src/components/AdBanner.tsx from base-template (AdMob banner)
  3. In app.json, set: expo.plugins -> ['react-native-google-mobile-ads', {androidAppId, iosAppId}]
  4. Call initializePurchases() on app startup
  5. Render <AdBanner onUpgradePress={...} /> on free-tier screens
  6. After purchase: call setAdFreeStatus(true) to suppress ads
  7. Test: checkSubscriptionStatus() should return false for new installs
"""
    print(instructions)

    if not dry_run:
        # Write env snippet to app dir if it exists
        app_path = PROJECT_ROOT / app_dir_name
        if app_path.exists():
            env_template = app_path / "env.template"
            if not env_template.exists():
                try:
                    safe_path(env_template)
                    env_template.write_text(env_block)
                    print(f"  Wrote env.template to {env_template}")
                except ValueError as e:
                    print(f"  Skipped env.template write: {e}")
        else:
            print(f"  (app dir '{app_dir_name}' not found — env.template not written)")

    return {"revenuecat_configured": rc_configured, "admob_configured": admob_configured}


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    if "--status" in args:
        status()
    elif "--sync-products" in args:
        sync_products()
    elif "--create-links" in args:
        print("Creating payment links requires completed Stripe onboarding.")
        print("Run: python3 AUTOMATIONS/payment_integrator.py --status")
    elif "--route" in args:
        idx = args.index("--route")
        if idx + 1 < len(args):
            result = route_product(args[idx + 1])
            print(json.dumps(result, indent=2))
        else:
            print("Usage: --route PRODUCT_TYPE")
            print(f"Types: {', '.join(ROUTING_RULES.keys())}")
    elif "--wire-app" in args:
        idx = args.index("--wire-app")
        if idx + 1 < len(args):
            wire_app(args[idx + 1])
        else:
            print("Usage: --wire-app APP_DIR_NAME")
    elif "--wire-mobile" in args:
        idx = args.index("--wire-mobile")
        if idx + 1 < len(args):
            wire_mobile(args[idx + 1], dry_run=dry_run)
        else:
            print("Usage: --wire-mobile APP_DIR_NAME [--dry-run]")
    elif "--dry-run" in args:
        print("Dry run — loading env and checking processor status:")
        status()
    else:
        print("Usage: python3 AUTOMATIONS/payment_integrator.py [--status|--sync-products|--create-links|--route TYPE|--wire-app APP|--wire-mobile APP|--dry-run]")
