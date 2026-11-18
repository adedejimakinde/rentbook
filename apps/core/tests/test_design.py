"""Shared design components: hero tabs, icons, the spam trap and the floating button."""

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.bookings.tests.factories import CategoryFactory, ItemFactory, ItemVariantFactory
from apps.core.services import EVENT_TABS, event_tabs

pytestmark = pytest.mark.django_db


def test_hero_tabs_link_to_their_category_when_it_exists():
    chairs = CategoryFactory(name="Chairs", slug="chairs")

    tabs = {tab["key"]: tab for tab in event_tabs([chairs])}

    assert tabs["weddings"]["url"] == chairs.get_absolute_url()
    assert tabs["birthdays"]["url"] == ""
    assert tabs["weddings"]["image"] is None  # no processed photo yet


def test_home_renders_every_event_tab_as_a_real_tab(client):
    html = client.get(reverse("core:home")).content.decode()

    assert html.count('role="tab"') == len(EVENT_TABS)
    assert 'role="tabpanel"' in html
    assert 'aria-selected="true"' in html


def test_icon_tag_renders_decorative_inline_svg():
    html = Template('{% load core_tags %}{% icon "cart" 22 %}').render(Context())

    assert html.startswith("<svg") and 'aria-hidden="true"' in html and 'width="22"' in html


def test_contact_spam_trap_is_hidden_from_people_and_screen_readers(client):
    html = client.get(reverse("core:contact")).content.decode()

    trap = html[html.index('class="hp-field"') - 5 : html.index('id="id_website"') + 40]
    assert 'aria-hidden="true"' in trap and 'tabindex="-1"' in trap


def test_form_pages_do_not_float_the_whatsapp_button(client):
    contact = client.get(reverse("core:contact")).content.decode()
    about = client.get(reverse("core:about")).content.decode()

    assert 'id="fab-wa"' not in contact
    assert 'id="fab-wa"' in about


def test_links_are_underlined_only_in_body_paragraphs(client):
    ItemVariantFactory(item=ItemFactory(name="Canopy", is_featured=True))

    html = client.get(reverse("core:home")).content.decode()

    assert "underline" not in html.split("<main", 1)[1].split("</main>", 1)[0]


def test_privacy_page_is_linked_from_the_footer_and_menu_and_in_the_sitemap(client):
    page = client.get(reverse("core:privacy"))
    html = client.get(reverse("core:about")).content.decode()

    assert page.status_code == 200
    assert "Privacy policy" in page.content.decode()
    assert "Stripe" in page.content.decode()
    assert html.count('href="/privacy/"') == 2  # footer and mobile menu
    assert "/privacy/" in client.get("/sitemap.xml").content.decode()


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("atglaceevents.rental@gmail.com", "atglaceevents.<wbr>rental@<wbr>gmail.com"),
        ("<b>x</b>@a.com", "&lt;b&gt;x&lt;/b&gt;@<wbr>a.com"),
        ("not-an-email", "not-an-email"),
    ],
)
def test_email_breaks_only_after_the_at_sign_or_a_dot_before_it(email, expected):
    html = Template("{% load core_tags %}{{ email|email_breaks }}").render(
        Context({"email": email})
    )

    assert html == expected


def test_quick_check_button_is_shorter_on_narrow_phones(client):
    html = client.get(reverse("core:home")).content.decode()

    assert '<span class="min-[400px]:hidden">Check my date</span>' in html
