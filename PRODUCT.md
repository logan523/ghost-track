# Product

## Register

product

## Users

Two audiences, equally important:

1. **Hiring managers at defense/tech companies** (Anduril, Palantir, Shield AI) reviewing a portfolio project. They scan for technical depth signaled through design quality and operational realism. They spend 30-60 seconds forming a first impression, then drill into specific features.

2. **Aviation security analysts** using a real monitoring tool. They need fast scanning of large airspace, clear alert hierarchy, and information density that supports split-second decisions. The interface must feel like something you'd trust with real airspace.

Context for both: desktop monitor, possibly dim room, prolonged viewing sessions. The dashboard is glanced at repeatedly, not stared at continuously.

## Product Purpose

Ghost Track monitors live ADS-B aircraft transponder data across contested airspace regions, applies Kalman filter and CUSUM statistical detectors to flag position anomalies and GNSS spoofing, then uses an LLM triage agent to correlate, prioritize, and explain incidents. It demonstrates the architecture pattern (raw sensor data to AI-filtered alerts to human-tasked response) without claiming actuation capability.

Success means: a viewer immediately understands (a) that real data is flowing, (b) that anomalies are being detected and triaged, and (c) that the system is operationally credible.

## Brand Personality

**Precise, calm, commanding.** Like an air traffic control display or a military C2 system: quiet authority, nothing decorative, every pixel is information. When nothing is happening, the system breathes quietly. When an event fires, attention is directed with restraint — no screaming, no panic. Confidence through composure.

## Anti-references

- **Not generic SaaS.** No cookie-cutter admin panels with icon+heading+text cards in identical grids. No blue-and-white data tables. No "dashboard template" look.
- **Not a toy.** No neon-on-black hacker aesthetic, no arcade-style particle effects, no sci-fi movie UIs.
- **Not crowded or noisy.** No Bloomberg-terminal information overload. Breathing room between elements. Hierarchy through scale and position, not through cramming more on screen.

## Design Principles

1. **Information earns its place.** Every element on screen must justify itself. If it's not actionable or context-setting, it doesn't belong. White space is not wasted — it's what makes the information legible.

2. **Quiet until needed.** The baseline state is calm and restrained. Alerts and anomalies use contrast and motion purposefully to pull attention, then release it. The system doesn't cry wolf.

3. **Operational credibility.** The interface should look like it belongs in a 24/7 operations center. Type hierarchy, monospaced data, muted palette, considered layout. No decoration.

4. **Reveal depth gradually.** A newcomer sees a clean map and can understand it in 30 seconds. An expert finds drill-down detail, raw data, and technical context without leaving the surface. Tutorial mode bridges the gap.

5. **Real data, visibly real.** Timestamps update. Aircraft move. Counts increment. The interface telegraphs liveness through subtle motion and fresh data, not through loading spinners.

## Accessibility & Inclusion

- WCAG AA contrast ratios minimum for all text, UI controls, and alert indicators
- Alert severity must be distinguishable by shape/position, not color alone
- Respect `prefers-reduced-motion`: disable animations when set
- Keyboard-navigable alert feed and region controls
