from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Sequence

from rapidfuzz import fuzz, process
from wordfreq import top_n_list, zipf_frequency

from yutome.transcripts import TranscriptSegment

QualityLabel = Literal["ok", "watch", "segment_cleanup_candidate", "transcript_cleanup_candidate"]

NEEDS_CLEANUP_LABELS: frozenset[QualityLabel] = frozenset(
    {"segment_cleanup_candidate", "transcript_cleanup_candidate"}
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{1,}[A-Za-z]")
_FUNCTION_WORDS = frozenset(
    "i me my mine you your yours he him his she her hers it its we our ours they them their theirs "
    "the a an and or but if then so because that this these those to of in on for with by from at as "
    "is are was were be been being do does did have has had not no yes yeah uh um like just right "
    "really very kind sort going gonna wanna can could should would will may might".split()
)
_PREFIXES = ("un", "re", "over", "under", "anti", "non", "pre", "post", "mis", "de", "dis")
_DERIVATIONAL_SUFFIXES = (
    "ish",
    "hood",
    "ness",
    "less",
    "ful",
    "able",
    "ible",
    "ly",
    "wise",
    "like",
    "esque",
    "ize",
    "ise",
)
_FOREIGN_LANGUAGE_GUARD = ("fr", "es", "de", "it", "pt", "tr", "id", "nl")
_TECHNICAL_SUFFIXES = (
    "emia",
    "itis",
    "osis",
    "ase",
    "oma",
    "algia",
    "ergic",
    "genic",
    "oid",
    "phylaxis",
    "cyte",
    "cytic",
)


@dataclass(frozen=True)
class TokenQualityFinding:
    token: str
    score: float
    reasons: tuple[str, ...]
    nearest_common: str | None
    frequency: float


@dataclass(frozen=True)
class SegmentQualityReport:
    sequence: int
    start_ms: int
    end_ms: int
    text: str
    score: float
    token_findings: tuple[TokenQualityFinding, ...]
    pattern_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptQualityReport:
    label: QualityLabel
    segment_count: int
    flagged_segment_count: int
    flagged_density: float
    strong_segment_count: int
    max_segment_score: float
    flagged_segments: tuple[SegmentQualityReport, ...]

    @property
    def needs_cleanup(self) -> bool:
        return self.label in NEEDS_CLEANUP_LABELS


def assess_transcript_quality(
    segments: Sequence[TranscriptSegment],
    *,
    language: str = "en",
    metadata_text: str | None = None,
    max_flagged_segments: int = 12,
) -> TranscriptQualityReport:
    """Estimate whether caption cleanup is likely to improve transcript usefulness.

    This is a triage heuristic, not an autocorrector. It intentionally avoids any
    corpus-specific term list and scores general caption-corruption signals:
    misspelled rare tokens, malformed short OOV tokens, broken local grammar, and
    anomaly density. Proper-name and URL-like contexts are downweighted because
    YouTube transcripts often contain brands, people, places, and sponsor domains.
    """

    flagged: list[SegmentQualityReport] = []
    metadata_tokens = _metadata_tokens(metadata_text)
    segment_reports: list[SegmentQualityReport] = []
    for segment in segments:
        report = assess_segment_quality(segment, language=language, metadata_tokens=metadata_tokens)
        segment_reports.append(report)
        if report.score >= 0.7:
            flagged.append(report)
    flagged = _apply_repeated_variant_boost(
        segments=segments,
        reports=segment_reports,
        flagged=flagged,
        language=language,
        metadata_tokens=metadata_tokens,
    )
    flagged.sort(key=lambda item: item.score, reverse=True)

    segment_count = len(segments)
    flagged_density = len(flagged) / max(1, segment_count)
    strong_segment_count = sum(1 for item in flagged if item.score >= 1.35)
    max_segment_score = flagged[0].score if flagged else 0.0
    if len(flagged) == 1:
        label = "segment_cleanup_candidate" if _single_segment_needs_cleanup(flagged[0]) else "watch"
    elif strong_segment_count >= 3 or flagged_density >= 0.03 or max_segment_score >= 2.0:
        label: QualityLabel = "transcript_cleanup_candidate"
    elif strong_segment_count >= 1 or len(flagged) >= 2 or max_segment_score >= 1.1:
        label = "segment_cleanup_candidate"
    elif flagged:
        label = "watch"
    else:
        label = "ok"
    return TranscriptQualityReport(
        label=label,
        segment_count=segment_count,
        flagged_segment_count=len(flagged),
        flagged_density=flagged_density,
        strong_segment_count=strong_segment_count,
        max_segment_score=max_segment_score,
        flagged_segments=tuple(flagged[:max_flagged_segments]),
    )


def assess_segment_quality(
    segment: TranscriptSegment,
    *,
    language: str = "en",
    metadata_tokens: frozenset[str] | None = None,
) -> SegmentQualityReport:
    foreign_tokens = _foreign_language_tokens(segment.text, primary_language=language)
    token_findings = tuple(
        finding
        for token in _tokens(segment.text)
        if (
            finding := _token_finding(
                token,
                segment.text,
                language=language,
                foreign_tokens=foreign_tokens,
                metadata_tokens=metadata_tokens or frozenset(),
            )
        )
        is not None
    )
    pattern_score, pattern_reasons = _pattern_score(segment.text)
    score = pattern_score + sum(finding.score for finding in token_findings)
    return SegmentQualityReport(
        sequence=segment.sequence,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        score=score,
        token_findings=token_findings,
        pattern_reasons=pattern_reasons,
    )


def _tokens(text: str) -> list[str]:
    return [match.group(0) for match in _WORD_RE.finditer(text)]


def _token_finding(
    token: str,
    text: str,
    *,
    language: str,
    foreign_tokens: frozenset[str],
    metadata_tokens: frozenset[str],
) -> TokenQualityFinding | None:
    raw = token.strip("'-")
    lower = raw.lower()
    if len(lower) < 4 or not lower.isalpha() or lower in _FUNCTION_WORDS:
        return None
    frequency = _frequency(lower, language)
    nearest, similarity, nearest_frequency = _nearest_common(lower, language=language)
    weird_shape = _weird_shape(lower)
    score = 0.0
    reasons: list[str] = []

    if frequency == 0:
        if len(lower) <= 6:
            score += 0.72
            reasons.append("short_oov")
        else:
            score += 0.35
            reasons.append("oov")
    elif frequency < 1.5:
        score += 0.55
        reasons.append(f"rare:{frequency:.1f}")

    # Edit-distance evidence only matters for tokens that are genuinely rare or OOV.
    # Otherwise normal inflections such as "hitchhiked" near "hitchhike" become noisy.
    if frequency < 2.0:
        if nearest and similarity >= 94 and nearest_frequency >= 1.8:
            score += 0.9
            reasons.append(f"near:{nearest}:{similarity:.0f}")
        elif nearest and similarity >= 87 and nearest_frequency >= 2.3:
            score += max(0.4, min(0.85, (similarity - 84) / 18))
            reasons.append(f"near:{nearest}:{similarity:.0f}")
        elif nearest and similarity >= 86 and nearest_frequency >= 3.5 and frequency == 0 and len(lower) <= 7:
            score += 0.25
            reasons.append(f"weak_near:{nearest}:{similarity:.0f}")

    if weird_shape:
        score += 0.35
        reasons.append("shape")
    if _inflectional_neighbor(lower, nearest) and frequency >= 1.4 and score < 1.5:
        score *= 0.12
        reasons.append("inflection_guard")
    if _technical_term_guard(lower) and nearest and similarity >= 87 and score < 1.1:
        score = 1.1
        reasons.append("technical_near_miss")
    if _technical_term_guard(lower) and score < 1.2 and not (nearest and similarity >= 87):
        score *= 0.25
        reasons.append("technical_guard")
    if _plausible_morphology(lower, language=language) and score < 1.7:
        score *= 0.12
        reasons.append("morph_guard")
    if lower in foreign_tokens and score < 1.5:
        score *= 0.2
        reasons.append("language_switch_guard")
    guard, guard_reason = _context_guard(raw, lower, text, frequency, metadata_tokens)
    if guard_reason and score < 1.5:
        score *= guard
        reasons.append(guard_reason)

    if score < 0.7:
        return None
    return TokenQualityFinding(
        token=lower,
        score=score,
        reasons=tuple(reasons),
        nearest_common=nearest,
        frequency=frequency,
    )


def _pattern_score(text: str) -> tuple[float, tuple[str, ...]]:
    lower = f" {text.lower()} "
    score = 0.0
    reasons: list[str] = []
    if re.search(r"\b(i|you|he|she|we|they|it)\s+\1\b", lower):
        score += 0.15
        reasons.append("duplicate_pronoun")
    if re.search(r"\b(the|a|an|to|of|in|on|for|with|and|or|but|that|this|it|is|are|was|were)\s+\1\b", lower):
        score += 0.2
        reasons.append("duplicate_function")
    if re.search(r"\b\w+\s+(?:is|are|was|were|be|being)\s+\w{4,}ing\s+from\b", lower):
        score += 0.7
        reasons.append("broken_passive")
    if re.search(r"\b[a-z]\s+(?:it|i|you|he|she|we|they|there|this|that)\b", lower):
        score += 0.2
        reasons.append("letter_fragment")
    return score, tuple(reasons)


def _context_guard(
    raw: str,
    lower: str,
    text: str,
    frequency: float,
    metadata_tokens: frozenset[str],
) -> tuple[float, str | None]:
    lower_text = text.lower()
    if ".com" in lower_text or "://" in lower_text or "/" in text:
        return 0.35, "urlish_guard"
    if lower in metadata_tokens or any(len(lower) >= 5 and lower in token for token in metadata_tokens):
        return 0.2, "metadata_guard"
    if any(character.isupper() for character in raw[1:]) or (raw[:1].isupper() and frequency < 2.4):
        return 0.35, "properish_guard"
    return 1.0, None


def _weird_shape(token: str) -> bool:
    return bool(
        re.search(r"(.)\1\1", token)
        or re.search(r"[bcdfghjklmnpqrstvwxyz]{5,}", token)
        or re.search(r"[aeiou]{4,}", token)
    )


def _plausible_morphology(token: str, *, language: str) -> bool:
    if _frequency(token, language) >= 2.15:
        return True
    for prefix in _PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 4:
            rest = token[len(prefix) :]
            if _frequency(rest, language) >= 2.4 or _plausible_suffix(rest, language=language):
                return True
    return _plausible_suffix(token, language=language)


def _plausible_suffix(token: str, *, language: str) -> bool:
    for suffix in _DERIVATIONAL_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            stem = token[: -len(suffix)]
            if _frequency(stem, language) >= 2.4 or _frequency(f"{stem}e", language) >= 2.4:
                return True
    if token.endswith("iest") and len(token) > 7:
        stem = token[:-4]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}y", language) >= 2.1
    if token.endswith("ier") and len(token) > 6:
        stem = token[:-3]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}y", language) >= 2.1
    if token.endswith("est") and len(token) > 6:
        stem = token[:-3]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}e", language) >= 2.1
    if token.endswith("y") and len(token) > 5:
        stem = token[:-1]
        return _frequency(stem, language) >= 2.3 or _frequency(f"{stem}e", language) >= 2.3
    if token.endswith("ing") and len(token) > 7:
        stem = token[:-3]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}e", language) >= 1.5
    if token.endswith("ed") and len(token) > 6:
        stem = token[:-2]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}e", language) >= 2.1
    if token.endswith("ers") and len(token) > 7:
        return _frequency(token[:-1], language) >= 2.1
    if token.endswith("es") and len(token) > 6:
        stem = token[:-2]
        return _frequency(stem, language) >= 2.1 or _frequency(f"{stem}e", language) >= 2.1
    if token.endswith("s") and len(token) > 6:
        return _frequency(token[:-1], language) >= 1.7
    return False


