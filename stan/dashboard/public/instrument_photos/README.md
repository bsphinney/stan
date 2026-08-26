# Instrument photos for the arcade games

Drop image files here and the arcade games can use them. Name them by
instrument, lowercase, no spaces:

    astral.jpg        exploris.jpg      evosep.jpg
    timstof.jpg       lumos.jpg         waters.jpg
    ltq.jpg

Any of .jpg / .png / .webp works. A few hundred pixels on the long edge is
plenty — these render at tile size (~64-96 px) in Mass Match and at tower
size in Core Defense, so anything larger is wasted bytes.

## Why they get embedded rather than linked

The games must stay self-contained: no `<img src="http...">`, no CDN. The
dashboard may be served behind SSO with a strict Content-Security-Policy,
and an external reference renders the game blank. So the build step
base64-encodes each file into a `data:` URI inside the HTML. Keep them small
for that reason — a 2 MB photo becomes ~2.7 MB of base64 in the page.

## Where to get them

Best: **photograph the instruments in the core.** They're yours, no
permission needed, and a real photo of the actual timsTOF beats a stock
render. A phone photo against a plain background, cropped square, is ideal.

Otherwise: ask the vendor's marketing/press contact for product images and
permission to use them. They generally want their hardware shown, and a
short email gets you high-res assets plus a clear license. Do not scrape
product pages — the photograph is the vendor's copyrighted work even though
the instrument itself is just an object.
