---
name: MartinFlix
style: Humanist Product (uiux §10.7), tuned to the masthead artwork
colors:
  forest: '#142723'
  ivory: '#F5E8D0'
  mint: '#8FCFA6'
  light: { bg: '#EEF2EC', surface: '#FBFCF8', fg: '#142723', muted: '#4B5F55', accent: '#2C6A4D', tint: '#DCEBE1' }
  dark:  { bg: '#0E1B18', surface: '#142723', fg: '#F1E8D6', muted: '#A8B9AD', accent: '#8FCFA6', tint: '#1D3A30' }
  states:
    light: { ok: '#1A6B3E', warn: '#8A5A00', crit: '#B42318' }
    dark:  { ok: '#6FD39A', warn: '#E8B341', crit: '#FF8A7A' }
fonts:
  display: Gabarito (variable, self-hosted)
  body: Figtree (variable, self-hosted)
---
# Design System: MartinFlix

## Overview
A calm home cinema control room. The dashboard and the public setup wizard are Operate surfaces: someone who uses admin tools should find everything before they notice the style. The brand comes from the real masthead artwork, a midcentury woodcut house with a projector, drawn in forest, ivory and mint. The dashboard puts real state first.

**Signature move:** the overview reads like a TV guide for your own library. Every monitored series shows its next episode code and air day, and every film shows its next release, such as “Digital · Tue 24 Nov”. Above it, a three-tile status strip is the only alarm: problems are named in the tile, which links to the page that explains them.

## Colors
The navigation rail and wizard header stay forest in both themes. This is the committed shell. The working surface follows the system theme. Light uses a cool sage paper, not cream. Dark uses the masthead’s forest-black, so the banner sits on its own ground. Ivory is for text on forest. Mint is the dark-theme accent and the current-page marker in the rail. State colours always come with a text label. Every pair is checked at 4.5:1 or better.

## Typography
Gabarito sets page and section headings. Figtree sets everything else at 16 px with 1.55 line height. Numbers use tabular figures. Use sentence case everywhere, with no uppercase tracked labels or eyebrows. Monospace is only for commands and paths.

## Layout
- The dashboard has a 232 px rail with Overview, Library, Services and Backups. Add is reached from Library and from empty library lists, and keeps Library highlighted. Each is a real URL. Below 860 px the rail becomes a top bar with icon and label tabs, and the overview’s poster rows scroll sideways.
- The Add page searches through Sonarr’s and Radarr’s lookup and shows TMDB and TheTVDB artwork, which the page’s content security policy allows by host.
- Content sits on panels: 12 px radius, 24 px padding, one soft offset shadow. Use 8 px radius for inputs, buttons and inner cards, and pills for badges.
- The wizard is two columns: the steps on the left and the live commands on the right, which stay in view. Below 900 px it stacks into one column.

## Components
- **Buttons:** solid accent for primary, tinted for secondary, quiet outline for tertiary. 40–44 px tall, icon plus label.
- **Badges:** a pill with a dot and a word (Running, Stopped, Warning).
- **Icons:** one authored SVG sprite at 1.75 stroke, 20 px. Never use emoji or Unicode glyphs as icons.
- **Empty states:** say what goes here and how to fill it.
- **Commands:** one flag per line with shell continuations, plus a copy button that confirms in place.
- **Accessibility:** visible focus rings (3 px), labelled controls, skip link, `prefers-reduced-motion` respected, 44 px touch targets.

## Source of truth
`web/catalog.json` lists every service, its group, role, icon, web port and dependency branch. `server.py` and `web/app.js` read it, and a test checks that it matches `deploy/hosts/martinflix/stacks.conf`. The tokens live at the top of `web/style.css` and `docs/setup.css`; keep the two in step.
