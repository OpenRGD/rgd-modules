"""Registry maintainer's EXT-01B review bridge (candidate only).

Requires a separately reviewed, exact pinned Core candidate on the caller's
trusted PYTHONPATH. No import of module-provider code. This is NOT part of
rgd_registry's runtime catalog/install interface. No approval or mutation.
"""
from __future__ import annotations
from rgd_registry import core as legacy
from proposal import ProposalError, parse, propose, encode_proposal

class ReviewError(ValueError):
    """Fixed rejection code, not an authority decision."""

def review_publication(manifest_raw, descriptor_bytes, artifact_bytes,
                       source_claim, submitted_proposal_raw):
    """Recompute every commitment from supplied bytes; check current legacy key.

    The caller must obtain bytes from an independently reviewed checkout and
    artifacts. No source/publisher/format verification is claimed here. Existing
    v0.1 releases cannot be replaced by an incomparable new-format proposal.
    This is read-only review, not a concurrency-safe publication transaction.
    """
    try:
        expected = propose(manifest_raw, descriptor_bytes, artifact_bytes, source_claim)
        submitted = parse(submitted_proposal_raw)
        if encode_proposal(submitted) != encode_proposal(expected):
            raise ReviewError("PROPOSAL_COMMITMENT_MISMATCH")
    except ProposalError as exc:
        raise ReviewError("INVALID_PROPOSAL_INPUT") from exc
    try:
        catalog = legacy.catalog()
    except legacy.RegistryError as exc:
        raise ReviewError("LEGACY_CATALOG_INVALID") from exc
    key = (expected["module_id"],expected["module_version"])
    if any((d["id"],d["version"]) == key for d in catalog):
        # No raw manifest/artifact commitments in the v0.1 descriptor with
        # which to compare. Never treat a matching ID or source pin as equal.
        raise ReviewError("LEGACY_RELEASE_KEY_CONFLICT")
    return {
        "review_status":"review_required",
        "legacy_key_status":"absent_from_reviewed_v0_1_catalog",
        "proposal":expected,
        "publisher_verified":False,
        "registry_publication_committed":False,
        "installable":False,
        "grants_authority":False,
    }
