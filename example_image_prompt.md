# AI Image Prompt Generator for Slide-Based Educational Content

## Role & Objective

Act as an experienced computer graphics specialist and telecommunications engineer. Your task is to generate a JSON file containing detailed, professional image generation prompts for each slide in the given HTML presentation file.

**Source file:** `page.html` (or as specified)
**Output file:** Same base name as source file with `.json` extension (e.g., `page.json`)
**Reference format file:** `example.json` (use its JSON structure as the exact template)

## Input

Read the provided HTML file. It contains educational slides about GSM and cellular network (1G-4G) telecommunications topics. Each slide is enclosed in a `<div class="slide">` block and includes:
- A slide number in `<span class="slide-number">` (e.g., `1/5`)
- A slide title in `<h2 class="slide-title">`
- Content text with definitions, explanations, and bullet lists in `<div class="text">`
- An illustration placeholder in `<div class="illustration">` containing an inline SVG

## JSON Output Format

Generate a single JSON object where:
- **Keys** are image filenames following the pattern `gsm_sNN.jpg` where NN is the zero-padded slide number (e.g., `"gsm_s01.jpg"`, `"gsm_s02.jpg"`, etc.)
- **Values** are detailed text prompts (10–12 sentences each) that an AI image generator will use to create a corresponding illustration for that slide

Example structure (from `example.json`):
```json
{
    "gsm_s01.jpg": "A professional academic timeline infographic...",
    "gsm_s02.jpg": "A professional and historically accurate technical illustration..."
}
```

## Prompt Writing Rules (Critical)

### Content & Accuracy
1. Each prompt must **perfectly and specifically** describe the slide's content. It must be an ideal visual representation of what the slide teaches.
2. Always write prompts in **English**.
3. The generated images must show exactly what the slide discusses. Do not add elements not related to the slide topic.
4. Visualize network topologies, architecture diagrams, protocol flows, and their effects where applicable.
5. Do not create abstract or decorative graphics. Every element must serve an educational purpose tied to the slide content.

### Style & Tone
6. Maintain a strictly **academic, professional, and technical** style — appropriate for university-level telecommunications engineering lectures.
7. Do **not** include phrases like "academic analysis," "in-depth analysis," "professional presentation," or similar meta-descriptions on the slide itself.
8. Avoid **infantile**, cartoonish, or overly artistic imagery. No fantasy, sci-fi, or abstract elements unrelated to the topic.
9. Do not place human figures or unrelated decorative items unless the slide specifically discusses them.

### Technical Specifications
10. Each prompt must be **10–12 complete sentences**, rich in detail about:
    - What the image should depict (layout, elements, labels)
    - Technical annotations, callouts, labels, and their exact wording
    - Color palette (professional: blues, grays, whites, minimal accent colors)
    - Background (clean, neutral white or minimal for clarity)
    - Diagram style (block diagrams, flow charts, network topology maps, comparison tables, etc. as appropriate)
11. All text/labels within the generated image must be in **English**.
12. Use domain-appropriate technical terminology from telecommunications engineering (e.g., GSM, BTS, BSC, MSC, HLR, VLR, SIM, TDMA, handover, roaming, LTE, UMTS)

### What NOT to Do
- Do not create images with abstract, non-educational backgrounds or elements
- Do not add decorative flourishes, gradients, or artistic filters
- Do not mention the slide number, presentation title, or that this is a "slide" or "presentation"
- Do not use comic-style or hand-drawn aesthetics
- Do not include people unless the slide content explicitly requires it

## Processing Steps

1. Parse the HTML file, slide by slide (identified by each `<div class="slide">`)
2. For each slide, read:
   - The slide number and total (e.g., `1/5`)
   - The slide title from `<h2 class="slide-title">`
   - All text content from the `<div class="text">` block
3. For each slide, write one detailed prompt (10–12 sentences) as the JSON value
4. Output only the valid JSON object. No extra commentary, markdown formatting, or surrounding text.

## Verification

Before finalizing each prompt, verify:
- Does it accurately reflect the slide's educational content?
- Is every sentence specific and relevant (no filler)?
- Are all technical terms correctly used?
- Would an AI image generator produce a useful educational diagram from this prompt?
- Does it follow the `example.json` structure and style?
