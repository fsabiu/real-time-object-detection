"""
Diagnostic WebRTC client to test the server without a browser.
This helps isolate if the issue is with the server logic or the browser networking.
"""
import asyncio
import aiohttp
import json
import logging
import sys
import time
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole, MediaRecorder

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("webrtc_client")

async def run_client(port=8080):
    url = f"http://localhost:{port}/offer"
    
    # Create PeerConnection
    pc = RTCPeerConnection()
    
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info(f"Connection state is {pc.connectionState}")

    @pc.on("track")
    def on_track(track):
        logger.info(f"Track received: {track.kind}")
        if track.kind == "video":
            logger.info("Video track received!")

    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info(f"Data channel received: {channel.label}")
        
        @channel.on("message")
        def on_message(message):
            logger.info(f"Received message: {message[:50]}...")

    # Create offer
    pc.addTransceiver("video", direction="recvonly")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    
    logger.info("Sending offer to server...")
    logger.debug(f"Offer SDP: {pc.localDescription.sdp}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Server returned error {response.status}: {text}")
                return
                
            data = await response.json()
            answer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
            
            logger.info("Received answer from server")
            logger.debug(f"Answer SDP: {answer.sdp}")
            
            await pc.setRemoteDescription(answer)
    
    logger.info("Waiting for connection...")
    
    # Keep alive for a bit to receive data
    start_time = time.time()
    while time.time() - start_time < 10:
        await asyncio.sleep(1)
        if pc.connectionState == "connected":
            logger.info("✅ Connection established successfully!")
        elif pc.connectionState == "failed":
            logger.error("❌ Connection failed!")
            break
            
    await pc.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Client error: {e}")
