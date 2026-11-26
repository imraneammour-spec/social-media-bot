"""Small session-backed UI localization helpers for the tenant dashboard."""

from __future__ import annotations

SUPPORTED_LOCALES = ("en", "fr", "ar")
DEFAULT_LOCALE = "en"
LOCALE_SESSION_KEY = "postenza_locale"
LEGACY_LOCALE_SESSION_KEY = "sociora_locale"


def session_locale(values):
    """Retain language preferences from existing signed sessions."""
    return values.get(LOCALE_SESSION_KEY, values.get(LEGACY_LOCALE_SESSION_KEY))

LOCALE_META = {
    "en": {"label": "English", "direction": "ltr"},
    "fr": {"label": "Français", "direction": "ltr"},
    "ar": {"label": "العربية", "direction": "rtl"},
}

TRANSLATIONS = {
    "en": {
        "Billing": "Billing", "product_name": "Postenza",
        "overview": "Overview", "content": "Content", "calendar": "Calendar",
        "engagement": "Engagement", "automation": "Automation", "instagram": "Instagram",
        "sign_out": "Sign out", "instagram_workspace": "Instagram workspace",
        "account": "Instagram account", "language": "Language",
        "automation_on": "Automation is on", "automation_off": "Automation is off",
        "social_tagline": "Social media, on autopilot.",
    },
    "fr": {
        "product_name": "Postenza",
        "Billing": "Facturation", "overview": "Vue d’ensemble", "content": "Contenu", "calendar": "Calendrier",
        "engagement": "Interactions", "automation": "Automatisation", "instagram": "Instagram",
        "sign_out": "Se déconnecter", "instagram_workspace": "Espace Instagram",
        "account": "Compte Instagram", "language": "Langue",
        "automation_on": "L’automatisation est activée", "automation_off": "L’automatisation est désactivée",
        "social_tagline": "Les réseaux sociaux en pilote automatique.",
    },
    "ar": {
        "product_name": "Postenza",
        "Billing": "الفوترة", "overview": "نظرة عامة", "content": "المحتوى", "calendar": "التقويم",
        "engagement": "التفاعل", "automation": "الأتمتة", "instagram": "إنستغرام",
        "sign_out": "تسجيل الخروج", "instagram_workspace": "مساحة عمل إنستغرام",
        "account": "حساب إنستغرام", "language": "اللغة",
        "automation_on": "الأتمتة مفعّلة", "automation_off": "الأتمتة متوقفة",
        "social_tagline": "وسائل التواصل الاجتماعي، على الطيار الآلي.",
    },
}


