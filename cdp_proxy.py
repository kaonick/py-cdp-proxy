"""
Refs from https://github.com/chromedp/chromedp-proxy
Edited by: @nick
"""
import asyncio
import json
import logging

import re
import time
from contextlib import asynccontextmanager
from typing import Optional
import aiofiles
import aiohttp
import uvicorn
from fastapi import FastAPI, Request, WebSocket, HTTPException
from starlette.responses import Response
import websockets
import os
import tempfile
import subprocess

from browser_utils import check_pid_alive

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CDP-Dispatcher")



# Command-line arguments (equivalent to Go's flag package)
import argparse

parser = argparse.ArgumentParser(description="WebSocket and HTTP proxy server")
parser.add_argument("-l", "--listen", default="localhost:9223", help="listen address")
parser.add_argument("-r", "--remote", default="localhost:9222", help="remote address")
parser.add_argument("-n", "--no-log", action="store_true", help="disable logging to file")
parser.add_argument("--log", default="logs/cdp-%s.log", help="log file mask")
parser.add_argument("-rb", "--run_browser", default=True, help="run browser")
parser.add_argument("-c", "--chrome_path", default="google-chrome", help="chrome exec path in linux")
parser.add_argument("-cw", "--chrome_path_win", default="C:/Program Files/Google/Chrome/Application/chrome.exe", help="chrome exec path in windows")


args = parser.parse_args()
# Reverse proxy setup (equivalent to httputil.NewSingleHostReverseProxy)
remote_url = f"http://{args.remote}"
remote_port = args.remote.split(":")[-1]
listen_port = args.listen.split(":")[-1]

# Constants
INCOMING_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB
OUTGOING_BUFFER_SIZE = 25 * 1024 * 1024  # 25MB

# Clean filename regex (equivalent to Go's cleanRE)
clean_re = re.compile(r"[^a-zA-Z0-9_\-\.]")

def create_remote_browser(port:int=9222,user_profile_path:str=None):
    if user_profile_path is None:
        user_profile_path = tempfile.mkdtemp()
    # exeFilePath = f'C:\Program Files\Google\Chrome\Application\chrome.exe --remote-debugging-port={port} --remote-allow-origins=* --user-data-dir="{user_profile_path}"'
    if os.name == 'nt':
        chrome_path = args.chrome_path_win
    else:
        chrome_path = args.chrome_path
    # exeFilePath = f'{chrome_path} --remote-debugging-port={port} --incognito -remote-allow-origins=* --user-data-dir={user_profile_path}'
    exeFilePath = [chrome_path, '--no-sandbox',"--disable-dev-shm-usage", f'--remote-debugging-port={port}', '--incognito', '-remote-allow-origins=*',f'--user-data-dir={user_profile_path}']
    # --disable-first-run-ui --no-default-browser-check --no-first-run
    # --disable-fre ??

    try:
        p = subprocess.Popen(exeFilePath)
        print(p.pid) # the pid
        time.sleep(1)
        return p.pid,port
    except Exception as err:
        print("Error ...")
        print(err)
        return None,None
now_pid=None
if args.run_browser:
    pid,port=create_remote_browser(port=int(remote_port))
    now_pid=pid

# FastAPI app
app = FastAPI()

async def check_browser(request: Request):
    global now_pid
    if now_pid is None or not check_pid_alive(now_pid):
        # Restart the browser
        pid,port=create_remote_browser(port=int(remote_port))
        now_pid=pid
        return Response(content=json.dumps({"status": f"re start browser={pid}:{port}"}), status_code=200)

    return Response(content=json.dumps({"status": f"running:{now_pid}"}), status_code=200)




async def reverse_proxy(request: Request):
    """Forward HTTP requests to the remote server."""
    async with aiohttp.ClientSession() as session:
        url = f"{remote_url}{request.url.path}{request.url.query and '?' + request.url.query or ''}"
        method = request.method
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        body = await request.body()

        async with session.request(method, url, headers=headers, data=body) as resp:
            raw = await resp.read()

            # Replace remote address with local address in response
            replace_url=f'ws://{args.remote}/devtools'.encode()
            new_url = f'ws://{args.listen}/devtools'.encode()
            raw= raw.replace(replace_url,new_url)

            # recount content-length
            headers = dict(resp.headers)
            headers['Content-Length']=str(len(raw))

            return Response(content=raw, status_code=resp.status, headers=headers)


