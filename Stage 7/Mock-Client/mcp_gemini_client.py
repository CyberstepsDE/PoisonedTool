"""
MCP Client that connects to an MCP server and sends prompts to Gemini
with MCP server functions available as function calling tools.
"""

import asyncio
import hashlib
import json
import logging
import os
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
        # Persisted approvals: url -> bool (True = approved, False = banned)
        self._approval_store_path = os.path.join(os.path.dirname(__file__), "mcp_server_approvals.json")
        self._server_approvals: Dict[str, bool] = self._load_server_approvals()
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

    def _get_server_hash(self, tools: List[Any]) -> str:
        """Create a hash of all tool names and descriptions for a server."""
        # Create a stable string representation containing tool names and descriptions
        tool_info = []
        for tool in sorted(tools, key=lambda x: x.name):
            # Include name and description as requested
            desc = tool.description or ""
            tool_info.append(f"name:{tool.name}|desc:{desc}")
            
            # Also include inputSchema in the hash for completeness, 
            # as changes to parameters are also critical.
            if hasattr(tool, "inputSchema"):
                schema_json = json.dumps(tool.inputSchema, sort_keys=True)
                tool_info.append(f"schema:{schema_json}")
        
        info_str = "||".join(tool_info)
        return hashlib.sha256(info_str.encode('utf-8')).hexdigest()

    def _load_server_approvals(self) -> Dict[str, Any]:
        """Load persisted server approvals from disk (best-effort)."""
        try:
            if os.path.exists(self._approval_store_path):
                with open(self._approval_store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Support both new dict format and old boolean format
                        result = {}
                        for k, v in data.items():
                            if isinstance(v, bool):
                                # Convert old format to new format internally
                                result[str(k)] = {"approved": v, "tools_hash": None}
                            else:
                                result[str(k)] = v
                        return result
        except Exception as e:
            logger.warning(f"Failed to load server approvals: {e}")
        return {}

    def _save_server_approvals(self) -> None:
        """Persist server approvals to disk (best-effort)."""
        try:
            with open(self._approval_store_path, "w", encoding="utf-8") as f:
                json.dump(self._server_approvals, f, indent=2, sort_keys=True)
        except Exception as e:
            logger.warning(f"Failed to save server approvals: {e}")

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
        """Print server list; return list of enabled server indices (auto-selecting known approvals)."""
        # 1. Identify known vs new servers
        approved_indices = []
        banned_indices = []
        new_indices = []

        for i, url in enumerate(self.mcp_server_urls):
            stored_data = self._server_approvals.get(url)
            status = None
            stored_hash = None
            
            if isinstance(stored_data, dict):
                status = stored_data.get("approved")
                stored_hash = stored_data.get("tools_hash")
            
            # Fetch current tools for this server
            current_tools = self._server_tools[i] if i < len(self._server_tools) else []
            current_hash = self._get_server_hash(current_tools)

            if status is True:
                # For approved servers, verify tools hash
                if stored_hash and current_hash != stored_hash:
                    logger.error(f"Hash mismatch for server {url}!")
                    print("\n" + "!"*60)
                    print(f"CRITICAL SECURITY ALERT: Server tools for {url} have changed!")
                    print(f"Stored hash:  {stored_hash}")
                    print(f"Current hash: {current_hash}")
                    print("This change in functions or descriptions could indicate a malicious update.")
                    print("Execution halted for your safety.")
                    print("!"*60 + "\n")
                    sys.exit(0)
                approved_indices.append(i)
            elif status is False:
                banned_indices.append(i)
            else:
                new_indices.append(i)

        # 2. Display known servers (informational)
        if approved_indices:
            print("\nPreviously approved servers:")
            for i in approved_indices:
                url = self.mcp_server_urls[i]
                tools = self._server_tools[i] if i < len(self._server_tools) else []
                tool_names = ", ".join(t.name for t in tools) or "(no tools)"
                print(f"  [{i}] {url} (tools: {tool_names})")
        
        if banned_indices:
            print("\nPreviously banned servers:")
            for i in banned_indices:
                url = self.mcp_server_urls[i]
                tools = self._server_tools[i] if i < len(self._server_tools) else []
                tool_names = ", ".join(t.name for t in tools) or "(no tools)"
                print(f"  [{i}] {url} (tools: {tool_names})")

        # 3. If no new servers, we are done
        if not new_indices:
            print("\nNo new servers found. Using saved preferences.")
            return approved_indices

        # 4. Prompt for NEW servers only
        print("\nNew MCP servers found (require approval):")
        print("-" * 60)
        for i in new_indices:
            url = self.mcp_server_urls[i]
            tools = self._server_tools[i] if i < len(self._server_tools) else []
            if tools:
                tool_entries = []
                for t in tools:
                    desc = getattr(t, "description", "") or ""
                    if desc:
                        tool_entries.append(f"{t.name} — {desc}")
                    else:
                        tool_entries.append(t.name)
                tools_display = "; ".join(tool_entries)
            else:
                tools_display = "(none)"
            print(f"  [{i}] {url}")
            print(f"      Tools: {tools_display}")
        print("-" * 60)

        while True:
            # We urge the user to select from the NEW indices, but we'll accept any valid index just in case.
            prompt_indices = " ".join(str(x) for x in new_indices)
            raw = input(f"Enable which NEW servers? (e.g. {prompt_indices}; Enter = all new): ").strip()
            
            if not raw:
                # Default: Enable all NEW servers + keep already APPROVED servers
                logger.info("User accepted all new servers by default.")
                return approved_indices + new_indices
            
            if raw.lower() in ("none", "no"):
                 # Enable NONE of the new servers -> only return known approved
                return approved_indices

            try:
                # Parse user choices
                selected_indices = [int(x) for x in raw.split()]
                
                # Validation: check bounds
                if not all(0 <= j < len(self.mcp_server_urls) for j in selected_indices):
                     print("Invalid input. Some indices are out of range.")
                     continue
                
                valid_selections = []
                # Keep strictly new approved ones + old approved ones + any explicitly unbanned ones?
                # Simpler: Just merge user selection with old approved.
                # If user inputs an index that was banned, we'll unban it (add to list).
                
                final_set = set(approved_indices) | set(selected_indices)
                return sorted(list(final_set))

            except ValueError:
                pass
            print(f"Invalid input. Use space-separated server numbers (e.g. {prompt_indices}).")

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
            # Persist the user's approval/banned choices for next runs.
            enabled_set = set(enabled)
            for i, url in enumerate(self.mcp_server_urls):
                is_approved = i in enabled_set
                current_tools = self._server_tools[i] if i < len(self._server_tools) else []
                current_hash = self._get_server_hash(current_tools)
                
                self._server_approvals[url] = {
                    "approved": is_approved,
                    "tools_hash": current_hash if is_approved else None
                }
            self._save_server_approvals()
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