def _metadata_tokens(metadata_text: str | None) -> frozenset[str]:
    if not metadata_text:
        return frozenset()
    return frozenset(
        token.lower()
        for token in _tokens(metadata_text)
        if len(token) >= 4 and token.lower() not in _FUNCTION_WORDS
    )


def _foreign_language_tokens(text: str, *, primary_language: str) -> frozenset[str]:
    candidates: list[str] = []
    for token in _tokens(text):
        lower = token.lower()
        if len(lower) < 4 or not lower.isalpha() or lower in _FUNCTION_WORDS:
            continue
        primary = _frequency(lower, primary_language)
        foreign = max(
            (_frequency(lower, language) for language in _FOREIGN_LANGUAGE_GUARD if language != primary_language),
            default=0.0,
        )
        if foreign >= 2.5 and foreign > primary + 0.8:
            candidates.append(lower)
    if len(set(candidates)) < 2:
        return frozenset()
    return frozenset(candidates)


def _technical_term_guard(token: str) -> bool:
    return len(token) >= 9 and any(token.endswith(suffix) for suffix in _TECHNICAL_SUFFIXES)


def _apply_repeated_variant_boost(
    *,
    segments: Sequence[TranscriptSegment],
    reports: list[SegmentQualityReport],
    flagged: list[SegmentQualityReport],
    language: str,
    metadata_tokens: frozenset[str],
) -> list[SegmentQualityReport]:
    occurrences: dict[str, list[int]] = defaultdict(list)
    for index, segment in enumerate(segments):
        for token in _tokens(segment.text):
            lower = token.lower()
            if len(lower) < 5 or not lower.isalpha() or lower in _FUNCTION_WORDS:
                continue
            if _metadata_supported(lower, metadata_tokens):
                continue
            if _frequency(lower, language) >= 2.0 or _plausible_morphology(lower, language=language):
                continue
            if lower in _foreign_language_tokens(segment.text, primary_language=language):
                continue
            occurrences[lower].append(index)
    tokens = sorted(occurrences)
    clusters: list[set[str]] = []
    used: set[str] = set()
    for token in tokens:
        if token in used:
            continue
        cluster = {token}
        for other in tokens:
            if other == token or other in used:
                continue
            if abs(len(token) - len(other)) > 5:
                continue
            if fuzz.ratio(token, other) >= 80 or fuzz.partial_ratio(token, other) >= 92:
                cluster.add(other)
        if len(cluster) >= 3:
            if len({_morph_family_key(item, language=language) for item in cluster}) == 1:
                used.update(cluster)
                continue
            clusters.append(cluster)
            used.update(cluster)
    if not clusters:
        return flagged

    report_by_sequence = {report.sequence: report for report in reports}
    flagged_by_sequence = {report.sequence: report for report in flagged}
    for cluster in clusters:
        affected_indexes = sorted({index for token in cluster for index in occurrences[token]})
        if len(affected_indexes) < 3:
            continue
        boost = min(1.2, 0.65 + (len(cluster) * 0.12))
        for index in affected_indexes:
            segment = segments[index]
            current = flagged_by_sequence.get(segment.sequence) or report_by_sequence[segment.sequence]
            finding = TokenQualityFinding(
                token="/".join(sorted(cluster)[:4]),
                score=boost,
                reasons=(f"variant_cluster:{len(cluster)}",),
                nearest_common=None,
                frequency=0.0,
            )
            boosted = SegmentQualityReport(
                sequence=current.sequence,
                start_ms=current.start_ms,
                end_ms=current.end_ms,
                text=current.text,
                score=current.score + boost,
                token_findings=current.token_findings + (finding,),
                pattern_reasons=current.pattern_reasons,
            )
            flagged_by_sequence[segment.sequence] = boosted
    return list(flagged_by_sequence.values())


