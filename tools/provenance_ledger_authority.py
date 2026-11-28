"""Opt-in EXT-01B.3D candidate reservation adapter.

No production admission or changes to existing TUF / ledger contracts.
Trusted local operator provisions policy, repository, trust cache and ledger.
"""
from copy import deepcopy
from pathlib import Path

from tuf.api.metadata import Root, Targets
from proposal import digest, encode_proposal, parse
from ledger_contract import decode
from provenance import verify_provenance, policy_from_bytes, ProvenanceError
from tools.review_trusted_candidate import cached_metadata, ed25519_material, TrustError
from tools.tuf_ledger_authority import tuf_reservation_authorizer
from tools.release_ledger import LedgerError

def provenance_reservation_authorizer(*,catalog_id,ledger_id,manifest_raw,descriptor_bytes,
        artifact_bytes,source_claim,filename,namespace,metadata_files,cache_dir,bootstrap,
        attestation_raw,policy_raw,local_repository):
    """Return owner-provisioned reservation callback, not a public API.

    Recompute provenance before TUF verification, then verify independent key
    material and persist both evidence commitments in the existing transaction.
    Exact retry returns historical receipt without invoking this callback.
    """
    descriptors=deepcopy(descriptor_bytes)
    artifacts=deepcopy(artifact_bytes)
    metadata=deepcopy(metadata_files)
    cache=Path(cache_dir)
    policy=policy_from_bytes(policy_raw)
    tuf=tuf_reservation_authorizer(catalog_id=catalog_id,ledger_id=ledger_id,
            manifest_raw=manifest_raw,descriptor_bytes=descriptors,artifact_bytes=artifacts,
            source_claim=source_claim,filename=filename,namespace=namespace,
            metadata_files=metadata,cache_dir=cache,bootstrap=bootstrap)
    def authorize(request_raw,proposal_raw,evidence_raw,prior_receipt_raw):
        req=decode("ReleaseLedgerRequest",request_raw)
        if ((req["catalog_id"],req["ledger_id"])!=(catalog_id,ledger_id)
            or req["operation"]!="RESERVE_CANDIDATE" or prior_receipt_raw is not None):
            raise LedgerError("PROVENANCE_RESERVATION_SCOPE_ONLY")
        try:
            report=verify_provenance(manifest_raw=manifest_raw,descriptor_bytes=descriptors,
                    artifact_bytes=artifacts,source_claim=source_claim,
                    submitted_proposal_raw=proposal_raw,filename=filename,
                    attestation_raw=attestation_raw,policy_raw=policy_raw,
                    local_repository=local_repository)
        except ProvenanceError as e:
            raise LedgerError("BUILD_PROVENANCE_REJECTED") from e
        if (report["proposal_sha256"]!=req["proposal_sha256"] or
            any(report[k]!=req[k] for k in ("module_id","module_version"))):
            raise LedgerError("PROVENANCE_BINDING_MISMATCH")
        # Actual prior adapter refreshes/verifies TUF and wheel/legacy review.
        # A failure after refresh may advance protected cache but not ledger.
        ctx_raw=tuf(request_raw,proposal_raw,evidence_raw,prior_receipt_raw)
        try:
            root=cached_metadata(cache,"root",Root).signed
            top=cached_metadata(cache,"targets",Targets).signed
            roles=top.delegations.roles
            # Same local protected cache and single writer context. No
            # organizational independence or hostile concurrent cache proof.
            materials=set()
            for name in ("root","timestamp","snapshot","targets"):
                materials.update(ed25519_material(root.keys,root.roles[name]))
            materials.update(ed25519_material(top.delegations.keys,roles["publisher-"+namespace]))
            if policy["builder_public_key"] in materials:
                raise LedgerError("BUILDER_ROLE_KEY_REUSE")
        except (TrustError,OSError,KeyError,AttributeError,TypeError,ValueError) as e:
            if isinstance(e,LedgerError):raise
            raise LedgerError("BUILDER_ROLE_SEPARATION_UNVERIFIED") from e
        return encode_proposal({"profile":"TUF_AND_BUILD_PROVENANCE_REVIEW_CANDIDATE",
            "request_sha256":digest(request_raw),
            "review_target_sha256":digest(evidence_raw),
            "tuf_review_context":parse(ctx_raw),
            "provenance_review":report,
            "provenance_review_sha256":digest(encode_proposal(report)),
            "builder_policy_sha256":digest(policy_raw),"attestation_sha256":digest(attestation_raw),
            "builder_key_separation_verified":True,
            "publication_committed":False,"installable":False,"grants_authority":False})
    return authorize
