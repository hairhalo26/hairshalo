# Texture study images

Used by the "Texture study" curl/straight divider on the storefront (`#curlStraight`).

Sources: `curly.png` and `staright.png` (both 1283×1226), supplied by the client.
Provenance and usage licence still need the client's confirmation (the curly
portrait matches the texture-study campaign image).

The two photographs are not pixel-registered: measured on the nose, mouth and
chin, the straight portrait sits 34px higher and 4px further right. Rather than
shift one layer in CSS, both files were cropped to the shared frame so every
layer on the page uses identical framing rules:

| file     | crop box (x0, y0, x1, y1) |
|----------|---------------------------|
| curly    | 0, 34, 1279, 1226         |
| straight | 4, 0, 1283, 1192          |

Result: 1279×1192 each. No scaling, rotation or retouching; the `-800` files
are the same crop resized for phones. After cropping, nose/mouth/chin agree
within ±4px. The eye and jaw differ by up to ~6px because the head pose differs
very slightly between the two images; no crop can remove that without
warping the image, which we don't do.

If the images are replaced, re-measure the offset and re-crop both files the
same way. Don't move one layer with CSS instead.
