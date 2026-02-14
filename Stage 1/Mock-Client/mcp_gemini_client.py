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

from config import GEMINI_API_KEY, MCP_SERVER_HOST, MCP_SERVER_PORT, MCP_SERVER_ENDPOINT

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mcp_gemini_client.log'),
        logging.StreamHandler(sys.stdout)
    ]
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


class MCPGeminiClient:
    """Client that bridges MCP server functions to Gemini API."""
    
    def __init__(self, gemini_api_key: str, mcp_server_url: str):
        """
        Initialize the client.
        
        Args:
            gemini_api_key: Google Gemini API key
            mcp_server_url: URL of the MCP server (e.g., "http://localhost:8000")
        """
        self.gemini_api_key = gemini_api_key
        self.mcp_server_url = mcp_server_url
        self.mcp_session: Optional[ClientSession] = None
        self.mcp_transport_context = None
        self.mcp_session_context = None
        self.mcp_tools: List[Dict[str, Any]] = []
        self._connected = False
        
        genai.configure(api_key=gemini_api_key)

        self.model = genai.GenerativeModel('models/gemini-2.5-flash')
    
    async def connect_mcp_server(self):
        """Connect to the MCP server and fetch available tools."""
        if self._connected:
            return True
            
        logger.info(f"Connecting to MCP server at {self.mcp_server_url}...")
        
        self.mcp_transport_context = sse_client(self.mcp_server_url)
        read, write = await self.mcp_transport_context.__aenter__()
        
        self.mcp_session_context = ClientSession(read, write)
        self.mcp_session = await self.mcp_session_context.__aenter__()
        
        await self.mcp_session.initialize()
        
        tools_result = await self.mcp_session.list_tools()
        logger.info(f"Found {len(tools_result.tools)} tools from MCP server:")
        
        self.mcp_tools = []
        for tool in tools_result.tools:
            logger.info(f"  - {tool.name}: {tool.description}")
            
            gemini_tool = {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
            
            if hasattr(tool, 'inputSchema') and tool.inputSchema:
                schema = tool.inputSchema
                if isinstance(schema, dict):
                    if 'properties' in schema:
                        gemini_tool["parameters"]["properties"] = schema['properties']
                    if 'required' in schema:
                        gemini_tool["parameters"]["required"] = schema['required']
            
            self.mcp_tools.append(gemini_tool)
        
        logger.info(f"MCP tools configuration: {json.dumps(self.mcp_tools, indent=2)}")
        
        self._connected = True
        logger.info("Connected to MCP server successfully!")
        return True
    
    async def disconnect_mcp_server(self):
        """Disconnect from the MCP server."""
        if self.mcp_session_context:
            await self.mcp_session_context.__aexit__(None, None, None)
            self.mcp_session_context = None
            self.mcp_session = None
        if self.mcp_transport_context:
            await self.mcp_transport_context.__aexit__(None, None, None)
            self.mcp_transport_context = None
        self._connected = False
    
    async def call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        if not self.mcp_session:
            raise RuntimeError("MCP session not initialized")
        
        logger.info(f"[MCP Tool Call] {tool_name} with arguments: {json.dumps(arguments, indent=2)}")
        
        result = await self.mcp_session.call_tool(tool_name, arguments)
        
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
        if not self.mcp_session:
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
        print("Type 'quit' or 'exit' to stop\n")
        logger.info("Starting interactive mode")
        
        try:
            await self.connect_mcp_server()
            
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
    
    mcp_server_url = f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}{MCP_SERVER_ENDPOINT}"
    logger.info(f"MCP Server URL: {mcp_server_url}")
    
    client = MCPGeminiClient(GEMINI_API_KEY, mcp_server_url)
    
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        logger.info(f"Processing single prompt from command line")
        response = await client.process_prompt(prompt)
        print(f"\n{response}")
        await client.disconnect_mcp_server()
    else:
        await client.interactive_mode()


if __name__ == "__main__":
    asyncio.run(main())
