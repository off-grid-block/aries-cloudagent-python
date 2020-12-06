# #########################################
# This is for multiple client implementation
# #########################################
import base64
import asyncio
import argparse
import binascii
import json
import logging
import os
import sys
import json
from urllib.parse import urlparse

from aiohttp import web, ClientSession, DummyCookieJar
from aiohttp_apispec import docs, response_schema, setup_aiohttp_apispec
import aiohttp_cors

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa

from runners.support.agent import DemoAgent, default_genesis_txns
from runners.support.utils import (
    log_json,
    log_msg,
    log_status,
    log_timer,
    prompt,
    prompt_loop,
    require_indy,
)


LOGGER = logging.getLogger(__name__)

agent = None
client_name=None
signing_did=None
signing_vk=None

class ClientAgent(DemoAgent):
    def __init__(self, label, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            label,
            http_port,
            admin_port,
            prefix="Client",
            extra_args=[
                "--auto-accept-invites",
                "--auto-accept-requests",
                "--auto-store-credential",
            ],
            seed=None,
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready = None
        self.cred_state = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self._connection_ready.set_result(True)

    async def handle_issue_credential(self, message):
        state = message["state"]
        credential_exchange_id = message["credential_exchange_id"]
        prev_state = self.cred_state.get(credential_exchange_id)

        if prev_state == state:
            return  # ignore
        self.cred_state[credential_exchange_id] = state

        # After receiving credential offer, send credential request
        if state == "offer_received":
            await self.admin_POST(
                "/issue-credential/records/" f"{credential_exchange_id}/send-request"
            )

       #Storing Credential ID in the Wallet
        elif state == "credential_acked":

            cred_id = message["credential_id"]
            resp = await self.admin_GET(f"/credential/{cred_id}")
            log_json(resp, label="Credential details:")
            log_json(
                message["credential_request_metadata"],
                label="Credential request metadata:",
            )

   # Handling Proof request from the Verifier Agent
    async def handle_present_proof(self, message):
        state = message["state"]
        presentation_exchange_id = message["presentation_exchange_id"]
        presentation_request = message["presentation_request"]

        if state == "request_received":

            credentials_by_reft = {}
            revealed = {}
            self_attested = {}
            predicates = {}

            # Select credentials to provide for the proof
            credentials = await self.admin_GET(
                f"/present-proof/records/{presentation_exchange_id}/credentials"
            )
            if credentials:
                for row in credentials:
                    for referent in row["presentation_referents"]:
                        if referent not in credentials_by_reft:
                            credentials_by_reft[referent] = row

            for referent in presentation_request["requested_attributes"]:
                if referent in credentials_by_reft:
                    revealed[referent] = {
                        "cred_id": credentials_by_reft[referent]["cred_info"][
                            "referent"
                        ],
                        "revealed": True,
                    }
                else:
                    self_attested[referent] = "my self-attested value"

            for referent in presentation_request["requested_predicates"]:
                if referent in credentials_by_reft:
                    predicates[referent] = {
                        "cred_id": credentials_by_reft[referent]["cred_info"][
                            "referent"
                        ],
                        "revealed": True,
                    }

            # Generate the Proof based on the Proof Request
            request = {
                "requested_predicates": predicates,
                "requested_attributes": revealed,
                "self_attested_attributes": self_attested,
            }

            # Sending Proof back to the Verifier Agent
            await self.admin_POST(
                (
                    "/present-proof/records/"
                    f"{presentation_exchange_id}/send-presentation"
                ),
                request,
            )

    async def handle_basicmessages(self, message):
        global signing_did
        global signing_vk
        try:
        # Sending a message containing the verkey and did
        # Start here
            msg=json.loads(message["content"])
            if 'status' in msg:
                if msg['status'] == "Requesting verkey and did":
                    msg= {
                        "status" : "Sending verkey and did",
                        "signing_did" : signing_did,
                        "signing_vk" : signing_vk
                    }
                # Sending verkey and did
                await agent.admin_POST(
                    f"/connections/{self.connection_id}/send-message",
                    {"content": json.dumps(msg)},
                )
        # Sending a message containing the verkey and did
        # Ends here
        except:
            self.log("Received message:", message["content"])

# Obtaining the Client Name
async def handle_get_client_name(request):
    global client_name
    return web.json_response({"client_name" : client_name.replace("_", " ")})

# Obtaining the List of Connections
async def handle_get_connections(request):
    connectionList = await agent.admin_GET(f"/connections", )
    return web.json_response({"connectionList" : connectionList})

# Handling Incoming Invitation
async def handle_input_invitation(request):
    global agent
    global signing_did

    if signing_did=='' or signing_did==None:
        await handle_get_signing_did(None)
    data = await request.json()
    if 'invitation' not in data:
        return web.json_response({"status" : "Invitation needed"})
    if data['invitation']=='' or data['invitation']==None:
        return web.json_response({"status" : "Enter valid invitation"})

    agent._connection_ready=asyncio.Future()
    details = data['invitation']
    try:
        connection = await agent.admin_POST("/connections/receive-invitation", {
            "invitation" : details,
            "signing_did" : signing_did,
        })

        agent.connection_id = connection["connection_id"]
        log_json(connection, label="Invitation response:")
        await agent.detect_connection()
        return web.json_response({"status" : True})
    except:
        return web.json_response({"status" : False})

# Creating and Getting Common did
async def handle_get_signing_did(request):

    global signing_did
    global signing_vk
    global agent

    if signing_did==None:
        result = await agent.admin_POST("/connections/create-signing-did")
        signing_did=result['signing_did']
        signing_vk=result['signing_vk']

    return web.json_response({"signing_did" : signing_did})

# Signing Proposal Response
async def handle_sign_message(request):
    global agent

    data                        = await request.json()

    #Check if signing did and message are there in the data request
    if 'message' not in data:
        return web.json_response({"status" : "Message needed"})
    if 'signing_did' not in data:
        return web.json_response({"status" : "Siging did needed"})

    message                     = data['message']
    signing_did                 = data['signing_did']

    # This part of the code is for
    # validation of the input for signing
    if message=='' or message==None:
        return web.json_response({"status" : "Invalid message"})
    if signing_did=='' or signing_did==None:
        return web.json_response({"status" : "Invalid did"})

    signature = await agent.admin_POST("/connections/sign-transaction", {
        "message" :  message,
        "signing_did" : signing_did,
    })

    if signature['signature']=="Error while signing":
        return web.json_response({"status" : "Error while signing"})
    else:
        temp                       = signature['signature'].encode('utf-8')
        temp1                      = base64.b64encode(temp).decode('iso-8859-15')
        return_data={
            "signature" : temp1,
        }
        return web.json_response(return_data)

async def main(start_port: int, show_timing: bool = False, container_name: str = "Simple_client"):
    global agent
    global client_name
    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)
    try:
        # Provision an agent and wallet, get back configuration details
        label=container_name
        client_name=label
        agent = ClientAgent(
            label, start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)
        # await agent.register_did()
        with log_timer("Startup duration:"):
            await agent.start_process()


        app = web.Application()
        app.add_routes([
            web.get('/get_client_name', handle_get_client_name),
            web.get('/get_connections', handle_get_connections),
            web.post('/input_invitation', handle_input_invitation),
            web.post('/sign_message', handle_sign_message),
            web.get('/get_signing_did', handle_get_signing_did),
        ])

        cors = aiohttp_cors.setup(
            app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods="*",
                )
            },
        )
        for route in app.router.routes():
            cors.add(route)

        return app
    except Exception:
        print("Error when starting to run server!!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs an Client demo agent.")
    parser.add_argument(
        "-p",   "--port",
        type=int, metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    parser.add_argument(
        "--container", help="Get the agent name"
    )
    args = parser.parse_args()
    require_indy()
    try:
        web.run_app(main(args.port, args.timing, args.container), host='0.0.0.0', port=(args.port+3))
    except KeyboardInterrupt:
        os._exit(1)