LANDING_TRANSLATIONS = {
    "en": {
        "landing_title": "Postenza | Instagram automation made simple",
        "landing_description": "Schedule image posts, automate Instagram engagement, and manage message activity from one Postenza workspace.",
        "landing_features": "Features", "landing_how_it_works": "How it works", "landing_automation": "Automation", "landing_faq": "FAQ", "landing_login": "Login", "landing_get_started": "Get Started", "landing_menu": "Open navigation", "landing_close_menu": "Close navigation", "landing_status_on": "On",
        "landing_hero_title": "Put your Instagram on autopilot.", "landing_hero_copy": "Schedule content, automate replies, manage engagement, and keep your Instagram active from one simple workspace.", "landing_see_how": "See How It Works", "landing_eyebrow": "Built for a focused Instagram workflow",
        "landing_strip_instagram": "Instagram-focused", "landing_strip_publish": "Scheduled image publishing", "landing_strip_controls": "Automation controls", "landing_strip_accounts": "Multiple accounts",
        "landing_schedule_title": "Schedule content", "landing_schedule_copy": "Plan image posts and publish automatically.", "landing_replies_title": "Automate replies", "landing_replies_copy": "Respond to matching comments with the rules you choose.", "landing_followups_title": "Private follow-ups", "landing_followups_copy": "Continue interactions privately when you configure it.", "landing_smart_title": "Smart replies", "landing_smart_copy": "Handle other comments with configurable smart responses.", "landing_engagement_title": "Manage engagement", "landing_engagement_copy": "Review leads, message activity, engagement history, and follow-ups in one place.", "landing_accounts_title": "Multiple Instagram accounts", "landing_accounts_copy": "Switch between connected Instagram accounts when you need to.",
        "landing_workflow_title": "A clear path from connection to control", "landing_step_connect": "Connect Instagram", "landing_step_connect_copy": "Connect a professional Instagram account.", "landing_step_configure": "Configure your workspace", "landing_step_configure_copy": "Set up your content and automation preferences.", "landing_step_schedule": "Schedule or automate", "landing_step_schedule_copy": "Choose when an image post publishes and what engagement rules run.", "landing_step_monitor": "Monitor everything", "landing_step_monitor_copy": "Keep a clear view of content, message activity, and engagement history.",
        "landing_publishing_label": "Publishing", "landing_publishing_title": "Create, schedule, and track image posts", "landing_publishing_copy": "Postenza currently supports image publishing. Every item keeps a clear lifecycle so you can see what is scheduled, published, or needs attention.", "landing_image_only": "Image posts only", "landing_create": "Create", "landing_schedule": "Schedule", "landing_publish": "Automatic publish", "landing_track": "Track status",
        "landing_automation_title": "Make every response intentional", "landing_automation_copy": "Choose which controls are on. Postenza helps organize public replies, private messages, follow-ups, and smart replies without taking control away from you.", "landing_comment": "Comment or keyword", "landing_public_reply": "Public reply", "landing_private": "Optional private message", "landing_followup": "Optional follow-up", "landing_smart_replies": "Smart replies",
        "landing_connection_label": "Instagram connection", "landing_connection_title": "Keep account status clear", "landing_connection_copy": "Connect a professional Instagram account, see its connection status, and reconnect or disconnect when needed.", "landing_faq_title": "Frequently asked questions",
        "landing_faq_accounts_q": "What Instagram accounts can I connect?", "landing_faq_accounts_a": "Postenza is built for professional Instagram accounts supported by the connection flow.", "landing_faq_schedule_q": "Can Postenza schedule posts automatically?", "landing_faq_schedule_a": "Yes. You can schedule supported image posts for automatic publishing.", "landing_faq_replies_q": "Does Postenza reply to comments automatically?", "landing_faq_replies_a": "It can when you enable and configure the relevant automation controls.", "landing_faq_off_q": "Can I turn automation off?", "landing_faq_off_a": "Yes. Automation controls remain configurable for each connected account.", "landing_faq_multi_q": "Does Postenza support multiple Instagram accounts?", "landing_faq_multi_a": "You can switch between connected Instagram accounts in your workspace.", "landing_faq_video_q": "Does Postenza support video or Reels yet?", "landing_faq_video_a": "Not in the current publishing workflow. Image publishing is supported today.",
        "landing_final_title": "Ready to put your Instagram on autopilot?", "landing_final_copy": "Start with a clearer workspace for content and engagement.", "landing_product": "Product", "landing_privacy": "Privacy", "landing_terms": "Terms", "landing_data_deletion": "Data Deletion",
    },
    "fr": {
        "landing_title": "Postenza | L’automatisation Instagram simplifiée", "landing_description": "Planifiez des publications image, automatisez l’engagement Instagram et suivez l’activité des messages dans un seul espace Postenza.", "landing_features": "Fonctionnalités", "landing_how_it_works": "Comment ça marche", "landing_automation": "Automatisation", "landing_faq": "FAQ", "landing_login": "Connexion", "landing_get_started": "Commencer", "landing_menu": "Ouvrir la navigation", "landing_close_menu": "Fermer la navigation", "landing_status_on": "Activée",
        "landing_hero_title": "Mettez votre Instagram en pilote automatique.", "landing_hero_copy": "Planifiez du contenu, automatisez les réponses, gérez les interactions et gardez votre Instagram actif depuis un espace simple.", "landing_see_how": "Voir comment ça marche", "landing_eyebrow": "Conçu pour un flux Instagram maîtrisé", "landing_strip_instagram": "Pensé pour Instagram", "landing_strip_publish": "Publication d’images planifiée", "landing_strip_controls": "Contrôles d’automatisation", "landing_strip_accounts": "Plusieurs comptes",
        "landing_schedule_title": "Planifier du contenu", "landing_schedule_copy": "Préparez des publications image et publiez-les automatiquement.", "landing_replies_title": "Automatiser les réponses", "landing_replies_copy": "Répondez aux commentaires correspondants avec les règles que vous choisissez.", "landing_followups_title": "Suivis privés", "landing_followups_copy": "Poursuivez les interactions en privé lorsque vous le configurez.", "landing_smart_title": "Réponses intelligentes", "landing_smart_copy": "Gérez les autres commentaires avec des réponses intelligentes configurables.", "landing_engagement_title": "Gérer les interactions", "landing_engagement_copy": "Consultez les prospects, l’activité des messages, l’historique des interactions et les suivis au même endroit.", "landing_accounts_title": "Plusieurs comptes Instagram", "landing_accounts_copy": "Basculez entre vos comptes Instagram connectés selon vos besoins.",
        "landing_workflow_title": "Un chemin clair de la connexion au contrôle", "landing_step_connect": "Connectez Instagram", "landing_step_connect_copy": "Connectez un compte Instagram professionnel.", "landing_step_configure": "Configurez votre espace", "landing_step_configure_copy": "Réglez vos préférences de contenu et d’automatisation.", "landing_step_schedule": "Planifiez ou automatisez", "landing_step_schedule_copy": "Choisissez quand une image est publiée et quelles règles d’interaction s’exécutent.", "landing_step_monitor": "Gardez tout à l’œil", "landing_step_monitor_copy": "Visualisez clairement le contenu, l’activité des messages et l’historique des interactions.",
        "landing_publishing_label": "Publication", "landing_publishing_title": "Créez, planifiez et suivez vos publications image", "landing_publishing_copy": "Postenza prend actuellement en charge la publication d’images. Chaque élément conserve un cycle clair : planifié, publié ou à vérifier.", "landing_image_only": "Publications image uniquement", "landing_create": "Créer", "landing_schedule": "Planifier", "landing_publish": "Publication automatique", "landing_track": "Suivre le statut",
        "landing_automation_title": "Rendez chaque réponse intentionnelle", "landing_automation_copy": "Choisissez les contrôles actifs. Postenza organise les réponses publiques, messages privés, suivis et réponses intelligentes sans vous enlever le contrôle.", "landing_comment": "Commentaire ou mot-clé", "landing_public_reply": "Réponse publique", "landing_private": "Message privé facultatif", "landing_followup": "Suivi facultatif", "landing_smart_replies": "Réponses intelligentes", "landing_connection_label": "Connexion Instagram", "landing_connection_title": "Gardez un statut de compte clair", "landing_connection_copy": "Connectez un compte Instagram professionnel, consultez son statut, reconnectez-le ou déconnectez-le si nécessaire.", "landing_faq_title": "Questions fréquentes",
        "landing_faq_accounts_q": "Quels comptes Instagram puis-je connecter ?", "landing_faq_accounts_a": "Postenza est conçu pour les comptes Instagram professionnels pris en charge par le flux de connexion.", "landing_faq_schedule_q": "Postenza peut-il planifier des publications automatiquement ?", "landing_faq_schedule_a": "Oui. Vous pouvez planifier des publications image compatibles pour une publication automatique.", "landing_faq_replies_q": "Postenza répond-il automatiquement aux commentaires ?", "landing_faq_replies_a": "Il le peut lorsque vous activez et configurez les contrôles d’automatisation correspondants.", "landing_faq_off_q": "Puis-je désactiver l’automatisation ?", "landing_faq_off_a": "Oui. Les contrôles restent configurables pour chaque compte connecté.", "landing_faq_multi_q": "Postenza prend-il en charge plusieurs comptes Instagram ?", "landing_faq_multi_a": "Vous pouvez basculer entre les comptes Instagram connectés dans votre espace.", "landing_faq_video_q": "Postenza prend-il déjà en charge les vidéos ou Reels ?", "landing_faq_video_a": "Pas dans le flux de publication actuel. La publication d’images est prise en charge aujourd’hui.",
        "landing_final_title": "Prêt à mettre votre Instagram en pilote automatique ?", "landing_final_copy": "Commencez avec un espace plus clair pour votre contenu et vos interactions.", "landing_product": "Produit", "landing_privacy": "Confidentialité", "landing_terms": "Conditions", "landing_data_deletion": "Suppression des données",
    },
    "ar": {
        "landing_title": "Postenza | أتمتة إنستغرام ببساطة", "landing_description": "جدول منشورات الصور، وأتمت تفاعل إنستغرام، وتابع نشاط الرسائل من مساحة Postenza واحدة.", "landing_features": "الميزات", "landing_how_it_works": "كيف يعمل", "landing_automation": "الأتمتة", "landing_faq": "الأسئلة الشائعة", "landing_login": "تسجيل الدخول", "landing_get_started": "ابدأ الآن", "landing_menu": "فتح التنقل", "landing_close_menu": "إغلاق التنقل", "landing_status_on": "مفعّلة",
        "landing_hero_title": "ضع إنستغرام الخاص بك على الطيار الآلي.", "landing_hero_copy": "جدول المحتوى، وأتمت الردود، وأدر التفاعل، وحافظ على نشاط إنستغرام من مساحة عمل بسيطة واحدة.", "landing_see_how": "اكتشف كيف يعمل", "landing_eyebrow": "مصمم لتدفق إنستغرام واضح", "landing_strip_instagram": "مصمم لإنستغرام", "landing_strip_publish": "نشر صور مجدول", "landing_strip_controls": "عناصر تحكم بالأتمتة", "landing_strip_accounts": "حسابات متعددة",
        "landing_schedule_title": "جدولة المحتوى", "landing_schedule_copy": "خطط لمنشورات الصور وانشرها تلقائياً.", "landing_replies_title": "أتمتة الردود", "landing_replies_copy": "استجب للتعليقات المطابقة بالقواعد التي تختارها.", "landing_followups_title": "متابعات خاصة", "landing_followups_copy": "واصل التفاعلات بشكل خاص عند إعداد ذلك.", "landing_smart_title": "ردود ذكية", "landing_smart_copy": "تعامل مع التعليقات الأخرى بردود ذكية قابلة للضبط.", "landing_engagement_title": "إدارة التفاعل", "landing_engagement_copy": "راجع العملاء المحتملين ونشاط الرسائل وسجل التفاعل والمتابعات في مكان واحد.", "landing_accounts_title": "حسابات إنستغرام متعددة", "landing_accounts_copy": "بدل بين حسابات إنستغرام المتصلة عندما تحتاج.",
        "landing_workflow_title": "مسار واضح من الاتصال إلى التحكم", "landing_step_connect": "صل إنستغرام", "landing_step_connect_copy": "صل حساب إنستغرام احترافياً.", "landing_step_configure": "اضبط مساحة عملك", "landing_step_configure_copy": "أعد تفضيلات المحتوى والأتمتة الخاصة بك.", "landing_step_schedule": "جدول أو أتمت", "landing_step_schedule_copy": "اختر وقت نشر الصورة وقواعد التفاعل التي تعمل.", "landing_step_monitor": "راقب كل شيء", "landing_step_monitor_copy": "احتفظ برؤية واضحة للمحتوى ونشاط الرسائل وسجل التفاعل.",
        "landing_publishing_label": "النشر", "landing_publishing_title": "أنشئ وجدول وتابع منشورات الصور", "landing_publishing_copy": "يدعم Postenza حالياً نشر الصور. يحتفظ كل عنصر بحالة واضحة لتعرف ما هو مجدول أو منشور أو يحتاج إلى انتباه.", "landing_image_only": "منشورات الصور فقط", "landing_create": "إنشاء", "landing_schedule": "جدولة", "landing_publish": "نشر تلقائي", "landing_track": "تتبع الحالة",
        "landing_automation_title": "اجعل كل رد مقصوداً", "landing_automation_copy": "اختر عناصر التحكم المفعلة. ينظم Postenza الردود العامة والرسائل الخاصة والمتابعات والردود الذكية دون أن يسلبك التحكم.", "landing_comment": "تعليق أو كلمة مفتاحية", "landing_public_reply": "رد علني", "landing_private": "رسالة خاصة اختيارية", "landing_followup": "متابعة اختيارية", "landing_smart_replies": "ردود ذكية", "landing_connection_label": "اتصال إنستغرام", "landing_connection_title": "حافظ على وضوح حالة الحساب", "landing_connection_copy": "صل حساب إنستغرام احترافياً، واطلع على حالة الاتصال، وأعد الاتصال أو افصله عند الحاجة.", "landing_faq_title": "الأسئلة الشائعة",
        "landing_faq_accounts_q": "ما حسابات إنستغرام التي يمكنني وصلها؟", "landing_faq_accounts_a": "Postenza مصمم لحسابات إنستغرام الاحترافية التي يدعمها مسار الاتصال.", "landing_faq_schedule_q": "هل يستطيع Postenza جدولة المنشورات تلقائياً؟", "landing_faq_schedule_a": "نعم، يمكنك جدولة منشورات الصور المدعومة للنشر التلقائي.", "landing_faq_replies_q": "هل يرد Postenza على التعليقات تلقائياً؟", "landing_faq_replies_a": "يمكنه ذلك عند تفعيل وضبط عناصر تحكم الأتمتة ذات الصلة.", "landing_faq_off_q": "هل يمكنني إيقاف الأتمتة؟", "landing_faq_off_a": "نعم، تبقى عناصر التحكم قابلة للضبط لكل حساب متصل.", "landing_faq_multi_q": "هل يدعم Postenza حسابات إنستغرام متعددة؟", "landing_faq_multi_a": "يمكنك التبديل بين حسابات إنستغرام المتصلة في مساحة عملك.", "landing_faq_video_q": "هل يدعم Postenza الفيديو أو Reels حالياً؟", "landing_faq_video_a": "ليس في سير النشر الحالي. نشر الصور مدعوم اليوم.",
        "landing_final_title": "هل أنت مستعد لوضع إنستغرام على الطيار الآلي؟", "landing_final_copy": "ابدأ بمساحة أوضح للمحتوى والتفاعل.", "landing_product": "المنتج", "landing_privacy": "الخصوصية", "landing_terms": "الشروط", "landing_data_deletion": "حذف البيانات",
    },
}


