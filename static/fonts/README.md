# fonts

Atkinson Hyperlegible Next (variable, latin + latin-ext subsets), by the
Braille Institute. Licensed under the SIL Open Font License 1.1.

Vendored into the image on purpose (plan §8): self-hosting is about not
depending on a third party at render time, not about size. The system font
stack in `templates/base.html` is the fallback (`font-display: swap`), so
pages render correctly if these files fail to load — including saved
offline copies of briefing pages.

Source: Google Fonts (`Atkinson Hyperlegible Next`), woff2 static subsets.