def _metadata_supported(token: str, metadata_tokens: frozenset[str]) -> bool:
    return token in metadata_tokens or any(len(token) >= 5 and token in metadata_token for metadata_token in metadata_tokens)


def _single_segment_needs_cleanup(segment: SegmentQualityReport) -> bool:
    if segment.score >= 2.0:
        return True
    for finding in segment.token_findings:
        if "technical_near_miss" in finding.reasons:
            return True
        if finding.score >= 1.1 and any(reason.startswith("near:") for reason in finding.reasons):
            return True
        if any(reason.startswith("variant_cluster") for reason in finding.reasons):
            return True
    return False


def _inflectional_neighbor(token: str, nearest: str | None) -> bool:
    if not nearest:
        return False
    variants = {
        f"{token}s",
        f"{token}es",
        f"{token}d",
        f"{token}ed",
        f"{token}r",
        f"{token}rs",
    }
    reverse_variants = {
        f"{nearest}s",
        f"{nearest}es",
        f"{nearest}d",
        f"{nearest}ed",
        f"{nearest}r",
        f"{nearest}rs",
    }
    return nearest in variants or token in reverse_variants


def _morph_family_key(token: str, *, language: str) -> str:
    if token.endswith("ings") and len(token) > 8 and _frequency(token[:-1], language) >= 1.4:
        return _morph_family_key(token[:-1], language=language)
    if token.endswith("ing") and len(token) > 7:
        stem = token[:-3]
        if _frequency(f"{stem}e", language) >= 1.4:
            return f"{stem}e"
        if _frequency(stem, language) >= 2.1:
            return stem
    if token.endswith("ed") and len(token) > 6:
        stem = token[:-2]
        if _frequency(f"{stem}e", language) >= 1.4:
            return f"{stem}e"
        if _frequency(stem, language) >= 2.1:
            return stem
    if token.endswith("s") and len(token) > 6 and _frequency(token[:-1], language) >= 1.4:
        return _morph_family_key(token[:-1], language=language)
    return token


@lru_cache(maxsize=50_000)
def _nearest_common(token: str, *, language: str) -> tuple[str | None, float, float]:
    choices = []
    buckets, _ = _dictionary(language)
    for length_delta in range(-4, 5):
        choices.extend(buckets.get((len(token) + length_delta, token[0]), []))
    if not choices:
        return None, 0.0, 0.0
    for word, similarity, _ in process.extract(token, choices, scorer=fuzz.ratio, limit=8):
        if word != token:
            return word, float(similarity), _frequency(word, language)
    return None, 0.0, 0.0


@lru_cache(maxsize=20_000)
def _frequency(token: str, language: str) -> float:
    return zipf_frequency(token, language)


@lru_cache(maxsize=8)
def _dictionary(language: str) -> tuple[dict[tuple[int, str], list[str]], frozenset[str]]:
    words = [word for word in top_n_list(language, 200_000) if 3 <= len(word) <= 22 and word.isalpha()]
    buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for word in words:
        buckets[(len(word), word[0])].append(word)
    return buckets, frozenset(words)
