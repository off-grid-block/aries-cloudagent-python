# ############################################################
# This file is for communicating with the credential issuer UI 
# ############################################################
import argparse
import asyncio
import json
import random
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

agent           = None
issue_message   = None
temp_conn_id    = None
credDefIdList   = []
connection_list = []

class CredentialIssuerAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "CredentialIssuer Agent",
            http_port,
            admin_port,
            prefix="CredentialIssuer",
            extra_args=["--auto-accept-invites", "--auto-accept-requests"],
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready=None
        self.cred_state = {}
        self.cred_attrs = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        global connection_list
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                connection_list.append({
                    "their_label"   : message['their_label'],
                    "connection_id" : message['connection_id']
                })
                self._connection_ready.set_result(True)
                
                # Sending a message requesting the verkey and did
                # Start here
                
                msg= {
                    "status" : "Requesting verkey and did"
                }
                await agent.admin_POST(
                    f"/connections/{self.connection_id}/send-message",
                    {"content": json.dumps(msg)},
                )
                
                # Sending a message requesting the verkey and did
                # Ends here

    async def handle_issue_credential(self, message):
        global issue_message
        issue_message = message

    async def handle_basicmessages(self, message):
        try:
        # Putting the verkey into the ledger using did 
        # Start here
        
            msg=json.loads(message["content"])
            if "status" in msg:
                if msg['status'] == "Sending verkey and did":
                    await putKeyToLedger(msg['signing_did'], msg['signing_vk'])
        except:
            self.log("Received message:", message["content"])
            
        # Putting the verkey into the ledger using did
        # Ends here
        
# Create Invitation for the Issuer Agent

async def handle_create_invitation(request):
    global agent
    connection = await agent.admin_POST("/connections/create-invitation")
    agent._connection_ready=asyncio.Future()
    agent.connection_id = connection["connection_id"]
    return web.json_response(connection["invitation"])
    
# Create Schema and Credential Definition

async def handle_create_schema_credential_definition(request):
    global agent
    global credDefIdList
    data                = await request.json()
    
    # Check if data is empty or has the values
    
    if "schema_name" not in data:
        return web.json_response({"status" : "Schema name needed"})
    if "attributes" not in data:
        return web.json_response({"status" : "Attributes needed"})
        
    # Schema name and attributes input validation
    
    if data['schema_name']=='' or data['schema_name']==None:
        return web.json_response({"status" : "Enter a valid schema name"})
    if data['attributes']=='' or data['attributes']==None:
        return web.json_response({"status" : "Enter valid attibutes"})

    schema_name         = data['schema_name']
    attr_list           = [element.strip(' ') for element in data['attributes'].split(",")]

    version = format(
        "%d.%d.%d"
        % (
            random.randint(1, 101),
            random.randint(1, 101),
            random.randint(1, 101),
        )
    )
    
    (schema_id, credential_definition_id) = await agent.register_schema_and_creddef(
        schema_name, version, attr_list
    )

 

    credDefIdList.append({
        "schema_name" : schema_name,
        "credential_definition_id" : credential_definition_id,
        "attr_list" : attr_list,
    })

    return web.json_response({
        "schema_id"                : schema_id,
        "credential_definition_id" : credential_definition_id
    })
    
# Sending Credential offer to the Client Agent 

