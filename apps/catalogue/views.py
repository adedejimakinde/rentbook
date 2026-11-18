"""Public catalogue pages. Views stay thin; queries and wording live in services."""

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_headers

from apps.catalogue.models import Category, ItemImage, Package
from apps.catalogue.services import listing
from apps.core.services import get_event_date, get_event_zip, set_event_date

GALLERY_PAGE_SIZE = 24


def is_htmx(request):
    """True for requests made by HTMX, which want a partial template."""
    return request.headers.get("HX-Request") == "true"


def _rental_seo(category):
    if category is None:
        return (
            "Party rental catalogue",
            "Browse bounce houses, chairs, tables, linen, backdrops, concession machines and "
            "a golf simulator for events across DFW. Pick a date to see what is free.",
        )
    return (
        f"{category.name} rentals in DFW",
        category.description
        or f"Rent {category.name.lower()} for your party or event in the DFW area.",
    )


@require_GET
@vary_on_headers("HX-Request")
def catalogue(request, category_slug=None):
    """List rentals, optionally for one category, with availability for the chosen date."""
    category = get_object_or_404(Category, slug=category_slug) if category_slug else None
    date_error = set_event_date(request, request.GET["date"]) if "date" in request.GET else ""
    event_date = get_event_date(request)
    page = listing.catalogue_page(category, request.GET.get("page"))
    title, description = _rental_seo(category)
    context = {
        "category": category,
        "categories": listing.categories_with_items(),
        "page": page,
        "event_date": event_date,
        "event_zip": get_event_zip(request),
        "date_error": date_error,
        "statuses": listing.item_statuses(page.object_list, event_date) if event_date else {},
        "meta_title": title,
        "meta_description": description,
    }
    template = "catalogue/_results.html" if is_htmx(request) else "catalogue/list.html"
    return render(request, template, context)


def _availability_context(request, item):
    """Work out the variant, quantity and availability message for the item page."""
    variants = list(item.variants.all())
    try:
        selected = next(v for v in variants if str(v.pk) == request.GET.get("variant"))
    except StopIteration:
        selected = variants[0] if variants else None
    try:
        quantity = max(1, min(int(request.GET.get("quantity", 1)), 999))
    except ValueError:
        quantity = 1
    date_error = ""
    if "date" in request.GET:
        date_error = set_event_date(request, request.GET["date"])
    event_date = get_event_date(request)
    status = None
    if selected is not None and event_date:
        in_stock = listing.availability.available_quantities([selected], event_date)
        status = listing.variant_status(selected, in_stock, quantity)
    return {
        "item": item,
        "variants": variants,
        "selected": selected,
        "quantity": quantity,
        "event_date": event_date,
        "date_error": date_error,
        "status": status,
        "line_total": listing.line_total(selected, quantity),
        "ask_price_url": (
            listing.price_enquiry_url(item.name, selected.name, event_date)
            if selected is not None and not selected.unit_price
            else ""
        ),
    }


@require_GET
def item_detail(request, slug):
    """One rental with gallery, variant picker, live availability and related items."""
    item = get_object_or_404(listing.active_items(), slug=slug)
    context = _availability_context(request, item)
    cover = item.cover_image()
    context.update(
        {
            "related": listing.related_items(item),
            "meta_title": f"{item.name} rental in DFW",
            "meta_description": item.short_description
            or f"Rent a {item.name.lower()} for your event in the DFW area.",
            "meta_image": cover,
            "meta_type": "product",
        }
    )
    return render(request, "catalogue/item_detail.html", context)


@require_GET
def item_availability(request, slug):
    """HTMX partial: availability for the chosen variant, quantity and date."""
    item = get_object_or_404(listing.active_items(), slug=slug)
    return render(request, "catalogue/_availability.html", _availability_context(request, item))


@require_GET
def package_list(request):
    """All active packages."""
    packages = Package.objects.filter(is_active=True).prefetch_related("items__variant__item")
    return render(
        request,
        "catalogue/package_list.html",
        {
            "packages": packages,
            "meta_title": "Party rental packages",
            "meta_description": "Ready-made rental bundles for DFW parties, weddings and "
            "corporate events, so you can book one package instead of piecing items together.",
        },
    )


@require_GET
def package_detail(request, slug):
    """One package and the items it includes."""
    package = get_object_or_404(
        Package.objects.filter(is_active=True).prefetch_related("items__variant__item"), slug=slug
    )
    lines = list(package.items.all())
    if not lines:
        raise Http404
    names = ", ".join(f"{line.quantity} x {line.variant.item.name}" for line in lines[:4])
    return render(
        request,
        "catalogue/package_detail.html",
        {
            "package": package,
            "lines": lines,
            "ask_price_url": (
                ""
                if package.package_price
                else listing.price_enquiry_url(
                    f"{package.name} package", event_date=get_event_date(request)
                )
            ),
            "meta_title": f"{package.name} package",
            "meta_description": f"{package.name}: {names}. Delivered and set up across DFW.",
        },
    )


@require_GET
def gallery(request):
    """Photos of our rentals, linked to their item pages."""
    photos = ItemImage.objects.filter(renditions_ready=True).select_related("item")
    page = Paginator(photos, GALLERY_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "catalogue/gallery.html",
        {
            "page": page,
            "meta_title": "Gallery",
            "meta_description": "Photos of our chairs, tables, bounce houses, backdrops and "
            "machines, ready for your DFW event.",
        },
    )
