# #########################################
# This is for msp agent implementation
# ########################################

import base64
import argparse
import asyncio
import json
import logging
import os
import sys
from uuid import uuid4
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiohttp import web
from aiohttp_apispec import setup_aiohttp_apispec
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

LOGGER          = logging.getLogger(__name__)
agent           = None
proof_message   = None
pool_handle     = None

class MSPAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "MSP Agent",
            http_port,
            admin_port,
            prefix="MSP",
            extra_args=["--auto-accept-invites", "--auto-accept-requests"],
            seed=None,
            **kwargs,
        )
        self._connection_ready  = None
        self.cred_state         = {}
        self.cred_attrs         = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()
        
    # Functions to handle connections,proof and basic messages
    
    async def handle_connections(self, message):
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self._connection_ready.set_result(True)
    
    async def handle_present_proof(self, message):
        global proof_message
        global proof_event
        if message["state"] == "presentation_received":
            proof_message=message
            proof_event.set()

    async def handle_basicmessages(self, message):
        self.log("Received message:", message["content"])

async def on_startup(app: web.Application):
    """Perform webserver startup actions."""

# Function to create an invitation for an agent

async def handle_create_invitation(request):
    global agent
    connection = await agent.admin_POST("/connections/create-invitation")
    agent.connection_id = connection["connection_id"]
    agent._connection_ready = asyncio.Future()
    log_json(connection, label="Invitation response:")
    log_msg(json.dumps(connection["invitation"]), label="Invitation:", color=None)
  
    return web.json_response(connection["invitation"])

async def handle_verify_proof(request):
    global agent
    global proof_event
    global proof_message
    req_attrs           = [] 
    temp_req            = None
    req_preds           = []
    attributes_asked    = {}
    data                = await request.json()

    # Check if any attribute not there.
    
    for element in ['proof_attr','connection_id']:
        if element not in data:
            return web.json_response({"status" : "Attribute missing"})
            
    # Check if any attribute has not value.
    
    if (data['proof_attr']==None or data['proof_attr']=='') or (data['connection_id']==None or data['connection_id']==''):
        return web.json_response({"status" : "Enter valid details"})

    proof_attr          = [element.strip(' ') for element in data['proof_attr'].split(",")]
    connection_id       = data['connection_id']

    for item in proof_attr:
        req_attrs.append(
            {
                "name": item.strip(),
            }
        )

    # Checking if predicate is given or not 
    
    if ('req_predicate' not in data)==True:
        temp_req = {}
    elif data['req_predicate']==None or data['req_predicate']=='' or (not data['req_predicate']):
        temp_req = {}
    else:
        temp_req = { 'predicate1_referent' : data['req_predicate'] }

    # Checking if the issuer did is given or not
    
    if ('issuer_did_list' in data)==True:
        issuer_did_list = data['issuer_did_list']
        for inc in range(0, len(req_attrs)):
            req_attrs[inc]['restrictions'] =  issuer_did_list

    indy_proof_request  = {
        "name": "simple_proof",
        "version": "1.0",
        "nonce": str(uuid4().int),
        "requested_attributes": {
            f"0_{req_attr['name']}_uuid": req_attr
            for req_attr in req_attrs
        },
        "requested_predicates" : temp_req
    }
    
    proof_request_web_request = {
        "connection_id": connection_id,
        "proof_request": indy_proof_request
    }
    
    # Sending Proof request
    try:
        await agent.admin_POST(
            "/present-proof/send-request",
            proof_request_web_request
        ) 
    except:
        return web.json_response({
            "status"        : "Error while sending proof request",
        })  

    proof_event = asyncio.Event()
    await proof_event.wait()
    presentation_exchange_id = proof_message["presentation_exchange_id"]
    
    # Verifying Proof
    try:
        proof = await agent.admin_POST(
            f"/present-proof/records/{presentation_exchange_id}/"
            "verify-presentation"
        )
    except:
        return web.json_response({
            "status"        : "Error while verifing proof",
        }) 
        
    # Return Proof Status and Attributes requested in the Proof Request
    
    if proof["verified"]=='true':
        try:
            pres_req = proof_message["presentation_request"]
            pres = proof_message["presentation"]

            for (referent, attr_spec) in pres_req["requested_attributes"].items():
                attributes_asked[str(attr_spec['name'])] = pres['requested_proof']['revealed_attrs'][referent]['raw']
                print(attr_spec['name'])
                print(pres['requested_proof']['revealed_attrs'][referent]['raw'])

            for id_spec in pres["identifiers"]:
                agent.log(id_spec['schema_id'])
                agent.log(id_spec['cred_def_id'])
            
            return web.json_response({
                "status"        : proof["verified"],
                "attributes"    : attributes_asked,
            })
        except:
            return web.json_response({
                "status"        : 'False',
            }) 
    else:
        return web.json_response({
            "status"        : proof["verified"],
        })   

# Verification of Signature

async def handle_verify_signature(request):
    global agent
    global pool_handle
    
    pool_data = await agent.admin_POST("/connections/open-pool", {
        "pool_handle" : pool_handle
    })
    
    pool_handle     = pool_data['pool_handle']
    data            = await request.json()
    resp            = {}

    # Check if any attribute is not present
    
    for element in ['message','their_did','signature']:
        if element not in data:
            return web.json_response({"status" : "Attribute missing"})
            
    # Check if any attribute has no value
    
    if (data['message']==None or data['message']=='') or (data['their_did']==None or data['their_did']=='') or (data['signature']==None or data['signature']==''):
        return web.json_response({"status" : "Enter valid details"})

    message         = data['message']
    their_did       = data['their_did']
    signature       = data['signature']


    try:
        temp=signature.encode('iso-8859-15')
        temp1=base64.b64decode(temp)
        signature=temp1.decode('utf-8')
    except:
        return web.json_response({"status" : "Invalid signature"})

    verify = await agent.admin_POST("/connections/verify-transaction", {
        "message" :  message,
        "their_did" : their_did,
        "signature" : signature,
        "pool_handle" : pool_handle
    })

    if verify['status']=='True':
        res = await agent.admin_GET(f"/connections", 
            params = {
                "their_did" : their_did
            })
        if res!=[]:
            resp['status']        = "Signature verified"
            resp['connection_id'] = res[0]['connection_id']
        else:
           resp['status']="Signature verified but not connected to the client agent" 
    else:
        resp['status']="not verified"

    return web.json_response(resp)


async def main(start_port: int, show_timing: bool = False):
    global agent
    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)

    try:
        agent = MSPAgent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)

        with log_timer("Startup duration:"):
            await agent.start_process()
        app = web.Application()
        app.add_routes([
            web.get('/create_invitation', handle_create_invitation),
            web.post('/verify_signature', handle_verify_signature),
            web.post('/verify_proof', handle_verify_proof),
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
        log_status("Error while provision an agent and wallet, get back configuration details")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="This is starting of MSP Agent")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        metavar=("<port>"),
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    
    args = parser.parse_args()
    require_indy()

    try:
        web.run_app(main(args.port, args.timing), host='0.0.0.0', port=(args.port+7))
    except KeyboardInterrupt:
        os._exit(1)