for _locale, _engagement, _followups in (
    ("en", "Engagement activity", "Private follow-ups"),
    ("fr", "Activité des interactions", "Suivis privés"),
    ("ar", "نشاط التفاعل", "المتابعات الخاصة"),
):
    LANDING_TRANSLATIONS[_locale].update(
        landing_feature_engagement_title=_engagement,
        landing_feature_followups_title=_followups,
    )


def normalize_locale(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SUPPORTED_LOCALES else DEFAULT_LOCALE


def locale_context(value: object) -> dict[str, object]:
    code = normalize_locale(value)
    return {"code": code, **LOCALE_META[code]}


def translate(locale: object, key: object, **values) -> str:
    normalized = normalize_locale(locale)
    message_key = str(key or "")
    from .app_translations import APP_TRANSLATIONS
    translated = APP_TRANSLATIONS.get(normalized, {}).get(message_key)
    if translated is None:
        translated = LANDING_TRANSLATIONS.get(normalized, {}).get(
            message_key,
            TRANSLATIONS[normalized].get(
                message_key,
                LANDING_TRANSLATIONS[DEFAULT_LOCALE].get(
                    message_key,
                    TRANSLATIONS[DEFAULT_LOCALE].get(message_key, message_key),
                ),
            ),
        )
    return translated.format(**values) if values else translated
