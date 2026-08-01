"""Hallucination-rate check for triage agent natural-language summaries.

Implements the separate hallucination-rate check from §6 of the spec:
cross-reference every factual claim in each summary against the underlying
structured anomaly source data. Uses an LLM-as-judge second pass when
an API key is available, or a keyword-overlap heuristic as fallback.

Reports the fraction of summaries with at least one unverifiable claim.
"""

import logging
import re
from typing import Optional

from detector.models import TriageReport

logger = logging.getLogger(__name__)


class HallucinationChecker:
    """Verifies factual claims in triage summaries against source data."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        from config import config

        self.api_key = api_key or config.deepseek_api_key
        self.model = model or config.triage_model
        self.base_url = config.deepseek_base_url
        self._client = None

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None and self.has_api_key:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def check_report(
        self, report: TriageReport, source_data: dict
    ) -> dict:
        """Check a single triage report for hallucinated claims.

        Uses DeepSeek as LLM judge when available; falls back to heuristic.
        """
        if not report.claims:
            return {
                "total_claims": 0,
                "verifiable": 0,
                "unverifiable": 0,
                "hallucinated": False,
                "details": "No claims extracted from summary",
            }

        if self.has_api_key:
            return self._llm_check(report, source_data)
        else:
            return self._heuristic_check(report, source_data)

    def _llm_check(self, report: TriageReport, source_data: dict) -> dict:
        """Use Claude as judge to verify claims against source data."""
        client = self._get_client()
        if client is None:
            return self._heuristic_check(report, source_data)

        source_json = _format_source_data(source_data)

        prompt = f"""You are a fact-checker. Verify each claim against the source data.

Source data (ground truth from detector):
{source_json}

Summary to verify:
{report.summary}

Claims extracted from summary:
{chr(10).join(f'- {c}' for c in report.claims)}

For each claim, determine if it is VERIFIABLE (supported by the source data) or UNVERIFIABLE (not supported / made up). A claim is unverifiable if:
- It asserts a specific number not in the source data
- It names a cause, actor, or intent not in the source data
- It describes a pattern or trend not evident in the source data
- It makes a comparison that cannot be confirmed

Respond with JSON:
{{"verdicts": [{{"claim": "...", "verdict": "verifiable"|"unverifiable", "reason": "..."}}]}}"""

        try:
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=1024,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.choices[0].message.content

            import json

            match = re.search(r"\{.*\}", text, re.DOTALL)
            result = json.loads(match.group(0)) if match else {"verdicts": []}

            verdicts = result.get("verdicts", [])
            verifiable = sum(
                1 for v in verdicts if v.get("verdict") == "verifiable"
            )
            unverifiable = sum(
                1 for v in verdicts if v.get("verdict") == "unverifiable"
            )

            return {
                "total_claims": len(verdicts),
                "verifiable": verifiable,
                "unverifiable": unverifiable,
                "hallucinated": unverifiable > 0,
                "details": verdicts,
            }
        except Exception as e:
            logger.warning(f"LLM hallucination check failed: {e}, using heuristic")
            return self._heuristic_check(report, source_data)

    def _heuristic_check(self, report: TriageReport, source_data: dict) -> dict:
        """Heuristic claim verification using approximate number matching.

        Only flags claims with specific numbers that can't be found
        (within rounding tolerance) in the source data. Qualitative claims
        without numbers pass by default.
        """
        verifiable = 0
        unverifiable = 0
        details = []

        source_text = str(source_data).lower()

        for claim in report.claims:
            claim_lower = claim.lower()
            numbers = re.findall(r"\d+\.?\d*", claim)

            # Qualitative claims (no numbers) pass
            if not numbers:
                verifiable += 1
                details.append({"claim": claim, "verdict": "verifiable",
                                "reason": "Qualitative claim, no numbers to verify"})
                continue

            # Check for speculative language
            speculative = any(
                w in claim_lower
                for w in ["likely", "probably", "may have", "could be", "might"]
            )

            # Approximate match: at least one number found in source (within rounding)
            numbers_found = False
            for n in numbers:
                val = float(n)
                # Check exact match first, then rounded variants
                for variant in [n, f"{val:.1f}", f"{val:.0f}", str(round(val))]:
                    if variant in source_text:
                        numbers_found = True
                        break
                if numbers_found:
                    break

            verdict = "verifiable" if (numbers_found and not speculative) else "unverifiable"

            if verdict == "verifiable":
                verifiable += 1
            else:
                unverifiable += 1

            details.append({
                "claim": claim,
                "verdict": verdict,
                "reason": (
                    "Numbers match source data" if verdict == "verifiable"
                    else "Numbers not found in source or speculative language detected"
                ),
            })

        return {
            "total_claims": len(report.claims),
            "verifiable": verifiable,
            "unverifiable": unverifiable,
            "hallucinated": unverifiable > 0,
            "details": details,
        }

    def evaluate(
        self, reports: list[TriageReport], source_data: dict
    ) -> dict:
        """Evaluate hallucination rate across all triage reports.

        Returns:
            Dict with hallucination_rate, total_claims_checked, unverifiable_claims
        """
        total_claims = 0
        unverifiable = 0
        hallucinated_reports = 0

        for report in reports:
            result = self.check_report(report, source_data)
            total_claims += result["total_claims"]
            unverifiable += result["unverifiable"]
            if result["hallucinated"]:
                hallucinated_reports += 1

        rate = (
            hallucinated_reports / len(reports) if reports else 0.0
        )

        logger.info(
            f"Hallucination check: {hallucinated_reports}/{len(reports)} reports "
            f"({rate:.1%}) with unverifiable claims. "
            f"{unverifiable}/{total_claims} total claims unverifiable."
        )

        return {
            "hallucination_rate": rate,
            "total_claims_checked": total_claims,
            "unverifiable_claims": unverifiable,
            "reports_checked": len(reports),
            "reports_with_hallucinations": hallucinated_reports,
        }


def _format_source_data(data: dict) -> str:
    """Format source data dict for LLM prompt."""
    import json

    # Truncate large data structures
    formatted = {}
    for k, v in data.items():
        if isinstance(v, list) and len(v) > 50:
            formatted[k] = v[:50]
            formatted[f"{k}_truncated"] = f"... ({len(v)} total items)"
        else:
            formatted[k] = v
    return json.dumps(formatted, indent=2, default=str)