# Create log file and logger (equivalent to createLog)
def create_log(no_log: bool, log_mask: str, id: str) -> tuple[
    Optional[aiofiles.threadpool.AsyncTextIOWrapper], logging.Logger]:
    logger = logging.getLogger(id)
    logger.setLevel(logging.INFO)

    handlers = [logging.StreamHandler()]  # Default to stdout

    if not no_log and log_mask:
        filename = log_mask
        if "%s" in log_mask:
            filename = log_mask % clean_re.sub("", id)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        file_handler = logging.FileHandler(filename)
        handlers.append(file_handler)

    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        logger.addHandler(handler)

    return handlers[-1] if len(handlers) > 1 else None, logger


# Check version (equivalent to checkVersion)
async def check_version(remote: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        url = f"http://{remote}/json/version"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to fetch version: {resp.status}")
            body = await resp.read()
            json.loads(body.decode())  # Validate JSON
            return body


# Proxy WebSocket connections (equivalent to proxyWS)
async def proxy_ws_to_remote(logger: logging.Logger, prefix: str, in_ws: WebSocket, out_ws: websockets.WebSocketClientProtocol,
                   err_queue: asyncio.Queue):
    try:
        while True:
            data = await in_ws.receive_text()
            logger.info(f"{prefix} {data}")
            await out_ws.send(data)
    except Exception as e:
        await err_queue.put(e)

async def proxy_ws_to_client(logger: logging.Logger, prefix: str, in_ws: websockets.WebSocketClientProtocol, out_ws: WebSocket,
                   err_queue: asyncio.Queue):
    try:
        while True:
            async for data in in_ws:
                logger.info(f"{prefix} {data}")
                await out_ws.send_text(data)
    except Exception as e:
        await err_queue.put(e)


# Main WebSocket handler
@app.websocket("/devtools/{id:path}")
async def websocket_endpoint(websocket: WebSocket, id: str):
    await websocket.accept()  # Upgrade to WebSocket

    # Create logger
    file_handler, logger = create_log(args.no_log, args.log, id)
    logger.info(f"---------- connection from {websocket.client.host} ----------")

    try:
        # Check remote version
        try:
            ver = await check_version(args.remote)
            logger.info(f"endpoint {args.remote} reported: {ver.decode()}")
        except Exception as e:
            msg = f"version error, got: {e}"
            logger.error(msg)
            raise HTTPException(status_code=500, detail=msg)

        # Connect to remote WebSocket
        endpoint = f"ws://{args.remote}/devtools/{id}"
        logger.info(f"connecting to {endpoint}")
        async with websockets.connect(endpoint, max_size=OUTGOING_BUFFER_SIZE) as out_ws:
            logger.info(f"connected to {endpoint}")

            # Proxy WebSocket traffic
            err_queue = asyncio.Queue()
            tasks = [
                asyncio.create_task(proxy_ws_to_remote(logger, "<-", websocket, out_ws, err_queue)),
                asyncio.create_task(proxy_ws_to_client(logger, "->", out_ws, websocket, err_queue))
            ]

            # Wait for an error or cancellation
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            if err_queue.qsize() > 0:
                raise await err_queue.get()

    except Exception as e:
        logger.error(f"Error: {e}")
        raise
    finally:
        logger.info(f"---------- closing {websocket.client.host} ----------")
        if file_handler:
            file_handler.close()


# HTTP routes for reverse proxy
app.add_api_route("/check_browser", check_browser, methods=["GET"])
app.add_api_route("/json", reverse_proxy, methods=["GET", "POST", "PUT", "DELETE"])
app.add_api_route("/{path:path}", reverse_proxy, methods=["GET", "POST", "PUT", "DELETE"])

# Run the server
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(listen_port)) # for debug