async def handle_send_credential_offer(request):
    global agent
    global temp_conn_id
    CRED_PREVIEW_TYPE = ("did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/issue-credential/1.0/credential-preview")
    data = await request.json()

    # Check if data is empty
    
    if 'credential_definition_id' not in data:
        return web.json_response({"status" : "Credential definition id needed"})
        
    if 'attr_data' not in data:
        return web.json_response({"status" : "Attribute data needed"})
        
    if 'connection_id' not in data:
        return web.json_response({"status" : "Connection id needed"})
        
    # Credential definition id, Attributes, Connection id input validation
    
    if data['credential_definition_id']=='' or data['credential_definition_id']==None:
        return web.json_response({"status" : "Enter a valid credential definition id"})
        
    if data['attr_data']=='' or data['attr_data']==None:
        return web.json_response({"status" : "Enter valid attibutes"})
        
    if data['connection_id']=='' or data['connection_id']==None:
        return web.json_response({"status" : "Enter a valid connection id"})

    credential_definition_id                        = data['credential_definition_id']
    attr_data                                       = data['attr_data']
    agent.cred_attrs[credential_definition_id]      = attr_data
    agent.connection_id                             = data["connection_id"]

    temp_conn_id                                    = agent.connection_id

    cred_preview = {
        "@type": CRED_PREVIEW_TYPE,
        "attributes": [
            {"name": n, "value": v}
            for (n, v) in agent.cred_attrs[credential_definition_id].items()
        ],
    }

    offer_request = {
        "connection_id": agent.connection_id,
        "cred_def_id": credential_definition_id,
        "comment": f"Offer on cred def id {credential_definition_id}",
        "credential_preview": cred_preview,
    }
    await agent.admin_POST("/issue-credential/send-offer", offer_request)

    return web.json_response({"status" : True})
    
# Issuing Credential to the Client Agent 

async def handle_issue_credential(request):
    global agent
    global issue_message
    global temp_conn_id

    CRED_PREVIEW_TYPE = ("did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/issue-credential/1.0/credential-preview")
    state = issue_message["state"]
    credential_exchange_id = issue_message["credential_exchange_id"]
    prev_state = agent.cred_state.get(credential_exchange_id)

    if prev_state == state:
        return  # ignore
    agent.cred_state[credential_exchange_id] = state

    if state == "request_received":
        cred_attrs = agent.cred_attrs[issue_message["credential_definition_id"]]
        cred_preview = {
            "@type": CRED_PREVIEW_TYPE,
            "attributes": [
                {"name": n, "value": v} for (n, v) in cred_attrs.items()
            ],
        }        
        res = await agent.admin_POST(
            f"/issue-credential/records/{credential_exchange_id}/issue",
            {
                "comment": f"Issuing credential, exchange {credential_exchange_id}",
                "credential_preview": cred_preview,
            },
        )
      

        return web.json_response({
            "status" : True
            }
        )
        
# Obtaining the list of Connections

async def handle_get_connection_list(request):
    global agent
    global connection_list
    return web.json_response({"connectionList" : connection_list})
    
# Obtaining the list of Credential Definition

async def handle_get_cred_def_list(request):
    global credDefIdList
    return web.json_response({"credDefIdList" : credDefIdList})
    
# Place the key to the Indy Ledger

async def putKeyToLedger(signing_did:str=None, signing_vk:str=None):
    global agent

    res=await agent.admin_POST("/connections/put-key-ledger", {
        "did"               : agent.did,
        "signing_did"       : signing_did,
        "signing_vk"        : signing_vk
    })
    
    if res['status']=="true":
        return web.json_response({"status" : True})
    else:
        return web.json_response({"status" : False})

async def main(start_port: int, show_timing: bool = False):
    global agent
    genesis = await default_genesis_txns()
    agent = None
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)
    try:
        agent = CredentialIssuerAgent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)
        await agent.register_did()
        with log_timer("Startup duration:"):
            await agent.start_process()
        app = web.Application()
        app.add_routes([
            web.get('/create_invitation', handle_create_invitation),
            web.post('/create_schema_cred_def', handle_create_schema_credential_definition),
            web.post('/send_credential_offer', handle_send_credential_offer),
            web.get('/issue_credential', handle_issue_credential),
            web.get('/get_connection_list', handle_get_connection_list),
            web.get('/get_cred_def_list', handle_get_cred_def_list),
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
    parser = argparse.ArgumentParser(description="Runs a CredentialIssuer demo agent.")
    parser.add_argument(
        "-p", "--port", type=int, metavar=("<port>")
    )
    parser.add_argument(
        "--timing", action="store_true"
    )
    args = parser.parse_args()
    require_indy()
    try:
        web.run_app(main(args.port, args.timing), host='0.0.0.0', port=(args.port+7))
    except KeyboardInterrupt:
        os._exit(1)





