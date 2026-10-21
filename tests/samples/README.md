# Test samples

Real screenshots used by the preprocessing, filter and OCR tests, described in `samples.json`.

**The images are not committed** (`*.png` is git-ignored): they are game footage and the repository
is public. Tests whose image is missing are skipped, so CI runs without them. To run the full set,
put the screenshots listed in `samples.json` into this folder.

`samples.json`:

```json
{
  "defaults": {
    "region": [0.12, 0.86, 0.76, 0.135],
    "vision": {"text_colors": ["#FFFFFF"], "outline_support": true}
  },
  "samples": [
    {
      "file": "Ekran görüntüsü 2026-10-21 193903.png",
      "expected": "Boobie, huh? Yeah, he ain’t here, though.",
      "reference_tr": "Boobie mi? Hı, ama burada değil.",
      "notes": "dark alley, ceiling lights"
    }
  ]
}
```

- `region`: subtitle area as `[x, y, width, height]`, normalized 0–1 to the image (like a profile).
- `vision`: `VisionSettings` overrides (a profile's vision settings).
- Each sample may override `region` and `vision`.
- `expected`: the subtitle exactly as shown (`""` = no subtitle: the frame must yield no text).
- `reference_tr`: a human Turkish translation, used to compare models.

File names may contain non-ASCII characters: load them with `np.fromfile` + `cv2.imdecode`
(`cv2.imread` cannot open such paths on Windows).
