"""
MCP Client that connects to an MCP server and sends prompts to Gemini
with MCP server functions available as function calling tools.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional

import google.generativeai as genai
from mcp import ClientSession
from mcp.client.sse import sse_client

from config import GEMINI_API_KEY, MCP_SERVER_URLS

# Configure logging (file only; no console)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('mcp_gemini_client.log')]
)
logger = logging.getLogger(__name__)


def log_response_content(response):
    """Log the response candidate.content as plain text."""
    try:
        if hasattr(response, 'candidates') and response.candidates:
            for i, candidate in enumerate(response.candidates):
                if hasattr(candidate, 'content') and candidate.content:
                    logger.info(f"[GEMINI RAW RESPONSE] Candidate {i} content:")
                    logger.info(f"{candidate.content}")
    except Exception as e:
        logger.warning(f"[GEMINI RAW RESPONSE] Could not log response content: {e}")


def _tool_to_gemini(tool, prefix: str) -> tuple:
    """Convert MCP tool to Gemini tool dict; return (prefixed_name, gemini_tool)."""
    name = f"{prefix}{tool.name}"
    gemini_tool = {
        "name": name,
        "description": tool.description or "",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
    if getattr(tool, "inputSchema", None) and isinstance(tool.inputSchema, dict):
        s = tool.inputSchema
        if "properties" in s:
            gemini_tool["parameters"]["properties"] = s["properties"]
        if "required" in s:
            gemini_tool["parameters"]["required"] = s["required"]
    return name, gemini_tool


class MCPGeminiClient:
    """Client that bridges one or more MCP servers to Gemini API."""

    def __init__(self, gemini_api_key: str, mcp_server_urls: List[str]):
        self.gemini_api_key = gemini_api_key
        self.mcp_server_urls = mcp_server_urls if isinstance(mcp_server_urls, list) else [mcp_server_urls]
        self._sessions: List[ClientSession] = []
        self._transport_contexts = []
        self._session_contexts = []
        self._server_tools: List[List[Any]] = []  # per-server list of MCP Tool objects
        self.mcp_tools: List[Dict[str, Any]] = []
        self._tool_session: Dict[str, tuple] = {}  # prefixed_tool_name -> (session_idx, real_name)
        self._server_enabled: List[bool] = []  # which servers are selected for use
        self._connected = False
        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel("models/gemini-2.5-flash")

    async def connect_mcp_server(self):
        """Connect to all MCP servers and fetch their tools. Call apply_server_selection() to choose which to use."""
        if self._connected:
            return True
        for i, url in enumerate(self.mcp_server_urls):
            logger.info(f"Connecting to MCP server {i} ({url})...")
            transport = sse_client(url)
            read, write = await transport.__aenter__()
            self._transport_contexts.append(transport)
            session_ctx = ClientSession(read, write)
            session = await session_ctx.__aenter__()
            self._session_contexts.append(session_ctx)
            self._sessions.append(session)
            await session.initialize()
            tools_result = await session.list_tools()
            self._server_tools.append(list(tools_result.tools))
            for tool in tools_result.tools:
                logger.info(f"  server {i}: {tool.name} — {tool.description}")
        self._server_enabled = [True] * len(self._sessions)
        self._connected = True
        return True

    def apply_server_selection(self, enabled_indices: Optional[List[int]] = None):
        """Build mcp_tools and _tool_session for selected servers. None = all enabled."""
        self.mcp_tools.clear()
        self._tool_session.clear()
        indices = enabled_indices if enabled_indices is not None else list(range(len(self._sessions)))
        self._server_enabled = [i in indices for i in range(len(self._sessions))]
        for i in indices:
            if i >= len(self._server_tools):
                continue
            prefix = f"s{i}_"
            for tool in self._server_tools[i]:
                name, gemini_tool = _tool_to_gemini(tool, prefix)
                self.mcp_tools.append(gemini_tool)
                self._tool_session[name] = (i, tool.name)
        logger.info(f"Total {len(self.mcp_tools)} tools from {sum(self._server_enabled)} MCP server(s) enabled")

    async def disconnect_mcp_server(self):
        """Disconnect from all MCP servers."""
        for ctx in self._session_contexts:
            await ctx.__aexit__(None, None, None)
        for ctx in self._transport_contexts:
            await ctx.__aexit__(None, None, None)
        self._sessions.clear()
        self._session_contexts.clear()
        self._transport_contexts.clear()
        self._server_tools.clear()
        self._tool_session.clear()
        self.mcp_tools.clear()
        self._connected = False

    def _prompt_server_selection(self) -> List[int]:
        """Print server list with addresses and tools; return list of enabled server indices."""
        print("\nMCP servers available:")
        print("-" * 60)
        for i, url in enumerate(self.mcp_server_urls):
            tools = self._server_tools[i] if i < len(self._server_tools) else []
            tool_names = ", ".join(t.name for t in tools) or "(none)"
            print(f"  [{i}] {url}")
            print(f"      Tools: {tool_names}")
        print("-" * 60)
        while True:
            raw = input("Enable which servers? (e.g. 0 1 for both, or 0 for first only; Enter = all): ").strip()
            if not raw:
                return list(range(len(self.mcp_server_urls)))
            try:
                indices = [int(x) for x in raw.split()]
                if all(0 <= j < len(self.mcp_server_urls) for j in indices):
                    return list(dict.fromkeys(indices))  # preserve order, no dupes
            except ValueError:
                pass
            print("Invalid input. Use space-separated server numbers (e.g. 0 1).")

    async def call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the appropriate MCP server."""
        session_idx, real_name = self._tool_session.get(tool_name, (None, tool_name))
        if session_idx is None:
            raise RuntimeError(f"Unknown tool: {tool_name}")
        session = self._sessions[session_idx]
        logger.info(f"[MCP Tool Call] {tool_name} -> MCP server {session_idx} ({real_name})")
        result = await session.call_tool(real_name, arguments)
        
        # Extract the actual result data
        if hasattr(result, 'content'):
            content_parts = []
            parsed_result = None
            for item in result.content:
                if hasattr(item, 'text'):
                    text = item.text
                    # Try to parse as JSON if it looks like JSON
                    try:
                        parsed_result = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        content_parts.append(text)
                elif isinstance(item, dict) and 'text' in item:
                    text = item['text']
                    try:
                        parsed_result = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        content_parts.append(text)
                else:
                    content_parts.append(str(item))
            
            if parsed_result is not None:
                result_data = parsed_result
            elif content_parts:
                # If we have text parts, join them and try to parse as JSON
                combined = "\n".join(content_parts)
                try:
                    result_data = json.loads(combined)
                except (json.JSONDecodeError, ValueError):
                    result_data = combined
            else:
                result_data = None
        elif isinstance(result, dict):
            result_data = result
        else:
            # Try to parse if it's a JSON string
            if isinstance(result, str):
                try:
                    result_data = json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    result_data = result
            else:
                result_data = result
        
        logger.info(f"[MCP Tool Result] {tool_name} returned: {json.dumps(result_data, indent=2) if isinstance(result_data, (dict, list)) else result_data}")
        return result_data
    
    async def process_prompt(self, prompt: str) -> str:
        """
        Process a prompt by sending it to Gemini with MCP tools available.
        
        Args:
            prompt: User prompt to send to Gemini
            
        Returns:
            Response from Gemini
        """
        if not self._connected:
            await self.connect_mcp_server()
        
        tools_config = None
        if self.mcp_tools:
            tools_config = [{"function_declarations": self.mcp_tools}]
        
        if tools_config:
            logger.info(f"configured with tools!")
            model = genai.GenerativeModel(
                'models/gemini-2.5-flash',
                tools=tools_config
            )
        else:
            model = self.model
        
        logger.info("="*80)
        logger.info(f"[GEMINI PROMPT] {prompt}")
        logger.info("="*80)
        
        # Log the full request structure
        request_data = {
            "model": "models/gemini-2.5-flash",
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": tools_config if tools_config else None
        }
        logger.info(f"[GEMINI REQUEST] {json.dumps(request_data, indent=2)}")
        
        logger.info(f"[PROMPT] {prompt}")
        chat = model.start_chat()
        response = chat.send_message(prompt)
        
        # Log the raw response content as plain text
        log_response_content(response)
        
        while hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            
            if hasattr(candidate, 'content') and candidate.content:
                parts = candidate.content.parts
                
                function_calls = [part for part in parts if hasattr(part, 'function_call')]
                
                if function_calls:
                    logger.info(f"[GEMINI FUNCTION CALLS] {len(function_calls)} function call(s) detected")
                    
                    function_responses = []
                    for part in function_calls:
                        if not hasattr(part, 'function_call'):
                            logger.warning(f"[GEMINI FUNCTION CALL] Skipping invalid function call part")
                            continue
                            
                        func_call = part.function_call
                        
                        # Get tool name and validate it
                        if not hasattr(func_call, 'name'):
                            logger.warning(f"[GEMINI FUNCTION CALL] Function call missing name attribute")
                            continue
                            
                        tool_name = func_call.name
                        
                        # Skip if tool name is empty or None
                        if not tool_name or not tool_name.strip():
                            logger.warning(f"[GEMINI FUNCTION CALL] Skipping function call with empty name: '{tool_name}'")
                            continue
                        
                        # Handle case where args might be None
                        if hasattr(func_call, 'args') and func_call.args is not None:
                            arguments = dict(func_call.args)
                        else:
                            arguments = {}
                        
                        logger.info(f"[GEMINI FUNCTION CALL] {tool_name}")
                        logger.info(f"[GEMINI FUNCTION ARGS] {json.dumps(arguments, indent=2)}")
                        
                        try:
                            result = await self.call_mcp_tool(tool_name, arguments)
                            
                            # Ensure result is a dict/list, not a string
                            # Gemini expects function responses to be structured data
                            if isinstance(result, str):
                                try:
                                    result = json.loads(result)
                                except (json.JSONDecodeError, ValueError):
                                    # If it's not JSON, wrap it in a dict
                                    result = {"result": result}
                            
                            function_responses.append({
                                "function_response": {
                                    "name": tool_name,
                                    "response": result
                                }
                            })
                            
                            logger.info(f"[GEMINI FUNCTION RESPONSE] {tool_name}: {json.dumps(result, indent=2) if isinstance(result, (dict, list)) else result}")
                        except Exception as e:
                            logger.error(f"[GEMINI FUNCTION ERROR] {tool_name}: {e}", exc_info=True)
                            function_responses.append({
                                "function_response": {
                                    "name": tool_name,
                                    "response": {"error": str(e)}
                                }
                            })
                    
                    if function_responses:
                        logger.info(f"[GEMINI FOLLOW-UP REQUEST] Sending {len(function_responses)} function response(s) back to Gemini")
                        logger.info(f"[GEMINI FOLLOW-UP DATA] {json.dumps(function_responses, indent=2)}")
                        
                        response = chat.send_message(function_responses)
                        
                        # Log the raw follow-up response content as plain text
                        logger.info(f"[GEMINI RAW FOLLOW-UP RESPONSE]")
                        log_response_content(response)
                    else:
                        logger.warning(f"[GEMINI FUNCTION CALLS] No valid function responses to send, breaking loop")
                        break
                else:
                    break
            else:
                break
        
        if hasattr(response, 'text'):
            final_response = response.text
            logger.info(f"[GEMINI FINAL RESPONSE] {final_response}")
            return final_response
        elif hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content'):
                parts = candidate.content.parts
                text_parts = [part.text for part in parts if hasattr(part, 'text')]
                final_response = "\n".join(text_parts)
                logger.info(f"[GEMINI FINAL RESPONSE] {final_response}")
                return final_response
        
        final_response = str(response)
        logger.info(f"[GEMINI FINAL RESPONSE] {final_response}")
        return final_response
    
    async def interactive_mode(self):
        """Run in interactive mode, accepting prompts from user."""
        print("\n" + "="*60)
        print("MCP-Gemini Client - Interactive Mode")
        print("="*60)
        logger.info("Starting interactive mode")
        try:
            await self.connect_mcp_server()
            enabled = self._prompt_server_selection()
            self.apply_server_selection(enabled)
            print("\nType 'quit' or 'exit' to stop.\n")
            while True:
                try:
                    prompt = input("You: ").strip()
                    
                    if prompt.lower() in ['quit', 'exit', 'q']:
                        logger.info("User requested exit")
                        print("Goodbye!")
                        break
                    
                    if not prompt:
                        continue
                    
                    response = await self.process_prompt(prompt)
                    print(f"\nGemini: {response}\n")
                    
                except KeyboardInterrupt:
                    logger.info("Keyboard interrupt received")
                    print("\n\nGoodbye!")
                    break
                except Exception as e:
                    logger.error(f"Error in interactive mode: {e}", exc_info=True)
                    print(f"\nError: {e}\n")
        finally:
            await self.disconnect_mcp_server()
            logger.info("Disconnected from MCP server")


async def main():
    """Main entry point."""
    logger.info("Starting MCP-Gemini Client")
    
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not found in environment variables")
        print("Error: GEMINI_API_KEY not found in environment variables")
        print("Please set it in .env file or environment")
        sys.exit(1)
    
    logger.info(f"MCP servers: {MCP_SERVER_URLS}")
    client = MCPGeminiClient(GEMINI_API_KEY, MCP_SERVER_URLS)
    
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        logger.info(f"Processing single prompt from command line")
        await client.connect_mcp_server()
        client.apply_server_selection(None)  # use all servers
        response = await client.process_prompt(prompt)
        print(f"\n{response}")
        await client.disconnect_mcp_server()
    else:
        await client.interactive_mode()


if __name__ == "__main__":
    asyncio.run(main())
