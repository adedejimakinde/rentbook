"""Site-level views."""

from django.conf import settings
from django.contrib import messages
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.catalogue.services import listing
from apps.core import services
from apps.core.forms import ContactForm

CONTACT_LIMIT_PER_HOUR = 5

FAQS = [
    (
        "Do you deliver and set up?",
        "Yes. We deliver, set up and pick up across DFW. Delivery and setup fees depend on your "
        "ZIP code, so enter it in the quick check on the home page to see whether we serve it.",
    ),
    (
        "How far ahead do I need to book?",
        "We need at least {notice} hours' notice before the event day. Weekends in spring and "
        "fall go first, so book as soon as you have a date.",
    ),
    (
        "How do I pay?",
        "Pay a {deposit}% deposit to hold your date, or pay in full. Cards and bank transfers "
        "(ACH) go through Stripe. You can also pay by Zelle, Cash App or bank deposit, and we "
        "mark it received once it arrives. An invoice is sent automatically.",
    ),
    (
        "What do bounce houses and machines need at my venue?",
        "Inflatables need a flat area with room around them. Bounce houses, popcorn machines and "
        "cotton candy machines need electricity. If there is no outlet near your setup area, add "
        "a generator and we will bring it with the rest.",
    ),
    (
        "What if it rains or the wind picks up?",
        "Inflatables should not be used in rain or strong wind, so we may have to pause or stop "
        "one for safety on the day. Call or WhatsApp us as soon as the forecast turns and we "
        "will talk through your options.",
    ),
    (
        "Is there a deposit on the equipment itself?",
        "Some items carry a refundable caution deposit. It is shown in your quote before you pay "
        "and is returned after pickup once the equipment comes back in good condition.",
    ),
    (
        "Do you work with churches, schools and companies?",
        "Yes. We supply church events, school fun days, corporate parties and weddings, as well "
        "as backyard birthdays. For a large event, message us on WhatsApp with the date and the "
        "headcount and we will help you work out quantities.",
    ),
    (
        "Which areas do you serve?",
        "We serve the DFW area. Use the ZIP check on the home page, or call us if your ZIP is "
        "not listed. We may still be able to help.",
    ),
]


def _page(request, template, title, description, **extra):
    return render(
        request, template, {"meta_title": title, "meta_description": description, **extra}
    )


@require_GET
def home(request):
    """Render the home page."""
    featured = listing.featured_items()
    categories = list(listing.categories_with_items())
    event_date = services.get_event_date(request)
    return _page(
        request,
        "core/home.html",
        "Party rentals across DFW",
        "Bounce houses, chairs, tables, linen, backdrops, concession machines and a golf "
        "simulator, delivered and set up across the Dallas-Fort Worth area.",
        categories=categories,
        event_tabs=services.event_tabs(categories),
        event_types=services.EVENT_TYPES,
        reviews=services.published_reviews(),
        featured=featured,
        statuses=listing.item_statuses(featured, event_date) if event_date and featured else {},
        event_date=event_date,
        event_zip=services.get_event_zip(request),
    )


@require_POST
def quick_check(request):
    """Check a date and ZIP, remember them for the session, and report delivery coverage."""
    result = services.quick_check(request, request.POST.get("date"), request.POST.get("zip"))
    template = (
        "core/_quick_check_result.html"
        if request.headers.get("HX-Request") == "true"
        else "core/quick_check.html"
    )
    return _page(
        request,
        template,
        "Check availability",
        "Check your event date and delivery ZIP for a DFW party rental.",
        result=result,
    )


@require_GET
def about(request):
    return _page(
        request,
        "core/about.html",
        "About us",
        "Atglace Events & Rentals supplies bounce houses, seating, linen, backdrops and "
        "machines for DFW parties, weddings, schools, churches and companies.",
    )


@require_GET
def faq(request):
    faqs = [
        (
            question,
            answer.format(notice=settings.MIN_NOTICE_HOURS, deposit=settings.DEPOSIT_PERCENT),
        )
        for question, answer in FAQS
    ]
    return _page(
        request,
        "core/faq.html",
        "FAQs",
        "Answers about delivery, setup, deposits, payment, power and weather for party rentals "
        "in the Dallas-Fort Worth area.",
        faqs=faqs,
    )


@require_http_methods(["GET", "POST"])
def contact(request):
    """Show the enquiry form and email it to the business."""
    form = ContactForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["website"]:
            return redirect("core:contact")
        if services.contact_rate_limited(request, CONTACT_LIMIT_PER_HOUR):
            form.add_error(None, "You have sent a lot of messages. Please call us instead.")
        else:
            data = form.cleaned_data
            services.send_contact_message(
                name=data["name"],
                email=data["email"],
                phone=data["phone"],
                event_date=data["event_date"],
                message=data["message"],
            )
            messages.success(request, "Thanks, we have your message and will reply soon.")
            return redirect("core:contact")
    return _page(
        request,
        "core/contact.html",
        "Contact us",
        "Message Atglace Events & Rentals about your DFW party rental, or call or WhatsApp us "
        "for a quick answer on dates and delivery.",
        form=form,
    )


TERMS_SECTIONS = [
    ("cancellation", "Cancellation and changes"),
    ("weather", "Weather"),
    ("damage", "Damage, loss and the caution deposit"),
    ("setup", "Setup requirements"),
]


@require_GET
def terms(request):
    """Rental terms customers accept at checkout. TERMS_VERSION names this version."""
    return _page(
        request,
        "core/terms.html",
        "Rental terms",
        "The rental terms for bookings with Atglace Events & Rentals: cancellation, weather, "
        "damage and setup requirements.",
        sections=TERMS_SECTIONS,
        terms_version=settings.TERMS_VERSION,
    )


@require_GET
def links(request):
    """Link-in-bio page with big buttons for social profiles."""
    return _page(
        request,
        "core/links.html",
        "Links",
        "Book party rentals, browse our catalogue, chat on WhatsApp or call Atglace Events & "
        "Rentals in DFW.",
    )


@require_GET
def manifest(request):
    """Web app manifest listing the site icons."""
    data = {
        "name": settings.BUSINESS_NAME,
        "short_name": "Atglace",
        "start_url": "/",
        "display": "browser",
        "theme_color": "#6A0DAD",
        "background_color": "#FFFFFF",
        "icons": [
            {"src": static("brand/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("brand/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


@require_GET
def robots(request):
    """robots.txt pointing crawlers at the sitemap and away from private paths."""
    lines = [
        "User-agent: *",
        "Disallow: /payments/",
        "Disallow: /cart/",
        "Disallow: /book/",
        "Disallow: /booking/",
        "Disallow: /health/",
        "Disallow: /check/",
        f"Sitemap: {settings.SITE_URL}/sitemap.xml",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@never_cache
@require_GET
def health(request):
    """Report liveness and database connectivity for load balancers and uptime checks."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "up"})
