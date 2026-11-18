# Atglace

The design system for Atglace Events & Rentals LLC, a party and event rental company serving the Dallas-Fort Worth area. Tagline: "Rent. Celebrate. Make memories." Positioning: "Your one-stop shop for event rentals."

## Voice

Warm, clear and local. We talk like a friendly DFW crew that shows up on time: short sentences, real details (delivery, setup, sandbags and stakes, power outlets), no hype words. Prices and rules are stated plainly. Examples: "We deliver, set up and pick up." "Pick your date and see what's free." "Balance due 7 days before your event."

## Visual foundations

- Ground: `surface-soft` for pages, `surface` for cards. Lots of white; colour is used with intent.
- Colour weight: 60% white and soft surfaces, 30% `brand` and `brand-deep`, 10% `accent` for the one primary action per screen. `accent-bright` and `gold` are decoration only.
- One primary action per screen in `accent` (Book now, Check availability, Pay). Secondary actions are `brand` outline or text buttons.
- Type: Montserrat only, weights 500, 600 and 700. Display and headings 700, body 500. Numbers use tabular figures.
- Shapes are soft and friendly: pills for buttons, chips and badges; `radius-md` for cards; `radius-lg` for hero media and sections.
- Depth: `shadow-card` for floating cards, `shadow-lift` on hover. No other shadows.
- Never: gradient washes, emoji as icons, underlined links outside body copy, left-border accent cards, gold or hot pink text on white.

## Iconography

Inline stroke icons, 2px stroke, round caps and joins, 20 to 24px, coloured with the text they sit beside. Icons sit in a 34 to 40px `surface-tint` tile (or a status tint) when they lead a row. The brand sparkle (four-point star) is used once per section at most, in `accent-bright`.

## Components (patterns)

- **Button:** pill, 44 to 48px tall (48px for the main action), 15px 700 label. Primary `accent` with white text; secondary 2px `brand` outline; ghost `brand` text with a chevron.
- **Chip / filter:** pill, 36 to 38px tall, `border-strong` outline at rest, `brand` fill with white text when active.
- **Badge:** pill, 11 to 12px 700, status colour on its tint (success, warning, danger). "Popular" uses `gold` background with `ink` text.
- **Product card:** `surface`, `radius-md`, square image on phone (4:3 on desktop) on top, 12 to 14px padding with the badge over its top-left corner, name (h3), one-line description in `ink-muted`, price (`price`) and availability dot. Lifts with `shadow-lift` on hover.
- **Section header:** eyebrow in `accent` uppercase, h2 in `ink`, optional ghost link on the right.
- **Row list:** 60px minimum rows divided by `border` hairlines, leading icon tile, chevron at the end.
- **Switch:** 50 by 30px pill, `brand` when on, `border-strong` tone when off, real `role="switch"`.
- **Form field:** label above (13px 700), 46px input with 16px text (so iPhones do not zoom), `radius-sm`, `border-strong` 1.5px, focus ring 3px `brand-bright` at 40%.
- **Header:** white, logo left, cart and Book now right, a 3px `gold` rule under it.
- **Footer:** `brand-deep`, white logo, link columns without underlines, service-area cities, social icons, contact.
- **Floating WhatsApp:** 56px `whatsapp` circle, bottom right, never overlapping prices or inputs: pages with a sticky bar lift it above the bar.

## Layout and density

Compact and refined, like a modern e-commerce app, never like an oversized template site.
- Fluid, never fixed: layouts use %, fr, minmax and max-width containers. Designs drawn at 390px are a reference, not a fixed width.
- Type scales with the screen using clamp(): hero headline 26 to 32px on phone and up to 44px on desktop, page titles 22 to 26px, section titles 19 to 22px, body 15px, captions 12 to 13px.
- Spacing comes only from the spacing tokens. Sections are 40px apart on phone and 64px on desktop; cards use 12 to 14px padding and 12px gaps.
- Side padding 16px on phone, 24px on tablet, 32px on desktop; content max-width 1200px.
- Hero media is at most 45% of the phone screen height (520px on desktop), so the next section always shows on first load. No section is ever full screen height.
- Grids: product cards 1 column under 340px, 2 on phones, 3 on tablets, 4 on desktop.
- Every page works at 320, 360, 375, 390, 412 and 430px phones, 768 and 1024px tablets and 1280px desktop, with no sideways scrolling, overlap or clipped text. Sticky bars respect iPhone safe areas.

## Motion

Motion is quick, light and optional:
- Scroll reveal: elements fade in and rise 16px over 500ms with ease-out, staggered 60ms, once.
- Shelves: CSS scroll-snap rows where the next card peeks in from the edge, with a slim progress line that fills as you swipe and arrow buttons on desktop. No dots.
- Hero: labelled tabs (Birthdays, Weddings, Church and school, Concessions) swap the hero content when tapped. Nothing rotates on its own, so nothing needs a pause button.
- Hover: cards lift 4px with `shadow-lift`, images scale 1.04 inside their frame.
- Marquee strips (cities served, reviews) scroll slowly and pause on hover.
- Everything respects `prefers-reduced-motion`: motion is removed, content stays.
- Budget: no animation libraries; CSS plus a few lines of Alpine or vanilla JS.
