"""
Configuration file for MCP-Gemini Client.
Set these values in your .env file or environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Google Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# MCP Server Connection (HTTP/SSE)
MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "localhost")
MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", "2506"))
MCP_SERVER_ENDPOINT = os.getenv("MCP_SERVER_ENDPOINT", "/sse")
