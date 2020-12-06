import argparse
import asyncio
import json
import logging
import os
import sys
from uuid import uuid4
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa
from aiohttp import web, ClientSession, DummyCookieJar
from aiohttp_apispec import docs, response_schema, setup_aiohttp_apispec
import aiohttp_cors
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
proof_message = None
proof_state = None

class AcmeAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "Acme Agent",
            http_port,
            admin_port,
            prefix="Acme",
            extra_args=["--auto-accept-invites", "--auto-accept-requests"],
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready = asyncio.Future()
        self.cred_state = {}
        self.cred_attrs = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self.log("Connected")
                self._connection_ready.set_result(True)

    async def handle_present_proof(self, message):
        global proof_message
        global proof_state
        
        state = message["state"]
        proof_state = state
        if state == "presentation_received":
            print("Proof has been got")
            proof_message = message

    async def handle_basicmessages(self, message):
        self.log("Received message:", message["content"])

async def on_startup(app: web.Application):
    """Perform webserver startup actions."""

async def handle_create_invitation(request):
    with log_timer("Generate invitation duration:"):
        log_status(
            "#5 Create a connection to alice and print out the invite details"
        )
        connection = await agent.admin_POST("/connections/create-invitation")

    agent.connection_id = connection["connection_id"]
    log_json(connection, label="Invitation response:")
    log_msg("*****************")
    log_msg(json.dumps(connection["invitation"]), label="Invitation:", color=None)
    log_msg("*****************")

    return web.json_response(connection["invitation"])


async def handle_send_request(request):
    global agent
    req_attrs = [] 
    req_preds = []
    data = await request.post()

    proof_attr = data['proof_attr'].split(",")
    proof_name = data['proof_name']
    proof_schema_name = data['proof_schema_name']

    for item in proof_attr:
        req_attrs.append(
            {
                "name": item,
                "restrictions": [{"schema_name": proof_schema_name}]
            }
        )

    indy_proof_request = {
        "name": proof_name,
        "version": "1.0",
        "nonce": str(uuid4().int),
        "requested_attributes": {
            f"0_{req_attr['name']}_uuid": req_attr
            for req_attr in req_attrs
        },
        "requested_predicates": {}
    }


    proof_request_web_request = {
        "connection_id": agent.connection_id,
        "proof_request": indy_proof_request
    }

    print(proof_request_web_request)

    await agent.admin_POST(
        "/present-proof/send-request",
        proof_request_web_request
    )

    return web.json_response({"status" : True})


async def handle_basic_message(request):
    global agent
    data = await request.post()
    msg = data['message']
    await agent.admin_POST(
        f"/connections/{agent.connection_id}/send-message", {"content": msg}
    )
    return web.json_response({"status" : True})

async def handle_verify_proof(request):
    global agent
    global proof_message
    global proof_state
    attributes_asked = {}

    if proof_message == None:
        return web.json_response({"status" : "You dont have proof to verify!!"})

    presentation_exchange_id = proof_message["presentation_exchange_id"]

    proof = await agent.admin_POST(
        f"/present-proof/records/{presentation_exchange_id}/"
        "verify-presentation"
    )

    pres_req = proof_message["presentation_request"]
    pres = proof_message["presentation"]

    log_status("#28.1 Received proof of education, check claims")
    for (referent, attr_spec) in pres_req["requested_attributes"].items():
        attributes_asked[f"{attr_spec['name']}: "] = f"{pres['requested_proof']['revealed_attrs'][referent]['raw']}"
        agent.log(
            f"{attr_spec['name']}: "
            f"{pres['requested_proof']['revealed_attrs'][referent]['raw']}"
        )
    for id_spec in pres["identifiers"]:
        agent.log(f"schema_id: {id_spec['schema_id']}")
        agent.log(f"cred_def_id {id_spec['cred_def_id']}")
    
    return web.json_response({
        "status" : proof["verified"],
        "attributes" : attributes_asked
    })

async def get_connection_id(request):
    global agent
    return web.json_response({"connection_id" : agent.connection_id})

async def main(start_port: int, show_timing: bool = False):
    global agent
    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)

    try:
        log_status("#1 Provision an agent and wallet, get back configuration details")
        agent = AcmeAgent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)
        await agent.register_did()

        with log_timer("Startup duration:"):
            await agent.start_process()
        log_msg("Admin url is at:", agent.admin_url)
        log_msg("Endpoint url is at:", agent.endpoint)

        app = web.Application()
        app.add_routes([
            web.get('/create_invitation', handle_create_invitation),
            web.post('/send_request', handle_send_request),
            web.post('/basic_message', handle_basic_message),
            web.get('/verify_proof', handle_verify_proof),
            web.get('/get_connection_id', get_connection_id),
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

        setup_aiohttp_apispec(
            app=app, title="Aries Cloud Agent two", version="v1", swagger_path="/api/doc"
        )
        app.on_startup.append(on_startup)
        
        return app

    except Exception:
        print("Aries Cloud Agent two")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="This is starting of Acme Agent")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8040,
        metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    
    args = parser.parse_args()
    require_indy()

    try:
        web.run_app(main(args.port, args.timing), host='0.0.0.0', port=8003)
    except KeyboardInterrupt:
        os._exit(1)

