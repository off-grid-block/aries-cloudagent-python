# ************Edited Beginning************
# File created for Yale research
#     by Ashlin, Minto, Athul Antony
# This is for multple client implementation
# ************Edited End******************

import asyncio
import argparse
import base64
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

temp_message=None
temp_data=None
agent = None
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
        self._connection_ready = asyncio.Future()
        self.cred_state = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        global temp_message
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self.log("Connected")
                temp_message=message
                self._connection_ready.set_result(True)

    async def handle_issue_credential(self, message):
        state = message["state"]
        credential_exchange_id = message["credential_exchange_id"]
        prev_state = self.cred_state.get(credential_exchange_id)
        if prev_state == state:
            return  # ignore
        self.cred_state[credential_exchange_id] = state

        self.log(
            "Credential: state =",
            state,
            ", credential_exchange_id =",
            credential_exchange_id,
        )

        if state == "offer_received":
            log_status("#15 After receiving credential offer, send credential request")
            await self.admin_POST(
                "/issue-credential/records/" f"{credential_exchange_id}/send-request"
            )

        elif state == "credential_acked":
            self.log("Stored credential {cred_id} in wallet")
            cred_id = message["credential_id"]
            log_status(f"#18.1 Stored credential {cred_id} in wallet")
            resp = await self.admin_GET(f"/credential/{cred_id}")
            log_json(resp, label="Credential details:")
            log_json(
                message["credential_request_metadata"],
                label="Credential request metadata:",
            )
            self.log("credential_id", message["credential_id"])
            self.log("credential_definition_id", message["credential_definition_id"])
            self.log("schema_id", message["schema_id"])

    async def handle_present_proof(self, message):
        state = message["state"]
        presentation_exchange_id = message["presentation_exchange_id"]
        presentation_request = message["presentation_request"]

        log_msg(
            "Presentation: state =",
            state,
            ", presentation_exchange_id =",
            presentation_exchange_id,
        )

        if state == "request_received":
            log_status(
                "#24 Query for credentials in the wallet that satisfy the proof request"
            )

            # include self-attested attributes (not included in credentials)
            credentials_by_reft = {}
            revealed = {}
            self_attested = {}
            predicates = {}

            # select credentials to provide for the proof
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

            log_status("#25 Generate the proof")
            request = {
                "requested_predicates": predicates,
                "requested_attributes": revealed,
                "self_attested_attributes": self_attested,
            }

            log_status("#26 Send the proof to X")
            await self.admin_POST(
                (
                    "/present-proof/records/"
                    f"{presentation_exchange_id}/send-presentation"
                ),
                request,
            )

    async def handle_basicmessages(self, message):
        global temp_data
        print(json.loads(message["content"]))
        temp_data=json.loads(message["content"])


async def handle_input_invitation(request):
    global agent
    data = await request.json()
    details = data['invitation']
    connection = await agent.admin_POST("/connections/receive-invitation", details)
    agent.connection_id = connection["connection_id"]
    log_json(connection, label="Invitation response:")
    await agent.detect_connection()


async def main(start_port: int, show_timing: bool = False, container_name: str = "Simple_client"):
    global temp_message
    global temp_data
    global agent
    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)
    try:
        log_status("#7 Provision an agent and wallet, get back configuration details")
        label=container_name
        agent = ClientAgent(
            label, start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)

        with log_timer("Startup duration:"):
            await agent.start_process()
        log_msg("Admin url is at:", agent.admin_url)
        log_msg("Endpoint url is at:", agent.endpoint)
        app = web.Application()

        app.add_routes([
            web.post('/input_invitation', handle_input_invitation),
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
        "-p",
        "--port",
        type=int,
        metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    parser.add_argument(
        "--container"
    )
    args = parser.parse_args()
    require_indy()
    try:
        web.run_app(main(args.port, args.timing, args.container), host='0.0.0.0', port=(args.port+3))
    except KeyboardInterrupt:
        os._exit(1)




