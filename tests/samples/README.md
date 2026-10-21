# Test samples

Real game screenshots used by the preprocessing, filter and OCR tests.

Put each screenshot here (`.png`) and describe it in `samples.json`:

```json
[
  {
    "file": "gta_night_street.png",
    "region": [0.15, 0.80, 0.70, 0.12],
    "text_colors": ["#FFFFFF"],
    "expected": "You think you can just walk in here?",
    "notes": "white text, bright neon background"
  }
]
```

- `region`: subtitle box as `[x, y, width, height]`, normalized 0–1 to the image size.
- `text_colors`: subtitle colors to mask for.
- `expected`: the exact subtitle text (`""` for frames with no subtitle — tests the filters).

Tests that need Windows OCR are marked `@pytest.mark.ocr` and skipped when the en-US OCR language
pack is missing.
