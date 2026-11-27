"""Public, server-rendered product pages describing implemented capabilities.

Every page ships in English, French and Arabic: PAGES holds one
title/heading/description/sections set per locale, and the template chrome
(``public_product.html``) renders through the shared ``t()`` helper.
"""

import os

from flask import render_template, request, session, url_for

from dashboard.i18n import LOCALE_META, locale_context, normalize_locale, session_locale, translate
from instagram_oauth import validated_public_origin
from database.billing import plans

PUBLIC_PAGES = ("/", "/features/instagram-scheduling", "/features/instagram-automation", "/pricing", "/privacy", "/terms", "/deletion")

# Locales served by the session-based locale picker (see dashboard/i18n.py).
# There are no distinct per-locale URL paths, so hreflang can only point at
# the canonical URL itself: hreflang_links() therefore emits x-default + en.
# Full per-locale hreflang needs locale URL paths, which is a webhook change
# outside this SEO pass.
HREFLANG_LOCALES = ("en", "fr", "ar")
LANDING_KEYWORDS = (
    "instagram automation",
    "instagram auto reply",
    "instagram dm automation",
    "schedule instagram posts",
    "instagram comment automation",
)


def hreflang_links(canonical_url):
    """Alternate-language links for a canonical public URL.

    Only x-default and en are emitted: the site serves fr/ar through a
    session locale on the same URL, so no distinct fr/ar URL exists to point
    at. Emitting fr/ar hreflang to the identical URL would be invalid.
    """
    return [
        {"hreflang": "x-default", "href": canonical_url},
        {"hreflang": "en", "href": canonical_url},
    ]


PAGES = {
    "instagram-scheduling": {
        "en": {
            "title": "Schedule Instagram posts | Instagram automation — Postenza",
            "heading": "Plan your Instagram image posts and Reels in one workspace",
            "description": "Schedule Instagram posts with Postenza's Instagram automation: prepare image posts and Reels, add captions, choose a publishing time, and track publishing status in one workspace.",
            "sections": [
                ("From draft to scheduled post", "Create a draft with a caption and a public image or video link, or upload a JPEG when media storage is configured. Review it, then choose a future time in your workspace calendar."),
                ("Know what happened after scheduling", "Postenza records scheduled, publishing, published, and failed states. If a provider request has an uncertain result, check Instagram before retrying to avoid a duplicate post."),
                ("Connect an eligible professional account", "Connect your Instagram professional account through Instagram Login. Publishing depends on the permissions granted to Postenza and on Meta's platform availability. Scheduled publishing currently supports image posts and Reels."),
            ],
        },
        "fr": {
            "title": "Planifier des publications Instagram | Automatisation Instagram — Postenza",
            "heading": "Planifiez vos publications image et vos Reels dans un seul espace",
            "description": "Planifiez vos publications Instagram avec l’automatisation Instagram de Postenza : préparez des publications image et des Reels, ajoutez des légendes, choisissez une heure de publication et suivez le statut de publication dans un seul espace.",
            "sections": [
                ("Du brouillon à la publication planifiée", "Créez un brouillon avec une légende et un lien public vers une image ou une vidéo, ou importez un JPEG lorsque le stockage multimédia est configuré. Vérifiez-le, puis choisissez une date future dans votre calendrier."),
                ("Sachez ce qui s’est passé après la planification", "Postenza enregistre les états planifié, en cours de publication, publié et en échec. Si le résultat d’une demande est incertain, vérifiez Instagram avant de réessayer pour éviter un doublon."),
                ("Connectez un compte professionnel éligible", "Connectez votre compte Instagram professionnel via Instagram Login. La publication dépend des autorisations accordées à Postenza et de la disponibilité de la plateforme Meta. La publication planifiée prend actuellement en charge les publications image et les Reels."),
            ],
        },
        "ar": {
            "title": "جدولة منشورات إنستغرام | أتمتة إنستغرام — Postenza",
            "heading": "خطط لمنشورات صور إنستغرام وريلز في مساحة عمل واحدة",
            "description": "جدول منشورات إنستغرام مع أتمتة إنستغرام من Postenza: أعدّ منشورات الصور وريلز، وأضف النصوص، واختر وقت النشر، وتابع حالة النشر في مساحة عمل واحدة.",
            "sections": [
                ("من المسودة إلى المنشور المجدول", "أنشئ مسودة بنص ورابط عام لصورة أو فيديو، أو ارفع صورة JPEG عند إعداد التخزين. راجعها، ثم اختر وقتًا مستقبليًا في تقويم مساحة عملك."),
                ("اعرف ما حدث بعد الجدولة", "يسجل Postenza الحالات المجدولة وقيد النشر والمنشورة والفاشلة. وإذا كانت نتيجة طلب ما غير مؤكدة، تحقق من إنستغرام قبل إعادة المحاولة لتجنب منشور مكرر."),
                ("اربط حسابًا احترافيًا مؤهلًا", "اربط حسابك الاحترافي على إنستغرام عبر Instagram Login. يعتمد النشر على الأذونات الممنوحة لـ Postenza وعلى توفر منصة Meta. تدعم الجدولة حاليًا منشورات الصور وريلز."),
            ],
        },
    },
    "instagram-automation": {
        "en": {
            "title": "Instagram auto reply & DM automation — Postenza",
            "heading": "Turn Instagram comments into organized conversations",
            "description": "Instagram auto reply and Instagram DM automation with Postenza: configure keyword replies, private message workflows, follow-ups, and lead activity for connected Instagram professional accounts.",
            "sections": [
                ("Set your own reply rules", "Choose trigger keywords, write a public reply, and configure a private delivery message. Enable only the workflows that fit the account you manage."),
                ("Follow the conversation", "View message activity, prompt requests, and follow-ups in the same workspace. Replies always use your ready-made texts, so they never stop; Gemini is used only to read prompts written in post images."),
                ("Respect Instagram messaging rules", "Private replies and follow-ups depend on Meta permissions, supported interactions, and messaging windows. Postenza does not guarantee delivery outside those rules."),
            ],
        },
        "fr": {
            "title": "Réponse auto Instagram et DM automatisés — Postenza",
            "heading": "Transformez les commentaires Instagram en conversations organisées",
            "description": "Réponse auto Instagram et DM automatisés avec Postenza : configurez des réponses par mot-clé, des parcours de messages privés, des suivis et l’activité des prospects pour les comptes Instagram professionnels connectés.",
            "sections": [
                ("Définissez vos propres règles de réponse", "Choisissez des mots-clés déclencheurs, rédigez une réponse publique et configurez un message privé d’envoi. Activez uniquement les parcours adaptés au compte que vous gérez."),
                ("Suivez la conversation", "Consultez l’activité des messages, les demandes de prompts et les suivis dans le même espace. Les réponses utilisent toujours vos textes prêts, donc elles ne s’arrêtent jamais ; Gemini sert uniquement à lire les prompts écrits dans les images des publications."),
                ("Respectez les règles de messagerie Instagram", "Les réponses privées et les suivis dépendent des autorisations Meta, des interactions prises en charge et des fenêtres de messagerie. Postenza ne garantit pas la livraison en dehors de ces règles."),
            ],
        },
        "ar": {
            "title": "الرد التلقائي على إنستغرام وأتمتة رسائل DM — Postenza",
            "heading": "حوّل تعليقات إنستغرام إلى محادثات منظمة",
            "description": "الرد التلقائي على إنستغرام وأتمتة رسائل DM مع Postenza: اضبط الردود بالكلمات المفتاحية، وسير الرسائل الخاصة، والمتابعات، ونشاط العملاء المحتملين لحسابات إنستغرام الاحترافية المتصلة.",
            "sections": [
                ("حدد قواعد الرد الخاصة بك", "اختر الكلمات المفتاحية المشغلة، واكتب ردًا علنيًا، واضبط رسالة تسليم خاصة. فعّل فقط سير العمل المناسب للحساب الذي تديره."),
                ("تابع المحادثة", "اطلع على نشاط الرسائل وطلبات البرومبتات والمتابعات في مساحة العمل نفسها. تستخدم الردود دائمًا نصوصك الجاهزة، لذلك لا تتوقف أبدًا؛ ويُستخدم Gemini فقط لقراءة البرومبتات المكتوبة في صور المنشورات."),
                ("احترم قواعد مراسلة إنستغرام", "تعتمد الردود الخاصة والمتابعات على أذونات Meta والتفاعلات المدعومة ومهل المراسلة. لا تضمن Postenza التسليم خارج هذه القواعد."),
            ],
        },
    },
}

