# ####################################################
# The code contains functions to
#       1) Communicate with the credential issuer UI
#       2) Acts as the Msp Server
#####################################################

import argparse
import asyncio
import json
import random
import logging
import base64
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
    require_indy,
)

LOGGER = logging.getLogger(__name__)

agent           = None #This global variable is common for issuer and verifier
#These are the global variables of the issuer
issue_message   = None
temp_conn_id    = None
credDefIdList   = []
connection_list = []
#These are the global variables of the verifier
proof_message   = None

class CI_MSPAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "Issuer and Verfier Agent",
            http_port,
            admin_port,
            prefix="CI_MSP",
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
                self.log("Connected")
                self.log(message)
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
                log_status("Putting did and verkey into the ledger")
                await agent.admin_POST(
                    f"/connections/{self.connection_id}/send-message",
                    {"content": json.dumps(msg)},
                )
                # Sending a message requesting the verkey and did
                # Ends here

    async def handle_issue_credential(self, message):
        global issue_message
        issue_message = message

    async def handle_present_proof(self, message):
        global proof_message
        global proof_event
        if message["state"] == "presentation_received":
            log_status("Proof has been got")
            proof_message=message
            proof_event.set()


    async def handle_basicmessages(self, message):
        try:
        # Putting the verkey using did into the ledger
        # Start here
            msg=json.loads(message["content"])
            if "status" in msg:
                if msg['status'] == "Sending verkey and did":
                    await putKeyToLedger(msg['signing_did'], msg['signing_vk'])
        except:
            self.log("Received message:", message["content"])
        # Putting the verkey using did into the ledger
        # Ends here

# ======================================================================
# ======================================================================
# ======================================================================
#     This is the section dedicated to the api functions of issuer and verfier
# ======================================================================
# ======================================================================
# ======================================================================

async def handle_create_invitation(request):
    global agent
    connection = await agent.admin_POST("/connections/create-invitation")
    agent._connection_ready=asyncio.Future()
    agent.connection_id = connection["connection_id"]
    log_status("Invitation has been created !!")
    return web.json_response(connection["invitation"])

# ======================================================================
# ======================================================================
# ======================================================================
#     This is the section dedicated to the api functions of issuer
# ======================================================================
# ======================================================================
# ======================================================================

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

    log_msg("schema has been created !!")
    log_msg("Schema and Credential definition has been created !!")
    log_msg("Schema id : ", schema_id)
    log_msg("Credential definition id : ", credential_definition_id)

    credDefIdList.append({
        "schema_name" : schema_name,
        "credential_definition_id" : credential_definition_id,
        "attr_list" : attr_list,
    })

    return web.json_response({
        "schema_id"                : schema_id,
        "credential_definition_id" : credential_definition_id
    })

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
        log_status("Credential has been issued!!")
        log_msg(res['credential'])

        return web.json_response({
            "status" : True
            }
        )

async def handle_get_connection_list(request):
    global connection_list
    return web.json_response({"connectionList" : connection_list})

async def handle_get_cred_def_list(request):
    global credDefIdList
    return web.json_response({"credDefIdList" : credDefIdList})

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

# ======================================================================
# ======================================================================
# ======================================================================
#     This is the section dedicated to the api functions of verfier
# ======================================================================
# ======================================================================
# ======================================================================

