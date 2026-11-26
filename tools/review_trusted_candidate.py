"""EXT-01B.3 TUF review reference, not a publisher, installer or trust service.

Offline metadata transport, actual python-tuf Updater verification. Bootstrap
and persistent cache are trusted local operator inputs, never producer metadata.
The existing v0.1 catalog is consulted read-only. No production keys or network.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote
from tuf.api.metadata import Metadata, Root, Targets, Snapshot, Timestamp
from tuf.api.exceptions import DownloadHTTPError, RepositoryError, DownloadError
from tuf.ngclient import Updater, UpdaterConfig
from tuf.ngclient.fetcher import FetcherInterface
from proposal import parse, digest, encode_proposal, ProposalError
from wheel_inspection import inspect_wheel, review_target, InspectionError
from tools.review_publication import review_publication, ReviewError

BASE = "https://offline-review.invalid/metadata/"
MAX_METADATA = 65536
MAX_METADATA_FILES = 32
HEX_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")

class TrustError(ValueError):
    """Fixed review diagnostic; no positive publication or permission."""

def need(condition, code):
    if not condition:
        raise TrustError(code)

class MemoryMetadata(FetcherInterface):
    """Only exact supplied metadata filenames; cannot access network or paths."""
    def __init__(self, files):
        need(type(files) is dict and len(files)<=MAX_METADATA_FILES,"METADATA_SET_LIMIT")
        self.files={}
        for name,raw in files.items():
            need(type(name) is str and re.fullmatch(r"[a-zA-Z0-9_.-]+\.json",name)
                 and type(raw) is bytes and 0<len(raw)<=MAX_METADATA,"METADATA_INPUT_LIMIT")
            try:
                parse(raw)  # reject duplicate keys and oversized/deep JSON before TUF
            except ProposalError as e:
                raise TrustError("METADATA_JSON_REJECTED") from e
            self.files[name]=raw

    def _fetch(self,url):
        need(url.startswith(BASE),"TRANSPORT_SCOPE")
        name=unquote(url[len(BASE):])
        if name not in self.files:
            raise DownloadHTTPError("metadata unavailable",404)
        return iter((self.files[name],))

def cached_metadata(cache,role,expected):
    # Only fixed top-level or strictly generated direct role names are used.
    path=cache/(role+".json")
    with path.open("rb") as f:
        raw=f.read(MAX_METADATA+1)
    need(len(raw)<=MAX_METADATA,"CACHE_METADATA_LIMIT")
    parse(raw)
    m=Metadata.from_bytes(raw)
    need(isinstance(m.signed,expected),"CACHE_METADATA_TYPE")
    return m

def ed25519_material(keys,role):
    materials=[]
    need(type(role.threshold) is int and 1<=role.threshold<=len(role.keyids)
         and len(role.keyids)==len(set(role.keyids)),"ROLE_THRESHOLD")
    for kid in role.keyids:
        key=keys[kid]
        public=key.keyval.get("public")
        need(key.keytype=="ed25519" and key.scheme=="ed25519" and
             isinstance(public,str) and re.fullmatch(r"[a-f0-9]{64}",public),"UNSUPPORTED_KEY_PROFILE")
        materials.append(public)
    need(len(materials)==len(set(materials)),"KEY_ALIAS_COLLISION")
    return set(materials)

def verify_target_bytes(target,raw):
    need(target is not None and "sha256" in target.hashes,"TARGET_MISSING_OR_UNSUPPORTED")
    target.verify_length_and_hashes(raw)

def review_trusted_candidate(*, manifest_raw, descriptor_bytes, artifact_bytes,
                             source_claim, submitted_proposal_raw, filename,
                             namespace, review_raw, metadata_files, cache_dir,
                             bootstrap, prior_proposal_sha256):
    """Review once against refreshed metadata; no publication, install or grant.

    bootstrap: explicitly provisioned root bytes ONLY for an empty private cache.
    On subsequent calls pass None and reuse that same protected persistent cache.
    prior_proposal_sha256: from the trusted caller's retained review ledger, or
    None when absent from that local view. NOT supplied by the producer.
    """
    # Independently recompute the existing candidate and reject legacy keys.
    try:
        old=review_publication(manifest_raw,descriptor_bytes,artifact_bytes,
                               source_claim,submitted_proposal_raw)
        report=inspect_wheel(manifest_raw,descriptor_bytes,artifact_bytes,
                             source_claim,submitted_proposal_raw,filename)
        expected=review_target(report,namespace)
        actual=parse(review_raw)
    except (ReviewError,InspectionError,ProposalError) as e:
        raise TrustError("PUBLICATION_OR_ARTIFACT_REVIEW_REJECTED") from e
    if prior_proposal_sha256 is not None:
        need(type(prior_proposal_sha256) is str and HEX_DIGEST.fullmatch(prior_proposal_sha256),
             "INVALID_PRIOR_COMMITMENT")
        need(prior_proposal_sha256==report["proposal_sha256"],"RELEASE_KEY_CONTENT_CONFLICT")

    # Independent registry target is authenticated below, not trusted from JSON.
    module,version=expected["module_id"],expected["module_version"]
    role_name="publisher-"+namespace
    proposal_path=f"proposals/{module}/{version}.json"
    review_path=f"reviews/{module}/{version}.json"
    cache=Path(cache_dir)
    need(cache.is_dir() and not cache.is_symlink(),"PRIVATE_CACHE_REQUIRED")
    if bootstrap is not None:
        need(not any(cache.iterdir()),"BOOTSTRAP_RESET_FORBIDDEN")
        need(type(bootstrap) is bytes and 0<len(bootstrap)<=MAX_METADATA,"BOOTSTRAP_LIMIT")
    else:
        need((cache/"root.json").is_file(),"BOOTSTRAP_REQUIRED")
    try:
        if bootstrap is not None:
            parse(bootstrap)
        fetcher=MemoryMetadata(metadata_files)
        updater=Updater(str(cache),BASE,fetcher=fetcher,
                        config=UpdaterConfig(max_root_rotations=16,max_delegations=16,
                           root_max_length=MAX_METADATA,timestamp_max_length=MAX_METADATA,
                           snapshot_max_length=MAX_METADATA,targets_max_length=MAX_METADATA),
                        bootstrap=bootstrap)
        updater.refresh()
        root=cached_metadata(cache,"root",Root).signed
        top=cached_metadata(cache,"targets",Targets).signed
        snapshot=cached_metadata(cache,"snapshot",Snapshot).signed
        timestamp=cached_metadata(cache,"timestamp",Timestamp).signed
        need(root.consistent_snapshot is True,"CONSISTENT_SNAPSHOT_REQUIRED")
        # No parent-role shortcut: force the selected direct publisher
        # delegation and a separate top-level registry review target.
        need(proposal_path not in top.targets,"PUBLISHER_DELEGATION_BYPASS")
        need(review_path in top.targets,"REGISTRY_REVIEW_MISSING")
        delegations=top.delegations
        need(delegations is not None and delegations.roles is not None
             and delegations.succinct_roles is None,"PUBLISHER_DELEGATION_MISSING")
        role=delegations.roles.get(role_name)
        need(role is not None and role.terminating is True
             and role.paths==[f"proposals/{namespace}.*/*"]
             and role.path_hash_prefixes is None,"PUBLISHER_DELEGATION_SCOPE")
        matches=[name for name,d in delegations.roles.items() if d.is_delegated_path(proposal_path)]
        need(matches==[role_name],"PUBLISHER_DELEGATION_AMBIGUOUS")
        # Key-material separation, not just key-ID string comparison.
        seen=set()
        for r in ("root","timestamp","snapshot","targets"):
            material=ed25519_material(root.keys,root.roles[r])
            need(not seen.intersection(material),"ROLE_KEY_REUSE")
            seen.update(material)
        need(not seen.intersection(ed25519_material(delegations.keys,role)),
             "ROLE_KEY_REUSE")
        # Require snapshot hashes/lengths as well as versions. Updater enforces
        # signature, expiry, root rotation and rollback checks.
        for m in (timestamp.snapshot_meta,snapshot.meta.get("targets.json"),
                  snapshot.meta.get(role_name+".json")):
            need(m is not None and m.length is not None and m.hashes is not None
                 and "sha256" in m.hashes,"SNAPSHOT_COMMITMENT_REQUIRED")
        target=updater.get_targetinfo(proposal_path)
        verify_target_bytes(target,submitted_proposal_raw)
        delegated=cached_metadata(cache,role_name,Targets).signed
        need(delegated.delegations is None and proposal_path in delegated.targets,
             "DIRECT_PUBLISHER_TARGET_REQUIRED")
        need(all(role.is_delegated_path(path) for path in delegated.targets),
             "PUBLISHER_TARGET_OUTSIDE_SCOPE")
        verify_target_bytes(delegated.targets[proposal_path],submitted_proposal_raw)
        verify_target_bytes(top.targets[review_path],review_raw)
        # Withdrawal is an authenticated denial; never treat an old review as current.
        withdrawn=review_target(report,namespace,"WITHDRAWN")
        if encode_proposal(actual)==encode_proposal(withdrawn):
            raise TrustError("REGISTRY_REVIEW_WITHDRAWN")
        need(encode_proposal(actual)==encode_proposal(expected),"REGISTRY_REVIEW_BINDING_MISMATCH")
    except TrustError:
        raise
    except (RepositoryError,DownloadError,OSError,ValueError,TypeError,KeyError) as e:
        raise TrustError("TUF_VERIFICATION_REJECTED") from e

    return {
        "review_status":"REVIEWED_CANDIDATE_ONLY",
        "namespace":namespace,
        "publisher_delegation_verified":True,
        "registry_review_target_verified":True,
        "tuf_snapshot_version":snapshot.version,
        "tuf_timestamp_version":timestamp.version,
        "proposal_sha256":report["proposal_sha256"],
        "inspection":report,
        "local_prior_commitment_matched":prior_proposal_sha256 is not None,
        "global_release_uniqueness_verified":False,
        "source_commit_verified":False,
        "build_provenance_verified":False,
        "publisher_legal_identity_verified":False,
        "registry_publication_committed":False,
        "installable":False,
        "grants_authority":False,
    }
