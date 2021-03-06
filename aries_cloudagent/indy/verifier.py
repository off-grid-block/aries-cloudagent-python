"""Base Indy Verifier class."""

import logging

from abc import ABC, ABCMeta, abstractmethod
from enum import Enum

from ..messaging.util import canon, encode

LOGGER = logging.getLogger(__name__)


class IndyVerifier(ABC, metaclass=ABCMeta):
    """Base class for Indy Verifier."""

    def __repr__(self) -> str:
        """
        Return a human readable representation of this class.

        Returns:
            A human readable string for this class

        """
        return "<{}>".format(self.__class__.__name__)

    @abstractmethod
    def verify_presentation(
        self,
        presentation_request,
        presentation,
        schemas,
        credential_definitions,
        rev_reg_defs,
        rev_reg_entries,
    ):
        """
        Verify a presentation.

        Args:
            presentation_request: Presentation request data
            presentation: Presentation data
            schemas: Schema data
            credential_definitions: credential definition data
            rev_reg_defs: revocation registry definitions
            rev_reg_entries: revocation registry entries
        """

    def non_revoc_intervals(self, pres_req: dict, pres: dict):
        """
        Remove superfluous non-revocation intervals in presentation request.

        Indy rejects proof requests with non-revocation intervals lining up
        with non-revocable credentials in proof: seek and remove.

        Args:
            pres_req: presentation request
            pres: corresponding presentation

        """

        for (req_proof_key, pres_key) in {
            "revealed_attrs": "requested_attributes",
            "revealed_attr_groups": "requested_attributes",
            "predicates": "requested_predicates",
        }.items():
            for (uuid, spec) in pres["requested_proof"].get(req_proof_key, {}).items():
                if (
                    pres["identifiers"][spec["sub_proof_index"]].get("timestamp")
                    is None
                ):
                    if pres_req[pres_key][uuid].pop("non_revoked", None):
                        LOGGER.warning(
                            (
                                "Amended presentation request (nonce=%s): removed "
                                "non-revocation interval at %s referent "
                                "%s; no corresponding revocable credential in proof"
                            ),
                            pres_req["nonce"],
                            pres_key,
                            uuid,
                        )

        if all(spec.get("timestamp") is None for spec in pres["identifiers"]):
            pres_req.pop("non_revoked", None)
            LOGGER.warning(
                (
                    "Amended presentation request (nonce=%s); removed global "
                    "non-revocation interval; no revocable credentials in proof"
                ),
                pres_req["nonce"],
            )

    async def pre_verify(self, pres_req: dict, pres: dict) -> ("PreVerifyResult", str):
        """
        Check for essential components and tampering in presentation.

        Visit encoded attribute values against raw, and predicate bounds,
        in presentation, cross-reference against presentation request.

        Args:
            pres_req: presentation request
            pres: corresponding presentation

        Returns:
            A tuple with `PreVerifyResult` representing the validation result and
            reason text for failure or None for OK.

        """
        if not (
            pres_req
            and "requested_predicates" in pres_req
            and "requested_attributes" in pres_req
        ):
            return (PreVerifyResult.INCOMPLETE, "Incomplete or missing proof request")
        if not pres:
            return (PreVerifyResult.INCOMPLETE, "No proof provided")
        if "requested_proof" not in pres:
            return (PreVerifyResult.INCOMPLETE, "Missing 'requested_proof'")
        if "proof" not in pres:
            return (PreVerifyResult.INCOMPLETE, "Missing 'proof'")

        async with self.ledger:
            for (index, ident) in enumerate(pres["identifiers"]):
                if not ident.get("timestamp"):
                    cred_def_id = ident["cred_def_id"]
                    cred_def = await self.ledger.get_credential_definition(cred_def_id)
                    if cred_def["value"].get("revocation"):
                        return (
                            PreVerifyResult.INCOMPLETE,
                            (
                                f"Missing timestamp in presentation identifier "
                                f"#{index} for cred def id {cred_def_id}"
                            ),
                        )

        for (uuid, req_pred) in pres_req["requested_predicates"].items():
            try:
                canon_attr = canon(req_pred["name"])
                for ge_proof in pres["proof"]["proofs"][
                    pres["requested_proof"]["predicates"][uuid]["sub_proof_index"]
                ]["primary_proof"]["ge_proofs"]:
                    pred = ge_proof["predicate"]
                    if pred["attr_name"] == canon_attr:
                        if pred["value"] != req_pred["p_value"]:
                            return (
                                PreVerifyResult.INCOMPLETE,
                                f"Predicate value != p_value: {pred['attr_name']}",
                            )
                        break
                else:
                    return (
                        PreVerifyResult.INCOMPLETE,
                        f"Missing requested predicate '{uuid}'",
                    )
            except (KeyError, TypeError):
                return (
                    PreVerifyResult.INCOMPLETE,
                    f"Missing requested predicate '{uuid}'",
                )

        revealed_attrs = pres["requested_proof"].get("revealed_attrs", {})
        revealed_groups = pres["requested_proof"].get("revealed_attr_groups", {})
        self_attested = pres["requested_proof"].get("self_attested_attrs", {})
        for (uuid, req_attr) in pres_req["requested_attributes"].items():
            if "name" in req_attr:
                if uuid in revealed_attrs:
                    pres_req_attr_spec = {req_attr["name"]: revealed_attrs[uuid]}
                elif uuid in self_attested:
                    if not req_attr.get("restrictions"):
                        continue
                    else:
                        return (
                            PreVerifyResult.INCOMPLETE,
                            "Attribute with restrictions cannot be self-attested "
                            f"'{req_attr['name']}'",
                        )
                else:
                    return (
                        PreVerifyResult.INCOMPLETE,
                        f"Missing requested attribute '{req_attr['name']}'",
                    )
            elif "names" in req_attr:
                group_spec = revealed_groups.get(uuid)
                if (
                    group_spec is None
                    or "sub_proof_index" not in group_spec
                    or "values" not in group_spec
                ):
                    return (
                        PreVerifyResult.INCOMPLETE,
                        f"Missing requested attribute group '{uuid}'",
                    )
                pres_req_attr_spec = {
                    attr: {
                        "sub_proof_index": group_spec["sub_proof_index"],
                        **group_spec["values"].get(attr),
                    }
                    for attr in req_attr["names"]
                }
            else:
                return (
                    PreVerifyResult.INCOMPLETE,
                    f"Request attribute missing 'name' and 'names': '{uuid}'",
                )

            for (attr, spec) in pres_req_attr_spec.items():
                try:
                    primary_enco = pres["proof"]["proofs"][spec["sub_proof_index"]][
                        "primary_proof"
                    ]["eq_proof"]["revealed_attrs"][canon(attr)]
                except (KeyError, TypeError):
                    return (
                        PreVerifyResult.INCOMPLETE,
                        f"Missing revealed attribute: '{attr}'",
                    )
                if primary_enco != spec["encoded"]:
                    return (
                        PreVerifyResult.ENCODING_MISMATCH,
                        f"Encoded representation mismatch for '{attr}'",
                    )
                if primary_enco != encode(spec["raw"]):
                    return (
                        PreVerifyResult.ENCODING_MISMATCH,
                        f"Encoded representation mismatch for '{attr}'",
                    )

        return (PreVerifyResult.OK, None)


class PreVerifyResult(Enum):
    """Represent the result of IndyVerifier.pre_verify."""

    OK = "ok"
    INCOMPLETE = "missing essential components"
    ENCODING_MISMATCH = "demonstrates tampering with raw values"
