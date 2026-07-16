#!/usr/bin/env python3
"""
Visual Strategy Planner — Pre-Generation Image Selection

Reads briefs + fetched images, assigns best product images to each page
BEFORE the generator runs. Output: visual_strategy.json consumed by inject_page_images.py.

Usage:
  python3 visual_strategy.py <site_slug>       # plan + write strategy file
  python3 visual_strategy.py <site_slug> --dry  # plan without writing
"""

import os, sys, json, hashlib
from pathlib import Path
from collections import defaultdict

SCRIPTS_DIR = Path(__file__).resolve().parent
GENERATED_DIR = SCRIPTS_DIR.parent / "generated"
IMAGE_MAP_PATH = SCRIPTS_DIR.parent / "data" / "image_map.json"

# Image role scoring: which product images make the best hero vs card
HERO_PRIORITY = {"panoramic", "landscape", "scenic", "aerial", "sunset", "overview"}
CARD_PRIORITY = {"activity", "close-up", "detail", "group", "food"}
INLINE_PRIORITY = {"detail", "interior", "close-up", "before-after", "action"}


def load_image_map():
    if IMAGE_MAP_PATH.exists():
        with open(IMAGE_MAP_PATH) as f:
            return json.load(f)
    return {}


def load_briefs(site_slug):
    """Load filtered briefs for the site."""
    briefs_path = Path(f"/tmp/filtered-{site_slug}.json")
    if briefs_path.exists():
        with open(briefs_path) as f:
            data = json.load(f)
            return data.get("briefs", [])
    return []


def score_image_for_role(image_info, role):
    """Score an image's suitability for a role based on tags, dimensions, aspect ratio."""
    score = 0
    tags = set(image_info.get("topic_tags", []))
    alt_text = image_info.get("alt_text", "").lower()
    url = image_info.get("url", "")

    # Dimension-based scoring
    width = image_info.get("width", 0)
    height = image_info.get("height", 0)
    aspect = width / height if (width and height) else 1.5

    if role == "hero":
        # Heroes prefer wide aspect, large dimensions, scenic tags
        if aspect >= 1.5:
            score += 3
        if width >= 1200:
            score += 2
        if tags & HERO_PRIORITY:
            score += 4
        # No "map" or "logo" images as heroes
        if "map" in alt_text or "logo" in alt_text or "screenshot" in alt_text:
            score -= 5
    elif role == "card":
        # Cards prefer square-ish, product-focused
        if 0.8 <= aspect <= 1.5:
            score += 3
        if tags & CARD_PRIORITY:
            score += 3
        # Exclude pure scenic for cards — need product focus
        if tags & {"panoramic", "aerial"} and not (tags & CARD_PRIORITY):
            score -= 2
    elif role == "inline":
        # Inline prefers detail/close-up, any aspect
        if tags & INLINE_PRIORITY:
            score += 3
        if width >= 800:
            score += 1

    # URL quality: prefer larger images
    if "720x720" in url or "360x240" in url:
        score -= 1
    if "1440x" in url or "1920x" in url or "2048x" in url:
        score += 2

    return score


def pick_images_for_product(product_code, image_map):
    """Pick hero, card, and inline images for a product code."""
    variants = image_map.get(product_code, {})
    if not variants:
        return None

    scored = []
    for idx_key, info in variants.items():
        hero_score = score_image_for_role(info, "hero")
        card_score = score_image_for_role(info, "card")
        inline_score = score_image_for_role(info, "inline")
        scored.append({
            "idx": idx_key,
            "url": info.get("url", ""),
            "width": info.get("width", 0),
            "height": info.get("height", 0),
            "alt": info.get("alt_text", ""),
            "hero_score": hero_score,
            "card_score": card_score,
            "inline_score": inline_score,
        })

    if not scored:
        return None

    best_hero = max(scored, key=lambda s: s["hero_score"])
    best_card = max(scored, key=lambda s: s["card_score"])
    best_inline = max(scored, key=lambda s: s["inline_score"])

    return {
        "product_code": product_code,
        "hero": {
            "url": best_hero["url"],
            "alt": best_hero["alt"],
            "width": best_hero["width"],
            "height": best_hero["height"],
            "score": best_hero["hero_score"],
        },
        "card": {
            "url": best_card["url"],
            "alt": best_card["alt"],
            "width": best_card["width"],
            "height": best_card["height"],
            "score": best_card["card_score"],
        },
        "inline": {
            "url": best_inline["url"],
            "alt": best_inline["alt"],
            "width": best_inline["width"],
            "height": best_inline["height"],
            "score": best_inline["inline_score"],
        },
    }


def plan_site(site_slug, image_map, dry_run=False):
    """Plan visual strategy for all briefs of a site."""
    briefs = load_briefs(site_slug)
    if not briefs:
        print(f"No briefs found for {site_slug}")
        return None

    strategy = {}
    products_planned = set()
    briefs_mapped = 0

    for brief in briefs:
        slug = brief.get("slug", "")
        products = brief.get("products_to_feature", [])
        primary = brief.get("primary_product_code", "")
        comparison = brief.get("comparison_products", [])

        all_codes = list(dict.fromkeys(
            [primary] + products + comparison
        ))  # dedup, preserve order
        all_codes = [c for c in all_codes if c]  # filter empties

        if not all_codes:
            continue

        page_images = {}
        for code in all_codes:
            pick = pick_images_for_product(code, image_map)
            if pick:
                page_images[code] = pick
                products_planned.add(code)

        if page_images:
            strategy[slug] = {
                "hero_product": primary,
                "products": page_images,
            }
            briefs_mapped += 1

    if not strategy:
        print(f"No images available for any briefs — image_map may be empty")
        return None

    # Select site-wide hero image (best hero-scoring image across all products)
    best_site_hero = None
    best_site_hero_score = -1
    for slug, plan in strategy.items():
        for code, picks in plan["products"].items():
            if picks["hero"]["score"] > best_site_hero_score:
                best_site_hero_score = picks["hero"]["score"]
                best_site_hero = {
                    "url": picks["hero"]["url"],
                    "alt": picks["hero"]["alt"],
                    "width": picks["hero"]["width"],
                    "height": picks["hero"]["height"],
                }

    strategy["_site"] = {
        "hero": best_site_hero,
        "products_planned": len(products_planned),
        "briefs_mapped": briefs_mapped,
    }

    if not dry_run:
        output_path = GENERATED_DIR / site_slug / "visual_strategy.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(strategy, f, indent=2)
        print(f"Written: {output_path}")
        print(f"  {briefs_mapped} briefs mapped, {len(products_planned)} product codes imaged")

    return strategy


def main():
    if len(sys.argv) < 2:
        print("Usage: visual_strategy.py <site_slug> [--dry]")
        sys.exit(1)

    site_slug = sys.argv[1]
    dry_run = "--dry" in sys.argv

    image_map = load_image_map()
    if not image_map:
        print("ERROR: image_map.json not found or empty — run fetch_product_images.py first")
        sys.exit(1)

    strategy = plan_site(site_slug, image_map, dry_run=dry_run)
    if strategy is None:
        sys.exit(1)

    # Print summary
    briefs = len([k for k in strategy if k != "_site"])
    if briefs > 0:
        print(f"\nVisual strategy: {briefs} pages planned with product-matched images")
    else:
        print("\nNo pages planned — image_map may lack images for these product codes")


if __name__ == "__main__":
    main()
