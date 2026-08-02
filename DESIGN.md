# Ghost Track — Design System

Source of truth for UI. Complements PRODUCT.md (brand/users) and eng Phase 0+1 plan.

**Personality:** Precise, calm, commanding. ATC / C2 ops glass. Quiet until needed.

**Classifier:** App UI (workspace map + alert feed). Not marketing.

---

## Information hierarchy (30s scan)

1. **Posture** — labeled threat segments + data mode (LIVE / SYNTHETIC / DEGRADED) + last update age  
2. **Outliers** — map with C2 chevrons, legend, optional layers  
3. **Next action** — alert feed with `recommended_action` visible collapsed  

Constraint worship: if only three things fit, these three win.

```
┌─ THREAT [BALTIC WATCH] [E.MED QUIET] ──── last poll 4s ── mode:LIVE ─┐
│ MAP workspace                          │ SIDEBAR                      │
│  chevrons + legend                     │ Ghost Track · stats          │
│  trails / selection                    │ region                       │
│  [detail dock when selected]           │ action-first alerts          │
│                                        │ evidence chips on expand     │
└────────────────────────────────────────┴──────────────────────────────┘
```

## Emotional arc

| Time | User | Feel | UI supports |
|------|------|------|-------------|
| 5s | Lands | Calm trust | Quiet map, muted chrome, no neon |
| 30s | Scans | Credible ops | Posture labels, action on card, F1 footer |
| 5min | Drills | Depth without leaving | Detail: kinematics + chips + evidence |

## Color (CSS variables — keep existing oklch system)

| Token | Role |
|-------|------|
| `--bg-root` / `--bg-surface` / `--bg-raised` | Surface stack |
| `--text-primary` / `--secondary` / `--muted` | Type (muted must pass AA on surfaces for labels ≥12px; bump if needed) |
| `--accent` (amber steel) | Selection, primary control, medium severity |
| `--red` | Flagged / critical (always paired with **shape**: ring) |
| `--green` | Quiet / healthy |
| `--amber` | Elevated / watch |

No purple gradients. No decorative glow as primary severity signal.

## Typography

| Role | Face | Notes |
|------|------|-------|
| UI / body | **IBM Plex Sans** (or Inter only if Plex unavailable) | Not system-ui as primary |
| Data / ICAO / scores | **IBM Plex Mono** or **JetBrains Mono** | Tabular nums for GS, times |

Minimum body for dense ops: 13px mono data OK; UI chrome labels ≥12px with AA contrast. Prefer `--text-muted` lightened to ~55% L if AA fails.

## Map symbology (aircraft)

| State | Glyph | Size |
|-------|-------|------|
| Clean | Single-path chevron, muted amber/steel fill, 1px outline | ~16–20px |
| Flagged | Filled + **ring** (shape, not color alone) | ~20–24px |
| Selected | Gold ring / bracket, higher z-index | as flagged |

- Heading = rotation; nose = track  
- No engines, windows, ground shadow  
- Hover tooltip: callsign · ICAO · alt · status  
- Legend: three states always visible when map has traffic  

**Motion:** trail interpolation OK; **no** pulsing red drop-shadow on flagged (use ring). Respect `prefers-reduced-motion`.

## Alert cards

Order:
1. Severity shape+number · time · region chip  
2. **recommended_action** (primary line, accent if sev≥4)  
3. Summary max 2 lines  
4. GS · aircraft IDs  
5. Expand: full summary, evidence chips, actions (zoom / handoff later)  

Structure: outer `article` or `div` with role; **no nested buttons**. Action row = sibling buttons.

## Evidence chips

Mono compact chips:

| Chip | Meaning |
|------|---------|
| `WEATHER` | SIGMET/METAR context |
| `XCHECK` | Second ADS-B corroboration |
| `JAM` | Interference zone / baseline |
| `ID` | Registration / type / operator |

States:
- **ok** — subtle accent border, readable text  
- **warn** — amber border  
- **unavailable** — muted, dashed border, label still present  

Never hide the chip row silently when fetch fails.

## Interaction states (user-visible)

| Feature | Loading | Empty | Error | Success | Partial |
|---------|---------|-------|-------|---------|---------|
| Map | dim controls, status “connecting” | “No tracks in window” mono | banner DEGRADED | markers move | stale tracks desaturated |
| Feed | skeleton 2 cards | “Detectors idle · 0 flags · next poll ~Ns” | toast + keep last | cards | “No alerts above GS filter” |
| Detail | “Loading enrich…” on chips | closed | chip unavailable | full kinematics | ID pending |
| Threat bar | gray segments | all quiet labeled | “—” region offline | labeled levels | one region missing |
| Mode banner | connecting | — | DEGRADED reason | LIVE / SYNTHETIC | mixed → force DEGRADED label |

## Threat bar

- Height ≥ 20px clickable row (not 4px only)  
- Text: `BALTIC · WATCH`  
- Click → sets region filter  
- Color + text + position (not color alone)  

## Data mode banner

Persistent strip under header or over map corner:

- `LIVE` green subtle  
- `SYNTHETIC DEMO` amber  
- `DEGRADED` red text + short reason  

Never show synthetic fleet with LIVE styling.

## Units

- Altitude: FL when ≥1000m else ft (show unit)  
- Speed: kts primary  
- Heading: ° true  
- Age: `12s` / `2m`  

## Layout

- Desktop primary: map flex 1 + sidebar 380px  
- Detail: dock over map bottom or sidebar bottom  
- Mobile (deferred polish): map full; sidebar as bottom sheet  

## A11y (Phase 1 hard requirements)

- WCAG AA text/controls  
- Severity: number badge + ring/shape  
- Keyboard: tab feed, Enter expand, Escape close detail, region select native  
- Focus-visible accent ring  
- Touch targets ≥44px on primary controls  
- `prefers-reduced-motion`: no pulse animations  
- ARIA: live regions for status; map `role="application"` ok with keyboard path to list  

## Anti-patterns (reject)

- Toy multi-path plane SVG  
- Purple/SaaS card grids  
- Nested interactive buttons  
- Color-only threat strip  
- Client-side fake Ghost Score display  
- Placeholder-as-only-label  
- Emoji decoration  
- “Unlock the power of…” copy  

## Components vocabulary

| Component | Use |
|-----------|-----|
| `map-ctrl` | Map toggles |
| `report-card` | Alert (article) |
| `sev` / `gs` | Severity & ghost score badges |
| `chip` | Evidence chips |
| `stat` | Header counts |
| `detail-stat` | Detail grid |
| `mode-banner` | LIVE/SYNTHETIC/DEGRADED |
| `threat-row` | Labeled threat segments |
| `legend` | Map symbol key |

## Portfolio footer

Quiet mono line: Phase-1 detector F1 / precision / hallucination rate from README (static). Builds hiring-manager trust without shouting.
