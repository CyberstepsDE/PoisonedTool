"""
Configuration file for MCP-Gemini Client.
Set these values in your .env file or environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Google Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# MCP Servers: comma-separated full URLs. Each URL is a separate MCP server.
_default_servers = "http://localhost:2506/sse,http://localhost:3101/sse"
MCP_SERVER_URLS = [u.strip() for u in (os.getenv("MCP_SERVER_URLS") or _default_servers).split(",") if u.strip()]
