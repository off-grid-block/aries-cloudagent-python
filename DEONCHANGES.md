## Integration with DEON Controller Package
Integrating DEON Fabric-Indy changes into ```aries-cloudagent-python``` v0.5.6.
See previous version of DEON ACA-Py agents in [this repository](https://github.com/off-grid-block/aca-py-controllers).

- ```aries_cloudagent/protocols/connections/v1_0/routes.py```

    - ```put_verification_to_ledger```
        - retrieve ledger object from request_context
        - ```register_nym()``` call to submit signing DID and verkey to ledger.

    - ```create_signing_did```
        - no significant update made from previous iteration.

    - ```store_public_did```
        - endpoint available at ```/connections/store-public-did```
        - new function to store a public DID, already registered with the Indy ledger, into the agent's wallet.
        - necessary for storing application signing DID and verkey information on the ledger.

    - ```sign_transaction```
        - no significant update made from previous iteration.

    - ```verify_transaction```
        - retrieve ledger object from request_context
        - retrieve application signing verkey from ledger using ```get_key_for_did()```