async def handle_verify_proof(request):
    global agent
    global proof_event
    global proof_message

    log_status("Send proof request has been called !!")
    req_attrs           = []
    temp_req            = None
    req_preds           = []
    attributes_asked    = {}
    data                = await request.json()

    #Check if any proof_attr not there.
    if 'proof_attr' not in data:
        return web.json_response({"status" : "Attribute missing"})
    if data['proof_attr']==None or data['proof_attr']=='':
        return web.json_response({"status" : "Enter valid proof_attr"})

    #Check if connection id or their_did is available or not -- starts here
    if 'connection_id' in data:
        log_status("Connection id available!!")
        if (data['connection_id']!=None and data['connection_id']!=''):
            connection_id       = data['connection_id']
    elif 'their_did' in data:
        log_status("Their did available!!")
        if (data['their_did']!=None and data['their_did']!=''):
            res = await agent.admin_GET(f"/connections",
                params = {
                    "their_did" : data['their_did']
                })
            if res!=[]:
                connection_id = res[0]['connection_id']
            else:
                return web.json_response({"status" : "Invalid their did"})
    else:
        log_status("Both not available!!")
        return web.json_response({"status" : "Enter valid did or connection id"})
    #Check if connection id or their_did is available or not -- ends here

    proof_attr          = [element.strip(' ') for element in data['proof_attr'].split(",")]

    for item in proof_attr:
        req_attrs.append(
            {"name": item.strip()}
        )

    # Checking if prdicate is given or not
    if ('req_predicate' not in data)==True:
        temp_req = {}
    elif data['req_predicate']==None or data['req_predicate']=='' or (not data['req_predicate']):
        temp_req = {}
    else:
        temp_req = { 'predicate1_referent' : data['req_predicate'] }

    # Cheching if issuer did given or not
    if ('issuer_did_list' in data)==True:
        log_msg("Issuer did has been given as input!!")
        issuer_did_list = data['issuer_did_list']
        if not issuer_did_list:
            pass
        else:
            for inc in range(0, len(req_attrs)):
                did_list = []
                for did_value in issuer_did_list:
                    did_list.append({
                        "issuer_did" : did_value
                    })
                req_attrs[inc]['restrictions'] =  did_list

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

    log_msg("++++++++++++++++++++++++++++++++++++++++++")
    log_msg("++++++++++++++++++++++++++++++++++++++++++")
    log_msg("++++++++++++++++++++++++++++++++++++++++++")
    log_msg(indy_proof_request)
    log_msg("++++++++++++++++++++++++++++++++++++++++++")
    log_msg("++++++++++++++++++++++++++++++++++++++++++")
    log_msg("++++++++++++++++++++++++++++++++++++++++++")

    proof_request_web_request = {
        "connection_id": connection_id,
        "proof_request": indy_proof_request
    }

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

    log_status("Verification of proof has been called !!")

    presentation_exchange_id = proof_message["presentation_exchange_id"]
    try:
        proof = await agent.admin_POST(
            f"/present-proof/records/{presentation_exchange_id}/"
            "verify-presentation"
        )
    except:
        return web.json_response({
            "status"        : "Error while verifing proof",
        })

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

async def handle_verify_signature(request):
    global agent

    data            = await request.json()
    resp            = {}

    #Check if any attribute not there.
    for element in ['message','their_did','signature']:
        if element not in data:
            return web.json_response({"status" : "Attribute missing"})
    #Check if any attribute has not value.
    if (data['message']==None or data['message']=='') or (data['their_did']==None or data['their_did']=='') or (data['signature']==None or data['signature']==''):
        return web.json_response({"status" : "Enter valid details"})

    message         = data['message']
    their_did       = data['their_did']
    signature       = data['signature']

    log_msg("message : "+message)
    log_msg("their_did : "+their_did)
    log_msg("signature : "+signature)
    try:
        temp=signature.encode('iso-8859-15')
        temp1=base64.b64decode(temp)
        signature=temp1.decode('utf-8')
    except:
        return web.json_response({"status" : "Invalid signature"})

    while True:
        verify = await agent.admin_POST("/connections/verify-transaction", {
            "message" :  message,
            "their_did" : their_did,
            "signature" : signature
        })
        if verify['status']!='operation not complete':
            break
        else:
            log_status("Still waiting for pool to be closed!!")

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

    log_status("The status : "+verify['status'])

    return web.json_response(resp)

async def main(start_port: int, show_timing: bool = False):
    global agent
    genesis = await default_genesis_txns()
    agent = None
    if not genesis:
        sys.exit(1)
    try:
        agent = CI_MSPAgent(
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
            # These are api's for ci and msp
            web.get('/create_invitation', handle_create_invitation),
            # These are api's for ci
            web.post('/create_schema_cred_def', handle_create_schema_credential_definition),
            web.post('/send_credential_offer', handle_send_credential_offer),
            web.get('/issue_credential', handle_issue_credential),
            web.get('/get_connection_list', handle_get_connection_list),
            web.get('/get_cred_def_list', handle_get_cred_def_list),
            #These are the api's for msp
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

        return app
    except Exception:
        print("Error when starting to run server!!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs a CI_MSP demo agent.")
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




