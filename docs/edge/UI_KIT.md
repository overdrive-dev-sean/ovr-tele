# OVR Event UI Kit (Design Spec)

This is a practical handoff spec for branding the event web UI and related assets.

## 1) Layout + Sizing

- Base container (mobile): 360-600 px width, centered
- Desktop container: 960-1200 px max width
- Page padding: 10 px mobile, 20-28 px desktop
- Section spacing: 16-24 px between blocks
- Card spacing: 10-12 px between cards
- Border radius: 8 px (cards), 6 px (inputs), 4 px (buttons)

## 2) Typography

- Primary font: Provide brand font family with weights 400/500/600/700
- Fallbacks: system-ui, -apple-system, Segoe UI, Roboto, sans-serif
- Body size: 16 px
- Label size: 14 px, semi-bold
- Heading size: 20-24 px

## 3) Color Tokens

Provide HEX values for each token:

- Primary: #RRGGBB
- Primary dark: #RRGGBB
- Accent: #RRGGBB
- Success: #RRGGBB
- Warning: #RRGGBB
- Error: #RRGGBB
- Text: #RRGGBB
- Text muted: #RRGGBB
- Surface: #RRGGBB (cards)
- Surface alt: #RRGGBB (secondary panels)
- Border: #RRGGBB
- Background: #RRGGBB

## 4) Logo / Header Asset

Top logo for the header:

- Orientation: horizontal wordmark, transparent background
- Mobile size target: 220-280 px wide, 64-90 px tall
- Desktop size target: 320-420 px wide, 80-110 px tall
- Recommended aspect ratio: 3:1 to 4:1
- Max height: 96 px (mobile), 120 px (desktop)

Deliverables:

- SVG (preferred, with text outlined)
- PNG at 1x, 2x, 3x
- Example PNG widths: 280, 560, 840 px

## 5) Buttons

- Size: 44-48 px height
- Font: 600 weight
- Primary: solid fill, white text
- Secondary: neutral fill or border
- States: hover (darken 6-10%), active (darken 10-15%)
- Focus: 2-3 px outline with 20% alpha of primary color

## 6) Inputs

- Height: 44-48 px
- Border: 1-2 px solid border token
- Focus: primary border + soft shadow
- Placeholder: text muted color

## 7) Cards / Panels

- Background: surface token
- Border: 1 px border token
- Shadow: 0 2px 8px rgba(0,0,0,0.10)

## 8) Icon Set (Optional)

- Grid: 24x24 or 32x32
- Style: single-weight stroke, minimal detail
- Deliverable: SVG

## 9) Brand Consistency Targets

- Strong contrast for readability in bright outdoor light
- Color-coded actions: green=start, red=end, blue=location, purple=notes
- Keep mobile layout unchanged; desktop uses two-column split on the Events tab

## 10) What To Send Back

- Logo assets (SVG + PNG)
- Icon set (SVG)
- Color token list (HEX)
- Typography font files or URLs

