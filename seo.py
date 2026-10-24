"""Public, server-rendered product pages describing implemented capabilities."""
import os
from flask import render_template, url_for

from instagram_oauth import validated_public_origin
from database.billing import plans

PUBLIC_PAGES = ("/", "/features/instagram-scheduling", "/features/instagram-automation", "/pricing", "/privacy", "/terms", "/deletion")
PAGES = {
    "instagram-scheduling": {
        "title": "Instagram image post scheduler | Postenza",
        "heading": "Plan your Instagram image posts in one workspace",
        "description": "Prepare image posts, add captions, choose a publishing time, and track publishing status with Postenza's Instagram scheduler.",
        "sections": [
            ("From draft to scheduled image post", "Create a draft with a caption and a public image link, or upload a JPEG when media storage is configured. Review it, then choose a future time in your workspace calendar."),
            ("Know what happened after scheduling", "Postenza records scheduled, publishing, published, and failed states. If a provider request has an uncertain result, check Instagram before retrying to avoid a duplicate post."),
            ("Connect an eligible professional account", "Connect your Instagram professional account through Instagram Login. Publishing depends on the permissions granted to Postenza and on Meta's platform availability. Scheduled publishing currently supports image posts."),
        ],
    },
    "instagram-automation": {
        "title": "Instagram comment and message automation | Postenza",
        "heading": "Turn Instagram comments into organized conversations",
        "description": "Configure keyword replies, private message workflows, follow-ups, and lead activity for connected Instagram professional accounts in Postenza.",
        "sections": [
            ("Set your own reply rules", "Choose trigger keywords, write a public reply, and configure a private delivery message. Enable only the workflows that fit the account you manage."),
            ("Follow the conversation", "View message activity, prompt requests, and follow-ups in the same workspace. AI-assisted replies depend on the configured AI service and your selected automation settings."),
            ("Respect Instagram messaging rules", "Private replies and follow-ups depend on Meta permissions, supported interactions, and messaging windows. Postenza does not guarantee delivery outside those rules."),
        ],
    },
}


def origin():
    return validated_public_origin(os.getenv("PUBLIC_BASE_URL", "")) or "https://postenza.app"


def register_seo(app):
    @app.get("/features/<slug>")
    def product_feature(slug):
        from flask import abort
        if slug not in PAGES:
            abort(404)
        return render_template("public_product.html", page=PAGES[slug], canonical_url=origin() + request_path(slug), plans=None)

    @app.get("/pricing")
    def pricing():
        page = {"title": "Plans and usage limits | Postenza", "heading": "A plan for your Instagram workflow",
                "description": "Compare Postenza's Free, Pro, and Agency plans and their Instagram account, content, and AI allowances.", "sections": []}
        return render_template("public_product.html", page=page, canonical_url=origin() + "/pricing", plans=plans())


def request_path(slug):
    return "/features/" + slug
