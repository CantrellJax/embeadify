# Brand assets

Repository-native assets that use no external images or font files, in the same visual language as
[emBEADings](https://github.com/CantrellJax/embeadings):

- `embeadify-mark.svg` — the compact project mark;
- `embeadify-social-card.svg` — editable 1280×640 social-preview source; and
- `embeadify-social-card.png` — rendered social-preview upload (when present).

The mark shows beads strung on a string. Blue and green beads are already in place; the amber bead is
the decision being threaded in. emBEADings' two molecules reach toward an amber near-touch point to say
"look here"; embeadify's amber bead is the next reviewed change landing on the string. Same palette,
same rounded square.

To reproduce the PNG with librsvg:

```bash
rsvg-convert -w 1280 -h 640 \
  assets/brand/embeadify-social-card.svg \
  -o assets/brand/embeadify-social-card.png
```

Keep examples synthetic. Never place tracker text, issue IDs, hostnames, or paths from a real project in
a public brand asset.
