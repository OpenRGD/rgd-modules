"""Local maintainer adapter: actual pinned TUF review for candidate reservation only.

Never accepts a producer-supplied positive report. No control-operation signer or
production publication authorization; local operator owns trust root/cache.
"""
from copy import deepcopy
from pathlib import Path
from ledger_contract import decode, digest, encode_proposal
from tools.review_trusted_candidate import review_trusted_candidate, TrustError
from tools.release_ledger import LedgerError

def tuf_reservation_authorizer(*,catalog_id,ledger_id,manifest_raw,descriptor_bytes,
                               artifact_bytes,source_claim,filename,namespace,
                               metadata_files,cache_dir,bootstrap):
    # Snapshot exact supplied bytes; don't let mutable caller dicts change
    # between validation and use. Caller is trusted local operator.
    descriptors=deepcopy(descriptor_bytes)
    artifacts=deepcopy(artifact_bytes)
    metadata=deepcopy(metadata_files)
    def authorize(request_raw,proposal_raw,evidence_raw,prior_receipt_raw):
        req=decode("ReleaseLedgerRequest",request_raw)
        if (req["catalog_id"],req["ledger_id"])!=(catalog_id,ledger_id) or \
           req["operation"]!="RESERVE_CANDIDATE" or prior_receipt_raw is not None:
            raise LedgerError("TUF_RESERVATION_SCOPE_ONLY")
        try:
            result=review_trusted_candidate(manifest_raw=manifest_raw,descriptor_bytes=descriptors,
                 artifact_bytes=artifacts,source_claim=source_claim,submitted_proposal_raw=proposal_raw,
                 filename=filename,namespace=namespace,review_raw=evidence_raw,metadata_files=metadata,
                 cache_dir=cache_dir,bootstrap=bootstrap,prior_proposal_sha256=None)
        except TrustError as e:
            raise LedgerError("TUF_REVIEW_REJECTED") from e
        if result["review_status"]!="REVIEWED_CANDIDATE_ONLY" or \
           result["proposal_sha256"]!=req["proposal_sha256"] or \
           result["publisher_delegation_verified"] is not True or \
           result["registry_review_target_verified"] is not True:
            raise LedgerError("TUF_REVIEW_BINDING_MISMATCH")
        # Persist commitments of verified review context, not private keys or
        # invented production approval. External archive owns actual TUF bytes.
        root=(Path(cache_dir)/"root.json").read_bytes()
        return encode_proposal({"profile":"TUF_OFFLINE_REVIEW_CANDIDATE",
            "request_sha256":digest(request_raw),"review_target_sha256":digest(evidence_raw),
            "review_result_sha256":digest(encode_proposal(result)),"trusted_root_sha256":digest(root),
            "metadata_sha256":{k:digest(v) for k,v in sorted(metadata.items())},
            "snapshot_version":result["tuf_snapshot_version"],"timestamp_version":result["tuf_timestamp_version"],
            "publication_committed":False,"installable":False,"grants_authority":False})
    return authorize
