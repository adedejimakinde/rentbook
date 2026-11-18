"""The starting catalogue: the client's real item list, unpriced and without photos."""

from django.db import transaction
from django.utils.text import slugify

from apps.catalogue.models import Category, Item, ItemVariant

# (category name, description, [(item name, short description, variant names, featured)])
CATALOGUE = [
    (
        "Bounce houses and inflatables",
        "Inflatables for birthdays, school fun days and church picnics, delivered, staked "
        "down and set up by our crew.",
        [
            ("Bounce house", "Classic inflatable bounce house for kids' parties.", [], True),
            (
                "Combo bounce house",
                "Bounce area plus a slide in one unit, for mixed-age parties.",
                [],
                True,
            ),
            ("Water slide", "Inflatable water slide for hot Texas afternoons.", [], False),
            ("Obstacle course", "Race-style inflatable course for bigger groups.", [], False),
        ],
    ),
    (
        "Tents and canopies",
        "Shade and cover for backyard parties, church grounds and school events.",
        [("Canopy", "Pop-up style canopy for shade at outdoor events.", [], False)],
    ),
    (
        "Tables",
        "Tables for guest seating, buffets and kids' corners.",
        [
            ("Round table", "Round banquet table for wedding and reception seating.", [], False),
            ("Rectangle table", "Rectangle table for buffets, gifts and long seating.", [], False),
            ("Kids table", "Kid-height table for the little guests.", [], False),
        ],
    ),
    (
        "Chairs",
        "Seating in the colours and styles that suit the event.",
        [
            ("Kids chair", "Kid-size chair to match the kids table.", [], False),
            (
                "Garden chair",
                "Lightweight garden chair for ceremonies and receptions.",
                ["Black", "White"],
                False,
            ),
            (
                "Chiavari chair",
                "Chiavari chair, the wedding and gala favourite.",
                ["Black", "Silver", "Gold"],
                True,
            ),
        ],
    ),
    (
        "Concessions",
        "Party food and drink machines your guests will line up for.",
        [
            ("Popcorn machine", "Fresh popcorn machine for parties and school events.", [], True),
            ("Cotton candy machine", "Cotton candy machine for birthdays and fairs.", [], False),
            ("Margarita machine", "Frozen drink machine for adult celebrations.", [], False),
            ("Slushy machine", "Slushy machine to keep the kids cool.", [], False),
        ],
    ),
    (
        "Decor",
        "Backdrops and table dressing for photos and head tables.",
        [
            ("Photo backdrop", "Backdrop for photos at weddings, showers and birthdays.", [], True),
            ("Table covers and linen", "Table covers and linen to dress your tables.", [], False),
        ],
    ),
    (
        "Experiences",
        "Something different that guests talk about after the party.",
        [("Golf simulator", "Indoor golf simulator for corporate and party fun.", [], True)],
    ),
    (
        "Power",
        "Reliable power for outdoor events.",
        [
            (
                "Generator",
                "Generator for inflatables and machines where there is no outlet.",
                [],
                False,
            )
        ],
    ),
]


# Items that need a power outlet. Set only when the item is first created.
NEEDS_POWER = {
    "Bounce house",
    "Combo bounce house",
    "Water slide",
    "Obstacle course",
    "Popcorn machine",
    "Cotton candy machine",
    "Margarita machine",
    "Slushy machine",
    "Golf simulator",
}


@transaction.atomic
def seed_catalogue():
    """Create any missing categories, items and variants. Returns counts of rows created.

    Existing rows are left exactly as they are, so prices, stock and descriptions that
    staff have edited since are never overwritten.
    """
    created = {"categories": 0, "items": 0, "variants": 0}
    for order, (category_name, description, items) in enumerate(CATALOGUE, start=1):
        category, was_created = Category.objects.get_or_create(
            slug=slugify(category_name),
            defaults={"name": category_name, "description": description, "sort_order": order},
        )
        created["categories"] += was_created
        for item_name, short, variant_names, featured in items:
            item, was_created = Item.objects.get_or_create(
                slug=slugify(item_name),
                defaults={
                    "category": category,
                    "name": item_name,
                    "short_description": short,
                    "base_price": 0,
                    "is_featured": featured,
                    "needs_power": item_name in NEEDS_POWER,
                },
            )
            created["items"] += was_created
            for variant_name in variant_names or ["Default"]:
                _, was_created = ItemVariant.objects.get_or_create(
                    item=item,
                    name=variant_name,
                    defaults={"stock_quantity": 0, "stock_confirmed": False},
                )
                created["variants"] += was_created
    return created