PRICING_PAGE = {
    "en": {
        "title": "Pricing — Instagram automation from $9/mo | Postenza",
        "heading": "A plan for your Instagram workflow",
        "description": "Compare Postenza's Instagram automation plans — schedule Instagram posts and automate replies — and their Instagram account, content, and AI allowances.",
        "sections": [],
    },
    "fr": {
        "title": "Tarifs — Automatisation Instagram dès 9 $/mois | Postenza",
        "heading": "Une offre pour votre flux Instagram",
        "description": "Comparez les offres d’automatisation Instagram de Postenza — planifiez vos publications Instagram et automatisez vos réponses — ainsi que leurs quotas de comptes Instagram, de contenu et d’IA.",
        "sections": [],
    },
    "ar": {
        "title": "الأسعار — أتمتة إنستغرام من 9 دولارات شهريًا | Postenza",
        "heading": "خطة لسير عملك على إنستغرام",
        "description": "قارن بين خطط أتمتة إنستغرام من Postenza — جدول منشورات إنستغرام وقم بأتمتة الردود — وحصصها من حسابات إنستغرام والمحتوى والذكاء الاصطناعي.",
        "sections": [],
    },
}


def _public_context():
    """Locale, CSRF token and ``t()`` for the public product pages."""
    from auth import csrf_token

    locale = locale_context(session_locale(session))
    return {
        "locale": locale,
        "locale_options": LOCALE_META,
        "csrf_token": csrf_token(),
        "t": lambda key, **values: translate(locale["code"], key, **values),
    }


def origin():
    return validated_public_origin(os.getenv("PUBLIC_BASE_URL", "")) or "https://postenza.app"


def register_seo(app):
    @app.get("/features/<slug>")
    def product_feature(slug):
        from flask import abort
        if slug not in PAGES:
            abort(404)
        code = normalize_locale(session_locale(session))
        return render_template("public_product.html", page=PAGES[slug][code], canonical_url=origin() + request_path(slug), plans=None, **_public_context())

    @app.get("/pricing")
    def pricing():
        code = normalize_locale(session_locale(session))
        return render_template("public_product.html", page=PRICING_PAGE[code], canonical_url=origin() + "/pricing", plans=plans(), **_public_context())


def request_path(slug):
    return "/features/" + slug
